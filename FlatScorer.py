#!/usr/bin/env python3
"""
FlatScorer — Multi-criteria apartment scoring tool.

Scores candidate apartments based on nearby amenities, transit access,
green space, road-noise proximity, walking commute to user-defined
destinations, and rent — producing a ranked comparison table, CSV
export, interactive Folium map, and weight-sensitivity analysis.

Usage:
    python FlatScorer.py --generate-config config.json
    python FlatScorer.py --config config.json

See README.md for full documentation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any

import folium
import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import requests
from osmnx._errors import InsufficientResponseError
from shapely.geometry import Point

# Configure default OSMnx settings
ox.settings.use_cache = True
ox.settings.log_console = False

# Identify this tool to Nominatim, per its usage policy:
# https://operations.osmfoundation.org/policies/nominatim/
ox.settings.useragent = "FlatScorer (github.com/Aduneer/FlatScorer)"

# Fallback Overpass API mirrors in case overpass-api.de has backend downtime
DEFAULT_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.private.coffee/api",
]

# Scores are a weighted average of 0..1 normalized metrics, rescaled to this
# maximum. Every score the tool produces is bounded to [0, SCORE_SCALE_MAX].
SCORE_SCALE_MAX = 10.0

# Winner/runner-up score gap below which the top two are reported as effectively
# tied. 0.25 on the fixed 0-10 scale, i.e. 2.5% of the range - unlike the old
# absolute threshold on an unbounded sum, this means the same thing in every run.
NARROW_MARGIN_THRESHOLD = 0.25

# Fractions of SCORE_SCALE_MAX at which map pins step from red to orange to green.
# Absolute, so a pin's colour means the same thing in every run.
MAP_COLOUR_BANDS = ((0.66, "green"), (0.33, "orange"), (0.0, "red"))

# Default relative importance of a destination that doesn't declare one.
DEFAULT_DEST_WEIGHT = 0.15

# "More is better" metrics are mapped onto 0..1 by a saturating curve; these are
# the half-credit points. Two supermarkets within the radius earn 0.5, four earn
# 0.67, and so on - the 6th supermarket is worth far less than the 2nd, which is
# closer to how people actually value amenities than a raw count is.
DEFAULT_SATURATION = {
    "supermarket": 2.0,
    "bakery": 2.0,
    "pharmacy": 1.0,
    "gym": 1.0,
    "transit": 4.0,
    # green_score units: m² of green within the radius / 1000, plus 0.5 per green
    # point feature. 30 ≈ a 30,000 m² park, or ~4% of a 500 m circle.
    "green": 30.0,
}

# "Less is better" metrics need an absolute anchor to normalize against: the
# value at which the term earns nothing at all.
DEFAULT_RENT_BUDGET_EUR = 2500.0
DEFAULT_COMMUTE_CAP_MIN = 45.0

DEFAULT_WEIGHTS = {
    "supermarket": 0.30,
    "bakery": 0.10,
    "pharmacy": 0.15,
    "gym": 0.15,
    "transit": 0.33,
    "green": 0.05,
    "noise": 0.05,
    "rent": 0.25,
}

# Nominatim's usage policy caps clients at 1 request/second, and osmnx does not
# throttle for us: https://operations.osmfoundation.org/policies/nominatim/
NOMINATIM_MIN_INTERVAL_S = 1.0

# Geocoding, like Overpass, fails transiently. Retry a couple of times before
# dropping a candidate from the ranking entirely.
GEOCODE_ATTEMPTS = 3
GEOCODE_BACKOFF_S = 2.0

# Monotonic timestamp of the last Nominatim request, shared process-wide so the
# rate limit holds across the candidate and destination loops (and across runs
# in the long-lived Streamlit process).
_last_geocode_at = 0.0

# Default configuration template (Generic demo using Washington, DC landmarks)
DEFAULT_CONFIG = {
    "candidates": [
        {
            "name": "Flat A - Dupont Circle",
            "address": "1500 Connecticut Ave NW, Washington, DC 20036, USA",
            "rent": 1800
        },
        {
            "name": "Flat B - Foggy Bottom",
            "address": "2100 Pennsylvania Avenue NW, Washington, DC 20037, USA",
            "rent": 2100
        },
        {
            "name": "Flat C - Logan Circle",
            "address": "1400 14th St NW, Washington, DC 20005, USA",
            "rent": 1950
        }
    ],
    "destinations": {
        "White House": {
            "address": "1600 Pennsylvania Ave NW, Washington, DC 20500, USA",
            "weight": 0.15,
            "icon": "landmark",
            "color": "blue"
        },
        "Union Station": {
            "address": "50 Massachusetts Ave NE, Washington, DC 20002, USA",
            "weight": 0.15,
            "icon": "train",
            "color": "red"
        }
    },
    "weights": dict(DEFAULT_WEIGHTS),
    "parameters": {
        "buffer_m": 500,
        "noise_cap_m": 200,
        "rent_budget_eur": DEFAULT_RENT_BUDGET_EUR,
        "commute_cap_min": DEFAULT_COMMUTE_CAP_MIN,
        "saturation": dict(DEFAULT_SATURATION),
        "projected_crs": "auto",
        "show_walk_routes": True
    },
    "output": {
        "csv_file": "apartment_scores.csv",
        "html_file": "apartment_map.html"
    }
}


class ConfigError(ValueError):
    """A configuration that cannot be scored, carrying *every* problem found.

    Reporting one problem at a time turns fixing a hand-edited config into a
    round-trip per typo, so `problems` holds the full list and the message
    renders all of them.
    """

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Configuration is not valid ({len(self.problems)} problem(s)):\n{body}")


def _as_number(value: Any) -> float | None:
    """Coerce a config value to float, or None if it isn't a usable number.

    `bool` is excluded deliberately: it passes `isinstance(x, int)`, so without
    this `"rent": true` would sail through as 1.0.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if isinstance(value, str):
        try:
            return _as_number(float(value.strip()))
        except ValueError:
            return None
    return None


