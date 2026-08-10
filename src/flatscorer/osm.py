"""Everything that talks to Overpass, and the layers built out of what it returns.

`query_with_retry` is the only thing in the package that should ever reach
Overpass. Import-time side effect: this module configures the global osmnx
settings, which is why `geocode` imports it for the useragent.
"""

from __future__ import annotations

import math
import time
from typing import Any

import geopandas as gpd
import osmnx as ox
import requests

from . import paths


def _configure_osmnx():
    """Apply this project's global osmnx settings. Idempotent, run at import."""
    ox.settings.use_cache = True
    ox.settings.log_console = False

    # Set explicitly rather than left to osmnx's own default, which happens to be
    # the same "cache" relative to the working directory. Routing it through
    # `paths` is what makes relocating it a one-line change later - and a frozen
    # app has no usable working directory to default to.
    ox.settings.cache_folder = paths.cache_dir()

    # Identify this tool to Nominatim, per its usage policy:
    # https://operations.osmfoundation.org/policies/nominatim/
    ox.settings.useragent = "FlatScorer (github.com/Aduneer/FlatScorer)"


_configure_osmnx()


# Fallback Overpass API mirrors in case overpass-api.de has backend downtime
DEFAULT_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.private.coffee/api",
]


# How close a POI node and an area have to be before they're treated as the same
# real-world feature mapped twice. Distance is measured to the area's geometry,
# not its centroid, so a node anywhere inside a large building already reads as 0
# and this only has to absorb nodes placed just outside a wall (an entrance, a
# doorway). Keep it small: the bigger it gets, the more genuinely distinct
# neighbouring POIs it starts swallowing.
DEFAULT_POI_DEDUPE_TOLERANCE_M = 10.0


def query_with_retry(fn, mirrors=DEFAULT_OVERPASS_MIRRORS, retries_per_mirror=2, backoff_s=3):
    """Run an Overpass-backed OSMnx call with automatic fallback across mirrors.

    Mirror selection is scoped to this call: `ox.settings.overpass_url` is a
    process-wide global, so leaving it pointed at whichever mirror happened to
    answer last would silently carry over into later runs - which matters in the
    long-lived Streamlit process, not just across CLI invocations.
    """
    original_url = ox.settings.overpass_url
    last_err = None
    try:
        for mirror in mirrors:
            ox.settings.overpass_url = mirror
            for attempt in range(1, retries_per_mirror + 1):
                try:
                    return fn()
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.HTTPError,
                        ConnectionError,
                        TimeoutError) as e:
                    last_err = e
                    print(f"[!] {mirror} attempt {attempt}/{retries_per_mirror} failed: {e}")
                    time.sleep(backoff_s)
            print(f"[!] Giving up on {mirror}, trying next mirror...")
    finally:
        ox.settings.overpass_url = original_url

    raise RuntimeError(
        "All Overpass mirrors failed. Check your network connection or OSM status."
    ) from last_err


def safe_filter(gdf: gpd.GeoDataFrame, col: str, val: Any) -> gpd.GeoDataFrame:
    """Filter a GeoDataFrame by column value(s), returning an empty GeoDataFrame if column missing."""
    if gdf is None or len(gdf) == 0 or col not in gdf.columns:
        return gdf.iloc[0:0] if gdf is not None else gpd.GeoDataFrame()
    if isinstance(val, list):
        return gdf[gdf[col].isin(val)]
    return gdf[gdf[col] == val]


def _feature_name(row: Any) -> str:
    """A feature's `name` tag, normalized for comparison, or "" when untagged."""
    if "name" not in row:
        return ""
    value = row["name"]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip().casefold()


def dedupe_features(gdf_proj: gpd.GeoDataFrame, tolerance_m: float = DEFAULT_POI_DEDUPE_TOLERANCE_M,
                    keep: str = "points") -> gpd.GeoDataFrame:
    """Collapse features that OSM maps twice - once as a node, once as an area.

    `features_from_bbox()` returns nodes, ways and relations alike, and mapping a
    supermarket on *both* a POI node and its building outline is extremely common
    in well-mapped cities. Counting both inflates amenity counts, and it inflates
    them hardest exactly where mapping is densest - the opposite of what a
    livability score wants.

    Two features are treated as the same thing when the area's geometry comes
    within `tolerance_m` of the node (distance to a polygon is 0 when the node is
    inside it, so this catches a node anywhere in a large building) *and* their
    `name` tags don't actively disagree. An unnamed building next to a named node
    still merges, which is the common shape of the duplicate; two differently
    named shops never do.

    `keep` decides which copy survives:
      - `"points"` - drop the redundant area. Right for amenity *counts*, where
        one supermarket should contribute 1 whichever way it's drawn.
      - `"areas"`  - drop the redundant node. Right for green space, where the
        polygon carries the m² that `green_area_and_points` needs.

    Node-vs-node duplicates are deliberately left alone: bus stops legitimately
    come in pairs a few meters apart on opposite sides of a road, and collapsing
    those would be the same bug in the other direction.
    """
    if gdf_proj is None or len(gdf_proj) < 2 or tolerance_m < 0:
        return gdf_proj

    # Everything below works on positions, never index labels: concatenated
    # layers (bus + tram) can repeat labels, and dropping by label would take
    # unrelated rows with them.
    is_point = list(gdf_proj.geometry.geom_type == "Point")
    point_positions = [i for i, point in enumerate(is_point) if point]
    area_positions = [i for i, point in enumerate(is_point) if not point]
    if not point_positions or not area_positions:
        return gdf_proj

    # Whichever copy we keep, the test is the same: does this feature sit on top
    # of one from the other set? Only which set gets discarded changes.
    discard_positions, retain_positions = (
        (area_positions, point_positions) if keep == "points" else (point_positions, area_positions)
    )
    retained = gdf_proj.iloc[retain_positions]

    redundant = set()
    for position in discard_positions:
        geometry = gdf_proj.geometry.iloc[position]
        if geometry is None or geometry.is_empty:
            continue
        candidates = retained.sindex.query(geometry.buffer(tolerance_m), predicate="intersects")
        if len(candidates) == 0:
            continue
        discard_name = _feature_name(gdf_proj.iloc[position])
        for candidate in candidates:
            retain_name = _feature_name(retained.iloc[candidate])
            if not discard_name or not retain_name or discard_name == retain_name:
                redundant.add(position)
                break

    if not redundant:
        return gdf_proj
    return gdf_proj.iloc[[i for i in range(len(gdf_proj)) if i not in redundant]]
