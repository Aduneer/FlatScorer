"""The config schema: its defaults, its validation, and the readers for one entry.

`validate_config` is pure and offline, and reports every problem at once rather
than the first. It is called from `run()`, from the CLI and from the GUI's Run
page, which is what lets a bad config fail before any network call happens.

This module sits one layer above the engine leaves: it imports the `DEFAULT_*`
constants from whichever module owns each concern in order to assemble
`DEFAULT_CONFIG`. Never import it from a leaf — `routing` reaching back here for
`DEFAULT_TRAVEL_MODE` would close an import cycle.
"""

from __future__ import annotations

import math
from typing import Any

from .osm import DEFAULT_POI_DEDUPE_TOLERANCE_M
from .routing import (
    DEFAULT_CYCLING_SPEED_M_PER_MIN,
    DEFAULT_TRAVEL_MODE,
    DEFAULT_WALKING_SPEED_M_PER_MIN,
    TRAVEL_MODES,
)
from .scoring import (
    DEFAULT_COMMUTE_CAP_MIN,
    DEFAULT_DEST_WEIGHT,
    DEFAULT_RENT_BUDGET_EUR,
    DEFAULT_SATURATION,
    DEFAULT_WEIGHTS,
)
from .spatial import DEFAULT_MAX_BBOX_SPAN_KM

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


def candidate_url(candidate: Any) -> str | None:
    """The listing URL a candidate carries, or None if it has none.

    Blank counts as absent: the GUI omits the key when its cell is empty, but a
    hand-written `"url": ""` means the same thing and shouldn't be an error.

    The scheme is re-checked here rather than assumed from `validate_config`,
    because `generate_map` renders this into an `<a href>` and is reachable from
    a config that skipped validation - `FlatScorer(config)` is a public entry
    point. A link that isn't http(s) is dropped rather than rendered.
    """
    if not isinstance(candidate, dict):
        return None
    url = candidate.get("url")
    if not isinstance(url, str):
        return None
    url = url.strip()
    return url if url.lower().startswith(("http://", "https://")) else None


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

    # Checked before the rent block, which returns early - every problem has to
    # be reported in one pass. Blank is absent, so only a non-empty value is
    # judged. The scheme matters because the link becomes an href in the map
    # popup and config.json is meant to be shared.
    url = candidate.get("url")
    if url is not None and str(url).strip():
        if not isinstance(url, str):
            problems.append(f"{label}: 'url' must be a string holding the listing link, "
                            f"got {type(url).__name__}")
        elif not url.strip().lower().startswith(("http://", "https://")):
            problems.append(f"{label}: 'url' must start with http:// or https://, got {url!r} - "
                            "it is rendered as a clickable link on the map")

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