def _validate_candidate(index: int, candidate: Any, problems: list[str], seen_names: dict[str, int]):
    """Check one candidate entry, appending any problems found."""
    label = f"candidates[{index}]"
    if not isinstance(candidate, dict):
        problems.append(f"{label}: expected an object with 'name', 'address' and 'rent', got {type(candidate).__name__}")
        return

    name = candidate.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{label}: 'name' is missing or empty")
    else:
        label = f"{label} ('{name}')"
        # run() keys resolved candidates by name, so a duplicate doesn't rank
        # twice - it silently overwrites the earlier flat and disappears.
        if name in seen_names:
            problems.append(f"{label}: duplicate name, already used by candidates[{seen_names[name]}] - names must be unique")
        else:
            seen_names[name] = index

    address = candidate.get("address")
    if not isinstance(address, str) or not address.strip():
        problems.append(f"{label}: 'address' is missing or empty")

    if "rent" not in candidate:
        problems.append(f"{label}: 'rent' is missing - a flat with no rent would score as if it were free, "
                        "taking full credit on the rent term and likely topping the ranking")
        return
    rent = _as_number(candidate["rent"])
    if rent is None:
        problems.append(f"{label}: 'rent' must be a number, got {candidate['rent']!r}")
    elif rent <= 0:
        problems.append(f"{label}: 'rent' is {rent:g} - a zero or negative rent takes full credit on the rent term "
                        "and would beat every real flat. Enter the actual monthly rent.")


def validate_config(config: Any) -> list[str]:
    """Return every reason `config` cannot be scored, as human-readable strings.

    Empty list means the config is usable. `--config` is the documented path for
    hand-edited JSON, so this is the most likely first-run failure - and a bare
    `KeyError: 'rent'` names neither the field's owner nor the other four things
    also wrong with the file.
    """
    problems: list[str] = []
    if not isinstance(config, dict):
        return [f"config: expected a JSON object at the top level, got {type(config).__name__}"]

    candidates = config.get("candidates")
    if candidates is None:
        problems.append("candidates: missing - at least one candidate apartment is required")
    elif not isinstance(candidates, list):
        problems.append(f"candidates: expected a list, got {type(candidates).__name__}")
    elif not candidates:
        problems.append("candidates: empty - at least one candidate apartment is required")
    else:
        seen_names: dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            _validate_candidate(index, candidate, problems, seen_names)

    destinations = config.get("destinations", {})
    if not isinstance(destinations, dict):
        problems.append(f"destinations: expected an object keyed by destination name, got {type(destinations).__name__}")
    else:
        for dest_name, dest_info in destinations.items():
            label = f"destinations['{dest_name}']"
            if not isinstance(dest_info, dict):
                problems.append(f"{label}: expected an object with an 'address', got {type(dest_info).__name__}")
                continue
            address = dest_info.get("address")
            if not isinstance(address, str) or not address.strip():
                problems.append(f"{label}: 'address' is missing or empty")
            if "weight" in dest_info:
                weight = _as_number(dest_info["weight"])
                if weight is None:
                    problems.append(f"{label}: 'weight' must be a number, got {dest_info['weight']!r}")
                elif weight < 0:
                    problems.append(f"{label}: 'weight' is negative ({weight:g}); weights are relative importances and cannot be below 0")

    weights = config.get("weights", {})
    if not isinstance(weights, dict):
        problems.append(f"weights: expected an object keyed by metric name, got {type(weights).__name__}")
    else:
        for key, value in weights.items():
            number = _as_number(value)
            if number is None:
                problems.append(f"weights['{key}']: must be a number, got {value!r}")
            elif number < 0:
                problems.append(f"weights['{key}']: is negative ({number:g}); weights are relative importances and cannot be below 0")
        # The score is a weighted *average*, so if nothing carries weight there is
        # nothing to average - every candidate scores 0 and the ranking is arbitrary.
        # Mirror _resolve_weight: an absent metric falls back to its default, and
        # destination weights count too (they live in `destinations`, not here).
        effective = [_as_number(weights.get(key, default)) for key, default in DEFAULT_WEIGHTS.items()]
        effective += [
            _as_number(d.get("weight", DEFAULT_DEST_WEIGHT))
            for d in (destinations.values() if isinstance(destinations, dict) else [])
            if isinstance(d, dict)
        ]
        if not any((n or 0.0) > 0 for n in effective):
            problems.append("weights: nothing carries any weight, so every candidate scores 0 - set at least one weight above 0")

    params = config.get("parameters", {})
    if not isinstance(params, dict):
        problems.append(f"parameters: expected an object, got {type(params).__name__}")
    else:
        # Each of these is a normalization anchor or a search radius; a zero or
        # negative value silently zeroes or inverts the term it governs.
        for key in ("buffer_m", "noise_cap_m", "rent_budget_eur", "commute_cap_min"):
            if key not in params:
                continue
            number = _as_number(params[key])
            if number is None:
                problems.append(f"parameters['{key}']: must be a number, got {params[key]!r}")
            elif number <= 0:
                problems.append(f"parameters['{key}']: must be greater than 0, got {number:g}")

        saturation = params.get("saturation", {})
        if not isinstance(saturation, dict):
            problems.append(f"parameters['saturation']: expected an object keyed by metric name, got {type(saturation).__name__}")
        else:
            for key, value in saturation.items():
                number = _as_number(value)
                if number is None:
                    problems.append(f"parameters['saturation']['{key}']: must be a number, got {value!r}")
                elif number <= 0:
                    problems.append(f"parameters['saturation']['{key}']: must be greater than 0, got {number:g} "
                                    "(it is the amount earning half credit, so it cannot be zero)")

    return problems


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


