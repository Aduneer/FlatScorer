#!/usr/bin/env python3
"""
FlatScorer — Multi-criteria apartment scoring tool.

Scores candidate apartments based on nearby amenities, transit access,
green space, road-noise proximity, walking or cycling commute to
user-defined destinations, and rent — producing a ranked comparison table,
CSV export, interactive Folium map, and weight-sensitivity analysis.

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
from collections.abc import Iterable
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

# How close a POI node and an area have to be before they're treated as the same
# real-world feature mapped twice. Distance is measured to the area's geometry,
# not its centroid, so a node anywhere inside a large building already reads as 0
# and this only has to absorb nodes placed just outside a wall (an entrance, a
# doorway). Keep it small: the bigger it gets, the more genuinely distinct
# neighbouring POIs it starts swallowing.
DEFAULT_POI_DEDUPE_TOLERANCE_M = 10.0

# Largest side of the search bounding box, in km, that will be downloaded without
# complaint. Every resolved point has to fit inside this, so it is really a guard
# against a mis-geocoded address: "work" landing in the wrong Berlin produces a
# box hundreds of km across, and asking Overpass for that much pedestrian network
# is a long hang followed by a rejection. 30 km comfortably covers any real
# single-city search (greater London is ~45 km east-west; a config that genuinely
# spans that raises the parameter).
DEFAULT_MAX_BBOX_SPAN_KM = 30.0

# Assumed walking pace, in metres per minute: 83.33 is 5 km/h, the usual planning
# figure for an unhurried adult on the flat. It converts every routed distance
# into the minutes that `commute_cap_min` is measured against, so the two anchors
# only mean what they say together - lower this and every commute term drops.
DEFAULT_WALKING_SPEED_M_PER_MIN = 83.33

# Assumed cycling pace, in metres per minute: 250 is 15 km/h, the usual planning
# figure for urban cycling *including* junctions, lights and locking up - not the
# speed a fit rider holds on a clear path. Same relationship to `commute_cap_min`
# as the walking pace above.
DEFAULT_CYCLING_SPEED_M_PER_MIN = 250.0

# Travel modes a destination may declare via `"mode"`. Each entry names
# everything that is mode-specific about routing a commute, so adding a mode is a
# table entry rather than a branch in `run()`:
#   network_type   - the OSMnx street network to download for it
#   speed_param    - the `parameters` key holding its pace, which is also the
#                    FlatScorer attribute the pace is read back from
#   column_suffix  - what the destination's minutes column is called, so a
#                    cycling commute is never reported in a column saying "walk"
#   label/verb     - wording for the run log and the map popups
#
# The networks genuinely differ - the walk graph carries footways bikes may not
# use and drops roads they may - so a mode always routes over its own graph. A
# bike time computed on the walk network would be wrong in both directions at
# once, and wrong invisibly.
TRAVEL_MODES = {
    "walk": {
        "network_type": "walk",
        "speed_param": "walking_speed_m_per_min",
        "column_suffix": "walk_min",
        "label": "walking",
        "verb": "walk",
    },
    "bike": {
        "network_type": "bike",
        "speed_param": "cycling_speed_m_per_min",
        "column_suffix": "bike_min",
        "label": "cycling",
        "verb": "cycle",
    },
}

# What a destination that doesn't declare a mode gets. Every config written
# before cycling existed is an all-walk config, and has to keep scoring
# identically - including paying for exactly one street-network download.
DEFAULT_TRAVEL_MODE = "walk"

# Roughly how long each pipeline step takes relative to the others, used *only*
# to place a caller's progress bar. They are wall-clock ratios, not step counts,
# because the steps differ by more than an order of magnitude: a geocode is one
# rate-limited second while a street-network download is tens of them. Counting
# steps equally would park the bar at 20% for most of the run, which is barely
# better than the spinner it replaces. They do not have to be exact - the bar
# only has to keep moving and finish where it says it will.
PROGRESS_WEIGHTS = {
    "geocode": 1.0,   # per address
    "graph": 25.0,    # per travel mode; the single slowest thing here
    "pois": 20.0,
    "score": 2.0,     # per candidate
    "output": 1.0,    # CSV, sensitivity report and map together
}

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
            "mode": "walk",
            "icon": "landmark",
            "color": "blue"
        },
        "Union Station": {
            "address": "50 Massachusetts Ave NE, Washington, DC 20002, USA",
            "weight": 0.15,
            "mode": "walk",
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
        "walking_speed_m_per_min": DEFAULT_WALKING_SPEED_M_PER_MIN,
        "cycling_speed_m_per_min": DEFAULT_CYCLING_SPEED_M_PER_MIN,
        "poi_dedupe_tolerance_m": DEFAULT_POI_DEDUPE_TOLERANCE_M,
        "max_bbox_span_km": DEFAULT_MAX_BBOX_SPAN_KM,
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


class SearchAreaError(ValueError):
    """The area to download is too large to be a plausible apartment search.

    Raised before the Overpass call rather than after it: the whole complaint is
    that a mis-geocoded address makes the tool hang for minutes on a download
    that was never going to succeed. Like `ConfigError` this is a hard error and
    not a prompt, because `run()` is driven by the GUI as well as the CLI and
    Streamlit has no interactive channel to answer one.
    """

    def __init__(self, span_km: float, max_span_km: float, outlier: str | None, outlier_km: float):
        self.span_km = span_km
        self.max_span_km = max_span_km
        self.outlier = outlier
        self.outlier_km = outlier_km

        message = (f"The search area spans {span_km:.0f} km, over the "
                   f"{max_span_km:g} km limit.")
        if outlier:
            message += (f" {outlier} sits {outlier_km:.0f} km from the middle of the "
                        "candidate apartments, so it has most likely geocoded to the "
                        "wrong place - check that address first.")
        message += (" Downloading a street network this size would hang for a long time "
                    "and then be refused by Overpass. If the search really is this large, "
                    "raise parameters['max_bbox_span_km'].")
        super().__init__(message)


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


def destination_mode(info: Any) -> str:
    """The travel mode a destination declares, defaulting to walking.

    `validate_config` rejects anything outside `TRAVEL_MODES` before `run()`
    reaches this, so the fallback for an unknown value only covers callers that
    skipped validation - it keeps the map and the routing loop agreeing on one
    answer rather than raising halfway through a scored run.
    """
    if not isinstance(info, dict):
        return DEFAULT_TRAVEL_MODE
    mode = info.get("mode", DEFAULT_TRAVEL_MODE)
    return mode if mode in TRAVEL_MODES else DEFAULT_TRAVEL_MODE


def commute_column(dest_name: str, mode: str = DEFAULT_TRAVEL_MODE) -> str:
    """Name of the table/CSV column carrying a destination's commute minutes.

    The mode is part of the name, so a cycling commute is never reported in a
    column called `..._walk_min`. An all-walk config keeps exactly the columns it
    had before cycling existed.
    """
    suffix = TRAVEL_MODES.get(mode, TRAVEL_MODES[DEFAULT_TRAVEL_MODE])["column_suffix"]
    return f"{dest_name.lower().replace(' ', '_')}_{suffix}"


# Every suffix `commute_column` can produce, longest first so a shorter suffix
# can't strip a prefix of a longer one when a label is recovered from a column.
COMMUTE_COLUMN_SUFFIXES = tuple(
    sorted((f"_{spec['column_suffix']}" for spec in TRAVEL_MODES.values()), key=len, reverse=True)
)


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
            # An unrecognised mode can't be guessed at: silently walking a
            # destination the user meant to cycle would report a commute two to
            # three times too long and quietly demote every flat near it.
            if "mode" in dest_info and dest_info["mode"] not in TRAVEL_MODES:
                problems.append(f"{label}: 'mode' must be one of {', '.join(sorted(TRAVEL_MODES))}, got {dest_info['mode']!r}")

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
        # Each of these is a normalization anchor, a search radius or a size
        # limit; a zero or negative value silently zeroes or inverts the term it
        # governs, or (for max_bbox_span_km) rejects every possible search.
        for key in ("buffer_m", "noise_cap_m", "rent_budget_eur", "commute_cap_min", "max_bbox_span_km",
                    "walking_speed_m_per_min", "cycling_speed_m_per_min"):
            if key not in params:
                continue
            number = _as_number(params[key])
            if number is None:
                problems.append(f"parameters['{key}']: must be a number, got {params[key]!r}")
            elif number <= 0:
                problems.append(f"parameters['{key}']: must be greater than 0, got {number:g}")

        # Unlike the anchors above, 0 is meaningful here - it means "only merge a
        # node that falls exactly on the area" - so this one is >= 0, not > 0.
        if "poi_dedupe_tolerance_m" in params:
            tolerance = _as_number(params["poi_dedupe_tolerance_m"])
            if tolerance is None:
                problems.append(f"parameters['poi_dedupe_tolerance_m']: must be a number, got {params['poi_dedupe_tolerance_m']!r}")
            elif tolerance < 0:
                problems.append(f"parameters['poi_dedupe_tolerance_m']: cannot be negative, got {tolerance:g}")

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


def to_point(lat: float, lon: float, crs: str) -> Point:
    """Convert (lat, lon) in WGS84 to a projected Point geometry."""
    return gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs).iloc[0]


def check_search_area(points: dict[str, tuple[float, float]], crs: str,
                      max_span_km: float = DEFAULT_MAX_BBOX_SPAN_KM,
                      centre_labels: Iterable[str] | None = None) -> float:
    """Return the search area's longest side in km, raising if it exceeds the limit.

    `points` maps a display label ("candidate 'Flat A' (1 Main St)") to its
    resolved (lat, lon). The span is measured in `crs`, not in degrees: a degree
    of longitude is ~68 km at 52 deg N and ~111 km at the equator, so a threshold
    in degrees would mean a different thing in every city.

    `centre_labels` names the points that define where the search is *supposed*
    to be - the candidates. Measuring the outlier from their midpoint rather than
    from the midpoint of everything is what makes the message name the one wrong
    address instead of splitting the blame between the two ends of the box.

    On a wildly wrong search the auto-detected UTM zone fits neither end and the
    reported km overstate the true distance (a DC/Berlin mix reads ~6,900 km for
    a real ~6,400). That only ever errs towards rejecting, and by then the number
    is absurd either way; near the threshold, where it matters, it is accurate.
    """
    if not points:
        return 0.0

    labels = list(points)
    projected = gpd.GeoSeries(
        [Point(lon, lat) for lat, lon in (points[label] for label in labels)],
        crs="EPSG:4326",
    ).to_crs(crs)
    xs = list(projected.x)
    ys = list(projected.y)

    span_km = max(max(xs) - min(xs), max(ys) - min(ys)) / 1000.0
    if span_km <= max_span_km:
        return span_km

    # Only reached on the failure path, so the extra pass costs nothing in the
    # normal case.
    centre_set = set(centre_labels) if centre_labels else set()
    centre_indices = [i for i, label in enumerate(labels) if label in centre_set] or list(range(len(labels)))
    centre_x = sum(xs[i] for i in centre_indices) / len(centre_indices)
    centre_y = sum(ys[i] for i in centre_indices) / len(centre_indices)

    distances = [math.hypot(x - centre_x, y - centre_y) / 1000.0 for x, y in zip(xs, ys)]
    farthest = max(range(len(labels)), key=lambda i: distances[i])
    raise SearchAreaError(span_km, max_span_km, labels[farthest], distances[farthest])


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


def nearest_node(G: nx.MultiDiGraph, point: tuple[float, float]):
    """Graph node nearest to a (lat, lon) pair.

    A thin wrapper so callers can hoist the lookup out of a loop without
    repeating osmnx's (x, y) argument order, which is the reverse of ours.
    """
    return ox.distance.nearest_nodes(G, point[1], point[0])


def route_time(G: nx.MultiDiGraph, orig: tuple[float, float], dest: tuple[float, float], speed_m_per_min: float = DEFAULT_WALKING_SPEED_M_PER_MIN, projected_crs: str | None = None,
               orig_node=None, dest_node=None) -> tuple[float, list[tuple[float, float]]]:
    """Calculate travel time in minutes and the shortest-path route (list of lat/lon points) between two coordinates over OSM graph G.

    Nothing here is mode-specific: the network to route over and the pace to
    divide by both arrive as arguments, so the same function serves walking and
    cycling. It is the caller's job to pass a graph and a speed that agree with
    each other - a cycling pace over the pedestrian network is not a bike time.

    The speed defaults to `DEFAULT_WALKING_SPEED_M_PER_MIN` so the function stays
    usable standalone, but `run()` always passes the pace configured for the
    destination's mode - the default is a fallback, not the value the tool
    actually scores with.

    `orig_node`/`dest_node` let a caller supply an already-resolved graph node.
    `run()` does, because the endpoints repeat: without it the lookup runs
    2*candidates*destinations times where candidates+destinations would do, and
    on a city-sized graph that lookup is not cheap. Passing them is purely an
    optimisation - omit them and the same nodes are resolved here. They must come
    from *this* graph: a node id resolved against another mode's network names a
    different place, which yields a plausible number rather than an error.
    """
    if orig_node is None:
        orig_node = nearest_node(G, orig)
    if dest_node is None:
        dest_node = nearest_node(G, dest)
    try:
        path = nx.shortest_path(G, orig_node, dest_node, weight="length")
        length_m = nx.path_weight(G, path, weight="length")
        route = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in path]
        return length_m / speed_m_per_min, route
    except nx.NetworkXNoPath:
        print(f"[!] Warning: No route found between {orig} and {dest}. Defaulting to straight-line distance.")
        dist_m = straight_line_distance_m(orig, dest, projected_crs)
        return dist_m / speed_m_per_min, [orig, dest]


class FlatScorer:
    """Multi-criteria apartment scoring engine."""

    def __init__(self, config: dict[str, Any], verbose: bool = True,
                 progress: Any = None):
        self.config = config
        self.verbose = verbose

        # Optional `callable(fraction, label)` invoked as run() moves through the
        # pipeline: `fraction` is 0..1 of the work finished, `label` names the
        # step now starting. The GUI drives a progress bar with it; the CLI
        # passes nothing and every call below becomes a no-op.
        self.progress = progress
        self._progress_total = 0.0
        self._progress_done = 0.0
        self._progress_pending = 0.0

        self.candidates_raw = config.get("candidates", [])
        self.destinations_config = config.get("destinations", {})
        self.weights = config.get("weights", {})
        self.params = config.get("parameters", {})
        self.output_config = config.get("output", {})

        self.buffer_m = self.params.get("buffer_m", 500)
        self.noise_cap_m = self.params.get("noise_cap_m", 200)
        self.rent_budget_eur = self.params.get("rent_budget_eur", DEFAULT_RENT_BUDGET_EUR)
        self.commute_cap_min = self.params.get("commute_cap_min", DEFAULT_COMMUTE_CAP_MIN)
        self.poi_dedupe_tolerance_m = self.params.get("poi_dedupe_tolerance_m", DEFAULT_POI_DEDUPE_TOLERANCE_M)
        self.max_bbox_span_km = self.params.get("max_bbox_span_km", DEFAULT_MAX_BBOX_SPAN_KM)
        # Named to match TRAVEL_MODES[...]["speed_param"], which is how
        # `mode_speed()` finds the right one without a lookup table of its own.
        self.walking_speed_m_per_min = self.params.get("walking_speed_m_per_min", DEFAULT_WALKING_SPEED_M_PER_MIN)
        self.cycling_speed_m_per_min = self.params.get("cycling_speed_m_per_min", DEFAULT_CYCLING_SPEED_M_PER_MIN)
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

    def _plan_progress(self) -> float:
        """Total weight of the run about to start, from the config alone.

        Everything here is known before the first network call, which is the
        point: a bar whose maximum is discovered halfway through is a bar that
        jumps backwards. Geocoding failures make the plan an *over*-estimate
        (fewer candidates to score), so `_progress_finish` pins the end at 1.0
        rather than letting the bar stop short.
        """
        destinations = [d for d in self.destinations_config.values() if isinstance(d, dict)]
        modes = {destination_mode(d) for d in destinations}
        return (PROGRESS_WEIGHTS["geocode"] * (len(self.candidates_raw) + len(destinations))
                + PROGRESS_WEIGHTS["graph"] * len(modes)
                + PROGRESS_WEIGHTS["pois"]
                + PROGRESS_WEIGHTS["score"] * len(self.candidates_raw)
                + PROGRESS_WEIGHTS["output"])

    def _progress_step(self, label: str, weight: float = 0.0):
        """Announce the step about to run, banking the weight of the previous one.

        The fraction describes what has *finished* while the label describes what
        is *starting*, which is the pairing a progress bar wants: someone reading
        "Downloading the cycling street network..." at 35% is being told where the
        wait is, not where it was. Carrying the previous step's weight here is
        what lets each call name one thing and cost one thing.
        """
        if self.progress is None:
            return
        self._progress_done += self._progress_pending
        self._progress_pending = weight
        fraction = min(self._progress_done / self._progress_total, 1.0) if self._progress_total > 0 else 1.0
        self.progress(fraction, label)

    def _progress_finish(self, label: str = "Finished"):
        """Pin the bar at 1.0, whatever the plan estimated."""
        if self.progress is not None:
            self.progress(1.0, label)

    def mode_speed(self, mode: str) -> float:
        """The configured pace, in m/min, for one travel mode."""
        spec = TRAVEL_MODES.get(mode, TRAVEL_MODES[DEFAULT_TRAVEL_MODE])
        return float(getattr(self, spec["speed_param"]))

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

        self._progress_total = self._plan_progress()
        self._progress_done = 0.0
        self._progress_pending = 0.0

        self._log("Geocoding candidate apartment addresses...")
        self.failed_candidates = []
        self.failed_destinations = []
        resolved_candidates = {}
        for candidate in self.candidates_raw:
            name = candidate["name"]
            addr = candidate["address"]
            self._progress_step(f"Geocoding candidate '{name}'...", PROGRESS_WEIGHTS["geocode"])
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
            self._progress_step(f"Geocoding destination '{dest_name}'...", PROGRESS_WEIGHTS["geocode"])
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

        # Refuse an implausibly large box *before* the download rather than after
        # it: a single address geocoded to the wrong city is otherwise a multi-
        # minute hang ending in an Overpass rejection with nothing to act on.
        # Labels carry the address so the message points at the entry to fix.
        candidate_points = {
            f"candidate '{name}' ({info['address']})": info["coords"]
            for name, info in resolved_candidates.items()
        }
        area_points = dict(candidate_points, **{
            f"destination '{dest_name}' ({data['info']['address']})": data["coords"]
            for dest_name, data in resolved_destinations.items()
        })
        span_km = check_search_area(area_points, projected_crs, self.max_bbox_span_km,
                                    centre_labels=candidate_points)
        self._log(f"[+] Search area spans {span_km:.1f} km (limit {self.max_bbox_span_km:g} km)")

        # One street network per travel mode actually used, in the order the
        # destinations first mention them. Downloading lazily is what keeps an
        # all-walk config - every config that predates cycling, including the
        # shipped example - paying for exactly the one download it always paid
        # for; only a genuinely mixed config pays for a second.
        dest_modes = {
            dest_name: destination_mode(data["info"])
            for dest_name, data in resolved_destinations.items()
        }
        modes_in_use = list(dict.fromkeys(dest_modes.values()))

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

        graphs = {}
        for mode in modes_in_use:
            spec = TRAVEL_MODES[mode]
            self._log(f"\nDownloading the {spec['label']} street network from OpenStreetMap...")
            self._progress_step(f"Downloading the {spec['label']} street network from OpenStreetMap "
                                "(the slowest step - a minute is normal)...", PROGRESS_WEIGHTS["graph"])
            # network_type is bound as a default rather than closed over, so the
            # lambda can't be caught out by the loop variable moving on.
            graphs[mode] = query_with_retry(
                lambda network_type=spec["network_type"]: ox.graph_from_bbox(bbox=bbox, network_type=network_type)
            )
        if not modes_in_use:
            self._log("\nNo destinations to route to, so no street network is needed.")

        self._log("Downloading points of interest (POIs) from OpenStreetMap...")
        self._progress_step("Downloading shops, transit stops and green space from OpenStreetMap...",
                            PROGRESS_WEIGHTS["pois"])
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

        def dedupe(gdf, label, keep="points"):
            """Collapse node+area duplicates, reporting what it removed."""
            before = len(gdf) if gdf is not None else 0
            result = dedupe_features(gdf, self.poi_dedupe_tolerance_m, keep=keep)
            removed = before - (len(result) if result is not None else 0)
            if removed:
                self._log(f"    {label}: merged {removed} of {before} feature(s) mapped as both a node and an area")
            return result

        # Roads are excluded on purpose: they only feed nearest_distance_m, which
        # takes a minimum, so a duplicated road can't inflate anything.
        self._log("\nDeduplicating POIs mapped as both a node and an area...")
        supermarkets_p = dedupe(supermarkets_p, "supermarkets")
        bakeries_p     = dedupe(bakeries_p, "bakeries")
        pharmacies_p   = dedupe(pharmacies_p, "pharmacies")
        gyms_p         = dedupe(gyms_p, "gyms")
        transit_p      = dedupe(transit_p, "transit stops")
        # Green keeps the polygon instead: it carries the m² that the green score
        # is mostly made of, while the node would only add a 0.5 bonus.
        green_p        = dedupe(green_p, "green spaces", keep="areas")

        # Each destination's nearest graph node is the same for every candidate,
        # so resolve it once here instead of once per (candidate, destination).
        # Keyed by (mode, destination), never by destination alone: a node id is
        # only meaningful in the graph it came from, and the same id names a
        # different junction in the cycling network. Reusing one across modes
        # produces a plausible commute time rather than an error, so nothing
        # downstream would notice.
        dest_nodes = {
            (dest_modes[dest_name], dest_name): nearest_node(graphs[dest_modes[dest_name]], data["coords"])
            for dest_name, data in resolved_destinations.items()
        }

        # Worth stating: these convert every routed distance into the minutes that
        # commute_cap_min judges, so a reader comparing two runs needs to know them.
        paces = ", ".join(
            f"{TRAVEL_MODES[mode]['label']} at {self.mode_speed(mode):g} m/min "
            f"= {self.mode_speed(mode) * 60 / 1000:.1f} km/h"
            for mode in modes_in_use
        )
        self._log(f"\nScoring candidates ({paces})..." if paces else "\nScoring candidates...")
        metrics_by_name = {}
        routes_by_candidate = {}
        rows = []

        for name, info in resolved_candidates.items():
            self._progress_step(f"Scoring '{name}'...", PROGRESS_WEIGHTS["score"])
            lat, lon = info["coords"]
            rent = info["rent"]
            # Per mode for the same reason the destination cache is: this flat's
            # nearest walking junction and nearest cycling junction are different
            # nodes in different graphs.
            orig_nodes = {mode: nearest_node(graphs[mode], (lat, lon)) for mode in modes_in_use}

            green_area_m2, green_points = green_area_and_points(lat, lon, green_p, projected_crs, dist=self.buffer_m)
            dist_to_busy_road = nearest_distance_m(lat, lon, roads_p, projected_crs)
            effective_road_dist = dist_to_busy_road if dist_to_busy_road is not None else self.noise_cap_m

            dest_times = {}
            dest_routes = {}
            for dest_name, dest_data in resolved_destinations.items():
                dest_coords = dest_data["coords"]
                mode = dest_modes[dest_name]
                dest_times[dest_name], dest_routes[dest_name] = route_time(
                    graphs[mode], (lat, lon), dest_coords,
                    speed_m_per_min=self.mode_speed(mode),
                    projected_crs=projected_crs,
                    orig_node=orig_nodes[mode], dest_node=dest_nodes[(mode, dest_name)],
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
                row[commute_column(dest_name, dest_modes[dest_name])] = round(mins, 1)

            row["lat"] = lat
            row["lon"] = lon
            rows.append(row)

        self._progress_step("Ranking, checking weight sensitivity and building the map...",
                            PROGRESS_WEIGHTS["output"])

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

        self._progress_finish()
        return df

    def generate_map(self, df: pd.DataFrame, resolved_destinations: dict[str, Any], html_file: str, routes_by_candidate: dict[str, dict[str, list[tuple[float, float]]]] | None = None):
        """Generate interactive Folium map with candidate apartments, destination pins, and predicted commute routes."""
        routes_by_candidate = routes_by_candidate or {}
        first_lat = df.iloc[0]["lat"]
        first_lon = df.iloc[0]["lon"]
        m_map = folium.Map(location=[first_lat, first_lon], zoom_start=13)

        dest_modes = {name: destination_mode(data["info"]) for name, data in resolved_destinations.items()}
        # An all-walk map keeps the layer name it has always had; only a map that
        # actually mixes modes needs the broader wording.
        layer_name = ("Predicted walking routes" if set(dest_modes.values()) <= {"walk"}
                      else "Predicted commute routes")
        route_group = folium.FeatureGroup(name=layer_name, show=self.show_walk_routes)

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
                suffix = next((s for s in COMMUTE_COLUMN_SUFFIXES if col.endswith(s)), None)
                if suffix is None:
                    continue
                dest_label = col[:-len(suffix)].replace("_", " ").title()
                verb = next(spec["verb"] for spec in TRAVEL_MODES.values() if suffix == f"_{spec['column_suffix']}")
                dest_lines.append(f"{dest_label}: {row[col]} min {verb}")
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
                mode = dest_modes.get(dest_name, DEFAULT_TRAVEL_MODE)
                mins = row.get(commute_column(dest_name, mode))
                folium.PolyLine(
                    locations=route_coords,
                    color=color,
                    weight=3,
                    opacity=0.6,
                    # Lines are coloured by candidate score, so on a mixed map the
                    # dashes are the only thing separating a cycled leg from a
                    # walked one.
                    dash_array="8" if mode != DEFAULT_TRAVEL_MODE else None,
                    tooltip=f"{row['name']} → {dest_name}: {mins} min {TRAVEL_MODES[mode]['verb']}",
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
