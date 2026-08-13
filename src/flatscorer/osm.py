"""Everything that talks to Overpass, and the layers built out of what it returns.

`query_with_retry` is the only thing in the package that should ever reach
Overpass. Import-time side effect: this module configures the global osmnx
settings, which is why `geocode` imports it - the `http_user_agent` set here is
what lets this package talk to Nominatim and Overpass at all.
"""

from __future__ import annotations

import math
import time
from typing import Any, NamedTuple

import geopandas as gpd
import osmnx as ox
import requests

from . import __version__, paths

# How this tool introduces itself to Nominatim, Overpass and the tile servers.
# Nominatim's policy is explicit that this is not optional and that a library's
# own default will not do: "Provide a valid HTTP Referer or User-Agent
# identifying the application (stock User-Agents as set by http libraries will
# not do)."  https://operations.osmfoundation.org/policies/nominatim/
USER_AGENT = f"FlatScorer/{__version__} (+https://github.com/Aduneer/FlatScorer)"


def _apply_setting(name: str, value: Any):
    """Assign an osmnx setting, refusing to invent one that doesn't exist.

    `ox.settings` is a plain module, so `ox.settings.useragent = ...` does not
    raise when the setting has been renamed - it silently creates a dead
    attribute that nothing ever reads. That is not hypothetical: osmnx 1.x called
    it `useragent` and 2.x calls it `http_user_agent`, so for the whole of the
    2.x pin this package identified itself to Nominatim and Overpass as *osmnx*
    while believing it had complied with the policy. Nothing failed, nothing
    warned, and the one IP block we earned was attributed to osmnx's name.

    Failing loudly at import is the point. A rename inside our `osmnx<3` pin is
    unlikely, and CI would catch it long before a user does - which is a far
    better trade than another silent multi-year miscompliance.
    """
    if not hasattr(ox.settings, name):
        raise AttributeError(
            f"osmnx has no setting named {name!r} - it was probably renamed upstream. "
            "Fix the name here rather than letting the assignment silently do nothing; "
            "some of these settings are policy obligations, not preferences."
        )
    setattr(ox.settings, name, value)


class RateLimitedError(RuntimeError):
    """An OSM service answered 429. The run stops here, on purpose.

    Deliberately *not* a `requests` exception subclass: `query_with_retry`
    catches those and reads them as "try again, then try somewhere else", which
    is the one response a rate-limit must never get.
    """

    def __init__(self, url: str, retry_after_s: int | None = None):
        self.url = url
        self.retry_after_s = retry_after_s
        wait = (
            f"It asked us to wait {retry_after_s} seconds"
            if retry_after_s
            else "Wait a few minutes"
        )
        super().__init__(
            f"{url} answered 429 Too Many Requests - we are over its rate limit. "
            f"{wait} before running again. FlatScorer stops rather than retrying or "
            "moving to another mirror: a client that keeps knocking after a 429 is how "
            "a throttle turns into a ban, and our User-Agent names this project, so "
            "such a ban would land on FlatScorer for everybody rather than on one "
            "anonymous IP. Little is lost by stopping - whatever already downloaded is "
            "cached, so a later run resumes from there."
        )