def _throttle_geocode():
    """Block until at least NOMINATIM_MIN_INTERVAL_S has passed since the last geocode."""
    global _last_geocode_at
    wait = NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _last_geocode_at)
    if wait > 0:
        time.sleep(wait)
    _last_geocode_at = time.monotonic()


def geocode_safe(address: str, label: str, attempts: int = GEOCODE_ATTEMPTS) -> tuple[float, float] | None:
    """Safely geocode an address into (latitude, longitude) tuple, with rate limiting and retries."""
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        _throttle_geocode()
        try:
            return ox.geocode(address)
        except InsufficientResponseError as e:
            # Nominatim answered, it just has no match - retrying cannot help.
            last_err = e
            break
        except Exception as e:  # noqa: BLE001 - geocoding can fail from network errors or osmnx's own exceptions; drop this candidate instead of crashing the run
            last_err = e
            if attempt < attempts:
                print(f"[!] Geocoding '{label}' failed (attempt {attempt}/{attempts}): {e} - retrying...")
                time.sleep(GEOCODE_BACKOFF_S * attempt)

    print(f"[!] Couldn't geocode '{label}': {address} ({last_err})")
    print("    Ensure the address includes street, house number, postal code, and city.")
    return None


def benefit_fraction(value: float, half_value: float) -> float:
    """Map an unbounded "more is better" metric onto 0..1 with diminishing returns.

    `half_value` is the amount that earns half credit: 0 -> 0.0, half_value -> 0.5,
    2x -> 0.67, 3x -> 0.75, approaching but never reaching 1.0. A non-positive
    half_value means "any at all is full credit".
    """
    value = max(float(value), 0.0)
    if half_value <= 0:
        return 1.0 if value > 0 else 0.0
    return value / (value + float(half_value))


def capped_fraction(value: float, cap: float) -> float:
    """Map a metric onto 0..1 by linear ramp, clamped at `cap`."""
    if cap <= 0:
        return 0.0
    return max(0.0, min(1.0, float(value) / float(cap)))


def cost_credit(value: float, cap: float) -> float:
    """Map a "less is better" metric onto 0..1: full credit at 0, none at/above `cap`."""
    return 1.0 - capped_fraction(value, cap)


def score_colour(score: float) -> str:
    """Map an absolute score onto a Folium marker colour."""
    fraction = float(score) / SCORE_SCALE_MAX if SCORE_SCALE_MAX else 0.0
    for threshold, colour in MAP_COLOUR_BANDS:
        if fraction > threshold:
            return colour
    return MAP_COLOUR_BANDS[-1][1]


def weight_shares(weights: dict[str, float]) -> dict[str, float]:
    """Each weight's share of the total, i.e. its actual influence on the score.

    Weights only ever act relative to one another (the score is a weighted
    *average*), so this - not the raw slider value - is what a weight means.
    An all-zero weight vector has no shares to report and yields zeros.
    """
    usable = {k: max(float(w), 0.0) for k, w in weights.items()}
    total = sum(usable.values())
    if total <= 0:
        return dict.fromkeys(usable, 0.0)
    return {k: w / total for k, w in usable.items()}


def _winner_margin(scores: dict[str, float]) -> float | None:
    """Return the score gap between the top pick and the runner-up, or None if fewer than two."""
    if len(scores) < 2:
        return None
    ranked = sorted(scores.values(), reverse=True)
    return ranked[0] - ranked[1]


def safe_filter(gdf: gpd.GeoDataFrame, col: str, val: Any) -> gpd.GeoDataFrame:
    """Filter a GeoDataFrame by column value(s), returning an empty GeoDataFrame if column missing."""
    if gdf is None or len(gdf) == 0 or col not in gdf.columns:
        return gdf.iloc[0:0] if gdf is not None else gpd.GeoDataFrame()
    if isinstance(val, list):
        return gdf[gdf[col].isin(val)]
    return gdf[gdf[col] == val]


def to_point(lat: float, lon: float, crs: str) -> Point:
    """Convert (lat, lon) in WGS84 to a projected Point geometry."""
    return gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs).iloc[0]


