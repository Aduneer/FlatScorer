"""FlatScorer — score and compare candidate apartments against OpenStreetMap data.

The public surface is re-exported here, so `from flatscorer import validate_config`
and `import flatscorer as fs; fs.route_time(...)` both work without anyone having
to know which submodule owns a name.

The re-export is *lazy*, via PEP 562. `import flatscorer` on its own must stay
cheap: osmnx, geopandas and folium together are seconds of cold start, and the
eventual frozen desktop build wants a window on screen before it pays that. So
nothing heavy is imported until a name is actually touched.

Patching note for tests: setting an attribute here shadows `__getattr__`, so
`monkeypatch.setattr(flatscorer, "FlatScorer", Fake)` works and is what the GUI
tests rely on. But it only affects lookups that go *through the package* — code
inside a submodule that calls its own sibling is unaffected. Patch the owning
module (`flatscorer.geocode.geocode_safe`) when you mean to intercept the engine.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("flatscorer")
except PackageNotFoundError:  # running from a checkout with nothing installed
    __version__ = "0.0.0.dev0"

# name -> submodule that defines it. Every public name lives here, plus the two
# private helpers the tests reach for through the package. `tests/test_api.py`
# asserts each entry actually resolves, so a name that moves without this table
# moving fails loudly rather than at some caller's import.
_EXPORTS = {
    # config
    "DEFAULT_CONFIG": "config",
    "ConfigError": "config",
    "validate_config": "config",
    "destination_mode": "config",
    "candidate_url": "config",
    "candidate_image": "config",
    "_as_number": "config",
    # geocode
    "geocode_safe": "geocode",
    "GEOCODE_ATTEMPTS": "geocode",
    "GEOCODE_BACKOFF_S": "geocode",
    "NOMINATIM_MIN_INTERVAL_S": "geocode",
    # osm
    "query_with_retry": "osm",
    "safe_filter": "osm",
    "dedupe_features": "osm",
    "DEFAULT_OVERPASS_MIRRORS": "osm",
    "DEFAULT_POI_DEDUPE_TOLERANCE_M": "osm",
    # spatial
    "SearchAreaError": "spatial",
    "check_search_area": "spatial",
    "resolve_crs": "spatial",
    "to_point": "spatial",
    "count_nearby": "spatial",
    "nearest_distance_m": "spatial",
    "green_area_and_points": "spatial",
    "straight_line_distance_m": "spatial",
    "DEFAULT_MAX_BBOX_SPAN_KM": "spatial",
    # routing
    "TRAVEL_MODES": "routing",
    "DEFAULT_TRAVEL_MODE": "routing",
    "DEFAULT_WALKING_SPEED_M_PER_MIN": "routing",
    "DEFAULT_CYCLING_SPEED_M_PER_MIN": "routing",
    "nearest_node": "routing",
    "route_time": "routing",
    "commute_column": "routing",
    "COMMUTE_COLUMN_SUFFIXES": "routing",
    # scoring
    "Anchors": "scoring",
    "benefit_fraction": "scoring",
    "capped_fraction": "scoring",
    "cost_credit": "scoring",
    "score_colour": "scoring",
    "weight_shares": "scoring",
    "normalize_metrics": "scoring",
    "resolve_weight": "scoring",
    "score_breakdown": "scoring",
    "compute_score": "scoring",
    "_winner_margin": "scoring",
    "SCORE_SCALE_MAX": "scoring",
    "NARROW_MARGIN_THRESHOLD": "scoring",
    "MAP_COLOUR_BANDS": "scoring",
    "DEFAULT_WEIGHTS": "scoring",
    "DEFAULT_DEST_WEIGHT": "scoring",
    "DEFAULT_SATURATION": "scoring",
    "DEFAULT_RENT_BUDGET_EUR": "scoring",
    "DEFAULT_COMMUTE_CAP_MIN": "scoring",
    # mapping
    "generate_map": "mapping",
    "_listing_link_html": "mapping",
    # report
    "generate_report": "report",
    # scorer / cli
    "FlatScorer": "scorer",
    "PROGRESS_WEIGHTS": "scorer",
    "main": "cli",
}

# Derived from _EXPORTS rather than written out, so the two cannot drift;
# `tests/test_api.py` pins that they agree.
__all__ = sorted(name for name in _EXPORTS if not name.startswith("_"))  # noqa: PLE0605


def __getattr__(name: str):
    """Resolve a re-exported name to the submodule that owns it (PEP 562)."""
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