def _retry_after_seconds(response) -> int | None:
    """The `Retry-After` header as whole seconds, when the server sent a usable one.

    The header may also carry an HTTP-date, which is not parsed here: the value
    only sharpens the wording of an error message, and "wait a few minutes" is a
    fine fallback for a form neither Overpass nor Nominatim is known to send.
    """
    try:
        return max(int(response.headers.get("Retry-After", "")), 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


def _raise_on_rate_limit(response, *args, **kwargs):
    """A `requests` response hook that turns any 429 into an immediate stop.

    It has to sit this low because osmnx handles 429 itself and never lets the
    status code reach us: `_overpass_request` sleeps 55 seconds and re-sends the
    same query *recursively, without a bound*. So the missing 429 branch in
    `query_with_retry` could not have been written there - there is no exception
    to branch on, and the real behaviour against a rate-limited mirror was not
    "retry twice and move on" but "knock every 55 seconds forever".

    A hook runs inside `requests.Session.send`, before osmnx sees the response
    at all, and an exception raised here propagates straight out through the
    osmnx call. `_configure_osmnx` installs it in `settings.requests_kwargs`,
    which osmnx splats into its Nominatim *and* Overpass requests alike - so one
    hook covers every service this package talks to, which is what makes the
    stop sign global rather than per-caller.
    """
    if response.status_code == 429:
        raise RateLimitedError(response.url, _retry_after_seconds(response))
    return response


def _configure_osmnx():
    """Apply this project's global osmnx settings. Idempotent, run at import."""
    _apply_setting("use_cache", True)
    _apply_setting("log_console", False)

    # Set explicitly rather than left to osmnx's own default, which happens to be
    # the same "cache" relative to the working directory. Routing it through
    # `paths` is what makes relocating it a one-line change later - and a frozen
    # app has no usable working directory to default to.
    _apply_setting("cache_folder", paths.cache_dir())

    # Both, not just one: osmnx sends a Referer of its own too, and a request
    # claiming to be osmnx in either header is the same misidentification.
    _apply_setting("http_user_agent", USER_AGENT)
    _apply_setting("http_referer", USER_AGENT)

    # The 429 stop sign, wired in at the single point every osmnx HTTP call goes
    # through - see `_raise_on_rate_limit` for why it cannot live any higher up.
    # Note that `timeout` still cannot be smuggled in here: osmnx passes it
    # separately and the duplicate keyword raises (see `_mirror_is_reachable`).
    # `hooks` collides with nothing.
    _apply_setting("requests_kwargs", {"hooks": {"response": [_raise_on_rate_limit]}})


_configure_osmnx()


# ODbL requires a Produced Work to credit its source, and every number this tool
# emits is derived from OpenStreetMap. The map gets the notice free from folium's
# tile layer and the report carries `report.CREDIT_HTML`; this is the plain-text
# form for the CSV, which had none - despite being the artifact most likely to be
# mailed to someone on its own, which is precisely when the credit has to travel
# with the file rather than with the tool that made it.
# **No commas in this string.** It becomes the CSV's first line, so a reader who
# forgets `comment="#"` parses it as the header. Comma-free, pandas yields a
# single column named after this notice - which tells the reader exactly what
# happened. Add a comma and it yields two columns instead, which is a bit less
# obviously wrong for no benefit. (Neither case raises: pandas absorbs the extra
# fields into a MultiIndex rather than erroring, so "it will fail loudly" is not
# the guarantee here - "it will look wrong in a self-explanatory way" is.)
OSM_ATTRIBUTION_TEXT = (
    "Data © OpenStreetMap contributors — available under the ODbL — "
    "https://www.openstreetmap.org/copyright — generated by FlatScorer "
    "(https://github.com/Aduneer/FlatScorer)"
)


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


class TransitLayer(NamedTuple):
    """One class of public-transport stop, and what a stop of that class is worth."""

    tag: str
    value: str
    weight: float
    label: str


# Not every stop is worth the same. A metro station and a once-hourly bus stop
# were counted identically before this table existed, which flattered a flat on
# a quiet bus route and undersold one on a metro line.
#
# **Weights are in bus-stop equivalents**, deliberately: a bus stop stays 1.0, so
# `DEFAULT_SATURATION["transit"]` keeps the meaning it was calibrated with and
# the change only moves scores where the transit genuinely is better. Anything
# that rescales this table silently re-tunes that anchor with it.
#
# The numbers are a judgement about service level, not a user preference - how
# much transit matters to *you* is already `weights["transit"]`, and a second
# knob on the same axis would only be a way to double-count the same opinion.
#
# Adding a class is an entry here and nothing else: `run()` iterates this table
# to build, dedupe and weight the layers. Order is most- to least-significant,
# which is the order the run log reports them in.
#
# A stop tagged as more than one class - `highway=bus_stop` *and*
# `railway=tram_stop` on one pole, which is how interchanges are commonly mapped
# - matches both layers and contributes both weights (2.5 here). That is
# intended: it really does give you both services. It is also the one place the
# per-class split can double-count a single node, so it is pinned by a test.
TRANSIT_LAYERS = (
    TransitLayer("railway", "station", 3.0, "rail/metro stations"),
    TransitLayer("railway", "halt", 2.0, "rail halts"),
    TransitLayer("railway", "tram_stop", 1.5, "tram stops"),
    TransitLayer("highway", "bus_stop", 1.0, "bus stops"),
)


# The per-feature weight `count_nearby` sums. Leading underscore because it is
# ours, not an OSM tag, and it must not collide with one.
TRANSIT_WEIGHT_COLUMN = "_transit_weight"


def transit_tags() -> dict[str, list[str]]:
    """The Overpass tag filter covering every transit class, grouped by tag key."""
    tags: dict[str, list[str]] = {}
    for layer in TRANSIT_LAYERS:
        tags.setdefault(layer.tag, []).append(layer.value)
    return tags


def with_transit_weight(gdf: gpd.GeoDataFrame, weight: float) -> gpd.GeoDataFrame:
    """Stamp every row of a transit layer with what one of its stops is worth."""
    if gdf is None or len(gdf) == 0:
        return gdf
    result = gdf.copy()
    result[TRANSIT_WEIGHT_COLUMN] = float(weight)
    return result


# A (connect, read) pair for the liveness probe below - the shape osmnx's own
# timeout setting cannot take. Generous enough that a merely busy mirror is not
# mistaken for a dead one, short enough that an unreachable host costs seconds.
MIRROR_PROBE_TIMEOUT_S = (5.0, 10.0)


def _mirror_is_reachable(mirror: str, timeout=MIRROR_PROBE_TIMEOUT_S) -> bool:
    """Is this Overpass instance answering at all? Cheap, and fast to give up on.

    This exists because the obvious fix doesn't work. osmnx spends
    `settings.requests_timeout` on two unrelated jobs: the `requests` client
    timeout *and* the server-side `[timeout:N]` directive it interpolates into
    the Overpass QL. So lowering it to fail fast on a dead host also tells a
    live one to abandon a big query early, and a `(connect, read)` tuple - the
    thing that would separate the two - renders as `[timeout:(5, 10)]` and gets
    the query rejected outright. Nor can the timeout be smuggled in through
    `settings.requests_kwargs`: osmnx passes `timeout=` *and* splats that dict
    into the same call, so it raises on the duplicate keyword.

    With the default 180 s, two unreachable mirrors at two attempts each is
    ~12 minutes of total silence. Probing `/api/status` with a real connect
    timeout turns that into ~20 seconds.

    **Any HTTP response means reachable**, including 404 and 504: the question
    is whether the host is there, not whether it likes the request. Only a
    connection-level failure counts as dead, which is also what keeps a mirror
    that doesn't publish `/api/status` from being skipped for no reason.

    The single exception is 429, which is not an answer about the host at all
    but an instruction to stop, and which therefore raises rather than returning
    either verdict. This is the one request in the package that does not go
    through osmnx, so the hook in `requests_kwargs` cannot fire for it and the
    same stop sign has to be applied by hand.
    """
    base = mirror.rstrip("/").removesuffix("/interpreter")
    try:
        response = requests.get(f"{base}/status", timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.exceptions.RequestException:
        return False
    _raise_on_rate_limit(response)
    return True


def query_with_retry(fn, mirrors=DEFAULT_OVERPASS_MIRRORS, retries_per_mirror=2, backoff_s=3):
    """Run an Overpass-backed OSMnx call with automatic fallback across mirrors.

    Mirror selection is scoped to this call: `ox.settings.overpass_url` is a
    process-wide global, so leaving it pointed at whichever mirror happened to
    answer last would silently carry over into later runs - which matters in the
    long-lived Streamlit process, not just across CLI invocations.

    Each mirror is probed for reachability before it is used, so an unreachable
    one costs seconds instead of minutes - see `_mirror_is_reachable`.

    **A 429 ends the run instead of moving through the mirror list.** The
    fallback here is for mirrors that are down or broken; a rate-limit is
    neither, it is the server telling this client to stop, and answering it by
    asking a different server the same question is mirror-shopping around a
    limit we were told about. `RateLimitedError` is raised out of this function
    untouched - see `_raise_on_rate_limit` for where it comes from.
    """
    original_url = ox.settings.overpass_url
    last_err = None
    try:
        for mirror in mirrors:
            if not _mirror_is_reachable(mirror):
                print(f"[!] {mirror} is not answering, skipping it...")
                last_err = last_err or ConnectionError(f"{mirror} unreachable")
                continue
            ox.settings.overpass_url = mirror
            for attempt in range(1, retries_per_mirror + 1):
                try:
                    return fn()
                except RateLimitedError:
                    # Explicit, though `RateLimitedError` is not in the tuple
                    # below and would leave on its own: the whole point of this
                    # function is retrying and falling through, so the one error
                    # that must do neither should say so where a reader is
                    # looking, and should survive someone widening that tuple.
                    raise
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