def count_nearby(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str, dist: float = 500) -> int:
    """Count features within `dist` meters of a given coordinate."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return 0
    buf = to_point(lat, lon, crs).buffer(dist)
    return int(gdf_proj.intersects(buf).sum())


def nearest_distance_m(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str) -> float | None:
    """Find distance in meters to the nearest feature in `gdf_proj`."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return None
    return float(gdf_proj.distance(to_point(lat, lon, crs)).min())


def green_area_and_points(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str, dist: float = 500) -> tuple[float, int]:
    """Calculate total green polygon area (m²) and green point count within `dist` meters."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return 0.0, 0
    buf = to_point(lat, lon, crs).buffer(dist)
    geom_type = gdf_proj.geometry.type
    polys = gdf_proj[geom_type.isin(["Polygon", "MultiPolygon"])]
    pts = gdf_proj[geom_type == "Point"]
    area_m2 = polys.geometry.intersection(buf).area.sum() if len(polys) else 0.0
    point_count = int(pts.intersects(buf).sum()) if len(pts) else 0
    return float(area_m2), point_count


def straight_line_distance_m(orig: tuple[float, float], dest: tuple[float, float], crs: str | None = None) -> float:
    """Straight-line distance in meters between two (lat, lon) pairs.

    With a projected `crs` this is a true metric distance. Without one it falls
    back to scaling degrees by 111 km, which overstates east-west distance away
    from the equator (~60% too long at 52 deg N) - so callers should pass the CRS.
    """
    if crs:
        return float(to_point(*orig, crs).distance(to_point(*dest, crs)))
    return float(Point(orig[1], orig[0]).distance(Point(dest[1], dest[0])) * 111000)


def walk_route(G: nx.MultiDiGraph, orig: tuple[float, float], dest: tuple[float, float], walking_speed_m_per_min: float = 83.33, projected_crs: str | None = None) -> tuple[float, list[tuple[float, float]]]:
    """Calculate walking time in minutes and the shortest-path route (list of lat/lon points) between two coordinates over OSM graph G (~5 km/h = 83.33 m/min)."""
    orig_node = ox.distance.nearest_nodes(G, orig[1], orig[0])
    dest_node = ox.distance.nearest_nodes(G, dest[1], dest[0])
    try:
        path = nx.shortest_path(G, orig_node, dest_node, weight="length")
        length_m = nx.path_weight(G, path, weight="length")
        route = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in path]
        return length_m / walking_speed_m_per_min, route
    except nx.NetworkXNoPath:
        print(f"[!] Warning: No walking path found between {orig} and {dest}. Defaulting to straight-line distance.")
        dist_m = straight_line_distance_m(orig, dest, projected_crs)
        return dist_m / walking_speed_m_per_min, [orig, dest]


class FlatScorer:
    """Multi-criteria apartment scoring engine."""

    def __init__(self, config: dict[str, Any], verbose: bool = True):
        self.config = config
        self.verbose = verbose

        self.candidates_raw = config.get("candidates", [])
        self.destinations_config = config.get("destinations", {})
        self.weights = config.get("weights", {})
        self.params = config.get("parameters", {})
        self.output_config = config.get("output", {})

        self.buffer_m = self.params.get("buffer_m", 500)
        self.noise_cap_m = self.params.get("noise_cap_m", 200)
        self.rent_budget_eur = self.params.get("rent_budget_eur", DEFAULT_RENT_BUDGET_EUR)
        self.commute_cap_min = self.params.get("commute_cap_min", DEFAULT_COMMUTE_CAP_MIN)
        # Per-metric half-credit points; a config may override any subset.
        self.saturation = dict(DEFAULT_SATURATION, **self.params.get("saturation", {}))
        self.configured_crs = self.params.get("projected_crs", "auto")
        self.show_walk_routes = self.params.get("show_walk_routes", True)

        # Populated by run(): (name, address) pairs that failed to geocode and
        # were therefore dropped from the ranking. Callers (e.g. the GUI) read
        # this to surface silent losses instead of quietly ranking fewer flats.
        self.failed_candidates: list[tuple[str, str]] = []
        self.failed_destinations: list[tuple[str, str]] = []

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def resolve_crs(self, lats: list[float], lons: list[float]) -> str:
        """Determine projected CRS (e.g. UTM zone) for metric calculations."""
        if self.configured_crs and self.configured_crs.lower() != "auto":
            return self.configured_crs

        # Auto-detect using GeoPandas estimate_utm_crs
        points_gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
        estimated_crs = points_gdf.estimate_utm_crs().to_string()
        self._log(f"[+] Auto-detected optimal projected CRS: {estimated_crs}")
        return estimated_crs

    def normalize_metrics(self, m: dict[str, Any]) -> dict[str, float]:
        """Put every raw metric on a common 0..1 scale, keyed by its weight name.

        Raw metrics arrive in wildly different units - a supermarket count, m² of
        park, euros of rent, minutes of walking - so weighting them directly meant
        whichever term happened to have the largest magnitude dominated regardless
        of its weight. Normalizing first is what makes the weights mean what the
        GUI implies they mean.

        Normalization is *absolute*, not min-max across the candidate set: every
        anchor (`saturation`, `rent_budget_eur`, `commute_cap_min`, `noise_cap_m`)
        is a configured constant, so a score means the same thing in every run and
        doesn't shift when an unrelated candidate is added or removed.
        """
        norm = {
            "supermarket": benefit_fraction(m.get("supermarket_count", 0), self.saturation["supermarket"]),
            "bakery":      benefit_fraction(m.get("bakery_count", 0), self.saturation["bakery"]),
            "pharmacy":    benefit_fraction(m.get("pharmacy_count", 0), self.saturation["pharmacy"]),
            "gym":         benefit_fraction(m.get("gym_count", 0), self.saturation["gym"]),
            "transit":     benefit_fraction(m.get("transit_count", 0), self.saturation["transit"]),
            "green":       benefit_fraction(m.get("green_score", 0.0), self.saturation["green"]),
            # Further from a busy road is quieter, up to the cap beyond which extra
            # distance buys nothing. Dividing by the cap (rather than a magic 20.0)
            # is what stops raising the cap from silently inflating noise's weight.
            "noise":       capped_fraction(m.get("noise_distance_m", 0.0), self.noise_cap_m),
            "rent":        cost_credit(m.get("rent_eur", 0.0), self.rent_budget_eur),
        }
        for dest_name, mins in m.get("destinations_min", {}).items():
            norm[f"dest_{dest_name}"] = cost_credit(mins, self.commute_cap_min)
        return norm

    def _resolve_weight(self, key: str, weights: dict[str, float]) -> float:
        """Look up a term's weight, falling back through the config to the defaults.

        Destination weights live in `destinations`, but an explicit `dest_<name>`
        entry in `weights` overrides it - that is how the sensitivity check nudges
        them. Negative weights are clamped to zero: a weighted average of 0..1
        values only stays inside 0..1 if no weight pulls the other way.
        """
        if key.startswith("dest_"):
            if key in weights:
                weight = weights[key]
            else:
                dest_name = key[len("dest_"):]
                weight = self.destinations_config.get(dest_name, {}).get("weight", DEFAULT_DEST_WEIGHT)
        else:
            weight = weights.get(key, DEFAULT_WEIGHTS.get(key, 0.0))
        return max(float(weight), 0.0)

    def score_breakdown(self, m: dict[str, Any], weights: dict[str, float]) -> dict[str, dict[str, float]]:
        """Per-term weight, influence share, normalized value and points contributed.

        The contributions sum to exactly the value `compute_score` returns, which
        makes it possible to answer "why did this flat win?" rather than just
        "this flat won".
        """
        norm = self.normalize_metrics(m)
        resolved = {key: self._resolve_weight(key, weights) for key in norm}
        shares = weight_shares(resolved)
        return {
            key: {
                "weight": resolved[key],
                "share": shares[key],
                "normalized": value,
                "contribution": SCORE_SCALE_MAX * shares[key] * value,
            }
            for key, value in norm.items()
        }

    def compute_score(self, m: dict[str, Any], weights: dict[str, float]) -> float:
        """Score a candidate on a fixed 0..SCORE_SCALE_MAX scale.

        A weighted average of the normalized metrics: every term sits in 0..1 and
        the weights are normalized to sum to 1, so the result is genuinely bounded
        - unlike the old raw weighted sum, which subtracted unbounded rent and
        commute terms and could land anywhere from negative to 40+.
        """
        return sum(term["contribution"] for term in self.score_breakdown(m, weights).values())

    def log_score_breakdown(self, metrics_by_name: dict[str, dict[str, Any]]):
        """Print each term's influence share and what it contributed to every candidate."""
        if not metrics_by_name:
            return
        breakdowns = {n: self.score_breakdown(m, self.weights) for n, m in metrics_by_name.items()}
        first = next(iter(breakdowns.values()))
        table = pd.DataFrame(
            {"share": {k: f"{v['share'] * 100:.1f}%" for k, v in first.items()}}
            | {
                name: {k: round(v["contribution"], 2) for k, v in bd.items()}
                for name, bd in breakdowns.items()
            }
        )
        table.loc["TOTAL"] = ["100.0%"] + [
            round(sum(v["contribution"] for v in bd.values()), 2) for bd in breakdowns.values()
        ]
        self._log(f"\nScore breakdown (points out of {SCORE_SCALE_MAX:.0f}, "
                  "'share' is each weight's influence after normalization):")
        self._log(table.to_string())

    def run_sensitivity_check(self, metrics_by_name: dict[str, dict[str, Any]], perturb: float = 0.2):
        """Run sensitivity analysis by nudging weights by +/- perturb% and observing winner stability."""
        baseline_scores = {n: self.compute_score(m, self.weights) for n, m in metrics_by_name.items()}
        if not baseline_scores:
            return

        baseline_top = max(baseline_scores, key=baseline_scores.get)
        baseline_margin = _winner_margin(baseline_scores)

        self._log(f"\nSensitivity check (each weight nudged +/-{int(perturb*100)}%, baseline winner: {baseline_top})")
        if baseline_margin is not None:
            self._log(f"Baseline margin over runner-up: {baseline_margin:.2f} points")
        self._log("-" * 75)

        # Build list of active weight keys
        active_weights = dict(self.weights)
        for dest_name in self.destinations_config:
            weight_key = f"dest_{dest_name}"
            if weight_key not in active_weights:
                active_weights[weight_key] = self.destinations_config[dest_name].get("weight", 0.15)

        any_flip = False
        narrowest_margin = baseline_margin
        for key, base_val in active_weights.items():
            for factor, label in [(1 + perturb, f"+{int(perturb*100)}%"), (1 - perturb, f"-{int(perturb*100)}%")]:
                w2 = dict(active_weights)
                w2[key] = base_val * factor
                scores2 = {n: self.compute_score(m, w2) for n, m in metrics_by_name.items()}
                new_top = max(scores2, key=scores2.get)
                margin = _winner_margin(scores2)
                if margin is not None and (narrowest_margin is None or margin < narrowest_margin):
                    narrowest_margin = margin
                flipped = new_top != baseline_top
                any_flip = any_flip or flipped
                marker = "  <- winner changes!" if flipped else ""
                margin_txt = f" (margin {margin:.2f})" if margin is not None else ""
                self._log(f"  {key:18s} {label:5s}  ->  top pick: {new_top}{margin_txt}{marker}")

        self._log("-" * 75)
        if not any_flip:
            self._log(f"Ranking is stable across all +/-{int(perturb*100)}% weight nudges - your top pick is robust.")
        else:
            self._log("Some weight changes flip the top pick - consider reviewing those criteria weights carefully.")

        if narrowest_margin is not None:
            self._log(f"Narrowest winner/runner-up gap seen: {narrowest_margin:.2f} points")
            if narrowest_margin < NARROW_MARGIN_THRESHOLD:
                self._log(f"[!] That is under {NARROW_MARGIN_THRESHOLD:.2f} points on a "
                          f"0-{SCORE_SCALE_MAX:.0f} scale - the top two are effectively tied; "
                          "treat the ranking as a toss-up rather than a clear winner.")

    def run(self) -> pd.DataFrame:
        """Execute geocoding, spatial network fetching, metric calculation, and reporting.

        Raises ConfigError before any network call if the config can't be scored.
        """
        problems = validate_config(self.config)
        if problems:
            raise ConfigError(problems)

        self._log("Geocoding candidate apartment addresses...")
        self.failed_candidates = []
        self.failed_destinations = []
        resolved_candidates = {}
        for candidate in self.candidates_raw:
            name = candidate["name"]
            addr = candidate["address"]
            # validate_config has already guaranteed this parses to a positive
            # number; coerce so a config written as "1800" still scores as 1800.
            rent = _as_number(candidate["rent"])
            coords = geocode_safe(addr, name)
            if coords:
                resolved_candidates[name] = {"coords": coords, "rent": rent, "address": addr, "raw": candidate}
            else:
                self.failed_candidates.append((name, addr))

        if not resolved_candidates:
            raise ValueError("No valid candidate apartment addresses could be geocoded.")

        if self.failed_candidates:
            print(f"\n[!] {len(self.failed_candidates)} candidate(s) dropped - they could not be geocoded "
                  "and are MISSING from the ranking:")
            for name, addr in self.failed_candidates:
                print(f"      - {name}: {addr}")

        self._log("\nGeocoding destination locations...")
        resolved_destinations = {}
        for dest_name, dest_info in self.destinations_config.items():
            addr = dest_info["address"]
            coords = geocode_safe(addr, dest_name)
            if coords:
                resolved_destinations[dest_name] = {
                    "coords": coords,
                    "info": dest_info
                }
            else:
                self.failed_destinations.append((dest_name, addr))
                print(f"[!] Warning: Destination '{dest_name}' could not be geocoded and will be skipped.")

        # Determine bounding box
        all_coords = [v["coords"] for v in resolved_candidates.values()] + [v["coords"] for v in resolved_destinations.values()]
        lats = [c[0] for c in all_coords]
        lons = [c[1] for c in all_coords]

        # Resolve projected CRS (UTM or user configured)
        projected_crs = self.resolve_crs(lats, lons)

        margin = 0.015
        bbox = (min(lons) - margin, min(lats) - margin, max(lons) + margin, max(lats) + margin)

        self._log("\nDownloading street walking network from OpenStreetMap...")
        def get_graph():
            return ox.graph_from_bbox(bbox=bbox, network_type="walk")

        def get_pois():
            tags = {
                "shop": ["supermarket", "bakery"],
                "amenity": ["pharmacy"],
                "leisure": ["fitness_centre", "park"],
                "landuse": ["grass", "forest"],
                "railway": ["tram_stop"],
                "highway": ["bus_stop", "primary", "secondary"],
            }
            return ox.features_from_bbox(bbox=bbox, tags=tags)

        G = query_with_retry(get_graph)
        self._log("Downloading points of interest (POIs) from OpenStreetMap...")
        pois = query_with_retry(get_pois)

        supermarkets = safe_filter(pois, "shop", "supermarket")
        bakeries     = safe_filter(pois, "shop", "bakery")
        pharmacies   = safe_filter(pois, "amenity", "pharmacy")
        gyms         = safe_filter(pois, "leisure", "fitness_centre")
        parks        = safe_filter(pois, "leisure", "park")
        greenland    = safe_filter(pois, "landuse", ["grass", "forest"])
        green_all    = pd.concat([gdf for gdf in [parks, greenland] if not gdf.empty]) if not (parks.empty and greenland.empty) else gpd.GeoDataFrame()
        bus          = safe_filter(pois, "highway", "bus_stop")
        tram         = safe_filter(pois, "railway", "tram_stop")
        transit      = pd.concat([gdf for gdf in [bus, tram] if not gdf.empty]) if not (bus.empty and tram.empty) else gpd.GeoDataFrame()
        busy_roads   = safe_filter(pois, "highway", ["primary", "secondary"])

        def proj(gdf):
            return gdf.to_crs(projected_crs) if (gdf is not None and len(gdf) > 0) else gdf

        supermarkets_p, bakeries_p, pharmacies_p, gyms_p, transit_p, green_p, roads_p = map(
            proj, [supermarkets, bakeries, pharmacies, gyms, transit, green_all, busy_roads]
        )

        self._log("\nScoring candidates...")
        metrics_by_name = {}
        routes_by_candidate = {}
        rows = []

        for name, info in resolved_candidates.items():
            lat, lon = info["coords"]
            rent = info["rent"]

            green_area_m2, green_points = green_area_and_points(lat, lon, green_p, projected_crs, dist=self.buffer_m)
            dist_to_busy_road = nearest_distance_m(lat, lon, roads_p, projected_crs)
            effective_road_dist = dist_to_busy_road if dist_to_busy_road is not None else self.noise_cap_m

            dest_times = {}
            dest_routes = {}
            for dest_name, dest_data in resolved_destinations.items():
                dest_coords = dest_data["coords"]
                dest_times[dest_name], dest_routes[dest_name] = walk_route(
                    G, (lat, lon), dest_coords, projected_crs=projected_crs
                )
            routes_by_candidate[name] = dest_routes

            m = {
                "supermarket_count": count_nearby(lat, lon, supermarkets_p, projected_crs, dist=self.buffer_m),
                "bakery_count":      count_nearby(lat, lon, bakeries_p, projected_crs, dist=self.buffer_m),
                "pharmacy_count":    count_nearby(lat, lon, pharmacies_p, projected_crs, dist=self.buffer_m),
                "gym_count":         count_nearby(lat, lon, gyms_p, projected_crs, dist=self.buffer_m),
                "transit_count":     count_nearby(lat, lon, transit_p, projected_crs, dist=self.buffer_m),
                "green_score":       green_area_m2 / 1000.0 + green_points * 0.5,
                "noise_distance_m":  effective_road_dist,
                "destinations_min":  dest_times,
                "rent_eur":          rent,
            }
            metrics_by_name[name] = m
            score = self.compute_score(m, self.weights)

            row = {
                "name": name,
                "score": round(score, 2),
                "rent_eur": rent,
                "supermarkets": m["supermarket_count"],
                "bakeries": m["bakery_count"],
                "pharmacies": m["pharmacy_count"],
                "gyms": m["gym_count"],
                "transit_stops": m["transit_count"],
                "green_area_m2": round(green_area_m2),
                "dist_busy_road_m": round(effective_road_dist),
            }

            for dest_name, mins in dest_times.items():
                clean_dest_key = dest_name.lower().replace(" ", "_")
                row[f"{clean_dest_key}_walk_min"] = round(mins, 1)

            row["lat"] = lat
            row["lon"] = lon
            rows.append(row)

        df = pd.DataFrame(rows).sort_values("score", ascending=False)
        self._log(f"\nScores are on a fixed 0-{SCORE_SCALE_MAX:.0f} scale and are comparable across runs.")
        self._log(df.drop(columns=["lat", "lon"]).to_string(index=False))

        self.log_score_breakdown(metrics_by_name)

        csv_file = self.output_config.get("csv_file", "apartment_scores.csv")
        df.to_csv(csv_file, index=False)
        self._log(f"\n[+] Saved score table to {csv_file}")

        self.run_sensitivity_check(metrics_by_name)

        # Generate Folium Map
        html_file = self.output_config.get("html_file", "apartment_map.html")
        self.generate_map(df, resolved_destinations, html_file, routes_by_candidate)

        return df

    def generate_map(self, df: pd.DataFrame, resolved_destinations: dict[str, Any], html_file: str, routes_by_candidate: dict[str, dict[str, list[tuple[float, float]]]] | None = None):
        """Generate interactive Folium map with candidate apartments, destination pins, and predicted walking routes."""
        routes_by_candidate = routes_by_candidate or {}
        first_lat = df.iloc[0]["lat"]
        first_lon = df.iloc[0]["lon"]
        m_map = folium.Map(location=[first_lat, first_lon], zoom_start=13)
        route_group = folium.FeatureGroup(name="Predicted walking routes", show=self.show_walk_routes)

        # Add destinations to map
        for dest_name, dest_data in resolved_destinations.items():
            coords = dest_data["coords"]
            info = dest_data["info"]
            icon_name = info.get("icon", "star")
            icon_color = info.get("color", "blue")
            folium.Marker(
                coords,
                tooltip=dest_name,
                popup=f"<b>Destination: {dest_name}</b><br>{info.get('address', '')}",
                icon=folium.Icon(color=icon_color, icon=icon_name, prefix="fa"),
            ).add_to(m_map)

        score_spread = float(df["score"].max() - df["score"].min())

        # Pins are coloured by absolute score, not by rank within the set. The old
        # min-max stretch always painted the worst candidate red and the best
        # green - even for a 0.1-point spread, which contradicted the sensitivity
        # report calling the same gap a tie. Now the scale is real, so two flats
        # that score alike simply get the same colour, and a set of mediocre flats
        # is allowed to be uniformly orange.
        self._log(f"[i] Map pins are coloured by absolute score: green above "
                  f"{MAP_COLOUR_BANDS[0][0] * SCORE_SCALE_MAX:.1f}, orange above "
                  f"{MAP_COLOUR_BANDS[1][0] * SCORE_SCALE_MAX:.1f}, red below.")
        if len(df) > 1 and score_spread < NARROW_MARGIN_THRESHOLD:
            self._log(f"[i] All candidates score within {score_spread:.2f} points of each other - "
                      "expect the pins to look alike, because they are alike.")

        for _, row in df.iterrows():
            color = score_colour(row["score"])

            dest_lines = []
            for col in df.columns:
                if col.endswith("_walk_min"):
                    dest_label = col.replace("_walk_min", "").replace("_", " ").title()
                    dest_lines.append(f"{dest_label}: {row[col]} min")
            dest_html = " | ".join(dest_lines)

            popup = (
                f"<b>{row['name']}</b><br>"
                f"Score: {row['score']} / {SCORE_SCALE_MAX:.0f}<br>"
                f"Rent: €{row['rent_eur']}<br>"
                f"Commute: {dest_html}<br>"
                f"Supermarkets: {row['supermarkets']} | Bakeries: {row['bakeries']}<br>"
                f"Pharmacies: {row['pharmacies']} | Gyms: {row['gyms']}<br>"
                f"Transit stops: {row['transit_stops']}<br>"
                f"Green area nearby: {row['green_area_m2']} m²<br>"
                f"Distance to busy road: {row['dist_busy_road_m']} m"
            )
            folium.Marker(
                [row["lat"], row["lon"]],
                tooltip=f"{row['name']} — Score: {row['score']} / {SCORE_SCALE_MAX:.0f}",
                popup=popup,
                icon=folium.Icon(color=color, icon="home", prefix="fa"),
            ).add_to(m_map)

            for dest_name, route_coords in routes_by_candidate.get(row["name"], {}).items():
                if not route_coords or len(route_coords) < 2:
                    continue
                dest_key = dest_name.lower().replace(" ", "_")
                mins = row.get(f"{dest_key}_walk_min")
                folium.PolyLine(
                    locations=route_coords,
                    color=color,
                    weight=3,
                    opacity=0.6,
                    tooltip=f"{row['name']} → {dest_name}: {mins} min",
                ).add_to(route_group)

        route_group.add_to(m_map)
        folium.LayerControl(collapsed=False).add_to(m_map)

        m_map.save(html_file)
        self._log(f"[+] Saved interactive map to {html_file}")


def _invocation() -> str:
    """How the user actually started this run, for accurate hint text.

    Installed via pip the entry point is `flatscorer`; from a checkout it's
    `python FlatScorer.py`. Telling someone to run the file they don't have is
    worse than no hint at all.
    """
    invoked_as = os.path.basename(sys.argv[0] or "")
    if invoked_as.endswith(".py"):
        return f"python {invoked_as}"
    return invoked_as or "flatscorer"


def main():
    parser = argparse.ArgumentParser(
        description="FlatScorer - Multi-Criteria Apartment Scoring Engine"
    )
    parser.add_argument(
        "-c", "--config", type=str, help="Path to JSON configuration file"
    )
    parser.add_argument(
        "--generate-config", type=str, help="Write default example config JSON to specified file and exit"
    )
    parser.add_argument(
        "--csv", type=str, help="Override output CSV file path"
    )
    parser.add_argument(
        "--html", type=str, help="Override output HTML map file path"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress detailed logs"
    )

    args = parser.parse_args()

    if args.generate_config:
        with open(args.generate_config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[+] Generated sample configuration file at '{args.generate_config}'")
        print("    Edit it with your own addresses, then run:")
        print(f"    {_invocation()} --config {args.generate_config}")
        sys.exit(0)

    if not args.config:
        print("No config file specified. Running with built-in demo data.")
        print(f"To create your own config: {_invocation()} --generate-config config.json")
        print()

    config = DEFAULT_CONFIG
    if args.config:
        if not os.path.exists(args.config):
            sys.exit(f"Error: Config file '{args.config}' not found.")
        with open(args.config, encoding="utf-8") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError as e:
                sys.exit(f"Error: Config file '{args.config}' is not valid JSON: {e}")

    # Validate before touching the network, and report every problem at once so a
    # hand-edited config takes one fix-and-rerun cycle rather than one per typo.
    problems = validate_config(config)
    if problems:
        print(f"Error: configuration is not valid ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)

    if args.csv:
        config.setdefault("output", {})["csv_file"] = args.csv
    if args.html:
        config.setdefault("output", {})["html_file"] = args.html

    scorer = FlatScorer(config, verbose=not args.quiet)
    scorer.run()


if __name__ == "__main__":
    main()
