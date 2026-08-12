"""Overpass access and the layers built from what it returns.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import time
from typing import Any

import geopandas as gpd
import osmnx as ox
import pandas as pd
import pytest
import requests
from conftest import (
    BERLIN_CRS,
    only_problem,
    valid_config,
)
from shapely.geometry import Point, Polygon

import flatscorer as fs

# ---------------------------------------------------------------- safe_filter --

def make_gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry=[Point(0, 0)] * len(rows), crs="EPSG:4326")


def test_safe_filter_matches_a_scalar_value():
    gdf = make_gdf([{"shop": "supermarket"}, {"shop": "bakery"}])
    assert len(fs.safe_filter(gdf, "shop", "supermarket")) == 1


def test_safe_filter_matches_any_value_in_a_list():
    gdf = make_gdf([{"landuse": "grass"}, {"landuse": "forest"}, {"landuse": "retail"}])
    assert len(fs.safe_filter(gdf, "landuse", ["grass", "forest"])) == 2


def test_safe_filter_returns_empty_when_the_column_is_absent():
    # Overpass simply omits tags nobody used in the bbox - that must not raise.
    gdf = make_gdf([{"shop": "supermarket"}])
    result = fs.safe_filter(gdf, "railway", "tram_stop")
    assert len(result) == 0
    assert list(result.columns) == list(gdf.columns)


def test_safe_filter_tolerates_none_and_empty_input():
    assert len(fs.safe_filter(None, "shop", "supermarket")) == 0
    assert len(fs.safe_filter(make_gdf([]), "shop", "supermarket")) == 0

# ----------------------------------------------------------- query_with_retry --

MIRRORS = ["https://mirror-a/api", "https://mirror-b/api"]
CONFIGURED_MIRROR = "https://configured-by-the-user/api"

# Captured at import, before `reachable_mirrors` below can patch it out: the
# probe's own tests need the real implementation, not the stub every other
# mirror test runs against.
REAL_MIRROR_PROBE = fs.osm._mirror_is_reachable


@pytest.fixture(autouse=True)
def reachable_mirrors(monkeypatch):
    """Treat every mirror as answering, unless a test says otherwise.

    `query_with_retry` probes each mirror before using it, and the sentinel URLs
    here resolve to nothing - so without this every mirror test would exercise
    the skip path rather than the fallback logic it means to test.
    """
    monkeypatch.setattr(fs.osm, "_mirror_is_reachable", lambda mirror, **kw: True)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture
def pinned_mirror(monkeypatch):
    """Pin overpass_url to a known sentinel so leak assertions can't pass by accident."""
    monkeypatch.setattr(ox.settings, "overpass_url", CONFIGURED_MIRROR)
    return CONFIGURED_MIRROR


def test_query_with_retry_returns_the_first_success(no_sleep):
    assert fs.query_with_retry(lambda: "data", mirrors=MIRRORS) == "data"


def test_query_with_retry_falls_through_to_the_next_mirror(no_sleep):
    seen = []

    def fails_on_mirror_a():
        seen.append(ox.settings.overpass_url)
        if ox.settings.overpass_url == MIRRORS[0]:
            raise TimeoutError("down")
        return "data"

    assert fs.query_with_retry(fails_on_mirror_a, mirrors=MIRRORS, retries_per_mirror=2) == "data"
    assert seen == [MIRRORS[0], MIRRORS[0], MIRRORS[1]]


def test_query_with_retry_restores_the_mirror_setting_after_success(no_sleep, pinned_mirror):
    # ox.settings.overpass_url is process-wide; the Streamlit app runs many
    # queries in one process, so a leaked mirror would persist across runs.
    fs.query_with_retry(lambda: "data", mirrors=MIRRORS)
    assert ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_restores_the_mirror_setting_after_partial_failure(no_sleep, pinned_mirror):
    def fails_on_mirror_a():
        if ox.settings.overpass_url == MIRRORS[0]:
            raise TimeoutError("down")
        return "data"

    fs.query_with_retry(fails_on_mirror_a, mirrors=MIRRORS, retries_per_mirror=1)
    assert ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_restores_the_mirror_setting_when_all_mirrors_fail(no_sleep, pinned_mirror):
    def always_fails():
        raise TimeoutError("down")

    with pytest.raises(RuntimeError, match="All Overpass mirrors failed"):
        fs.query_with_retry(always_fails, mirrors=MIRRORS, retries_per_mirror=1)
    assert ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_does_not_swallow_unexpected_errors(no_sleep):
    # Only network-shaped failures are worth retrying; a bug in the callable
    # should surface immediately rather than being retried six times.
    def broken():
        raise ValueError("bad tags")

    with pytest.raises(ValueError, match="bad tags"):
        fs.query_with_retry(broken, mirrors=MIRRORS)

# ------------------------------------------------------------- POI deduplication --

def building(cx: float, cy: float, half: float = 20.0) -> Polygon:
    """A square 'building' footprint centred on a projected coordinate."""
    return Polygon([(cx - half, cy - half), (cx + half, cy - half),
                    (cx + half, cy + half), (cx - half, cy + half)])


def poi_gdf(rows: list[tuple[Any, str | None]]) -> gpd.GeoDataFrame:
    """Build a projected POI layer from (geometry, name) pairs."""
    return gpd.GeoDataFrame(
        {"name": [name for _, name in rows]},
        geometry=[geometry for geometry, _ in rows],
        crs=BERLIN_CRS,
    )


ORIGIN_X, ORIGIN_Y = 390000.0, 5819000.0


def test_a_shop_mapped_as_both_node_and_building_counts_once():
    """The core 1.6 bug: one supermarket drawn twice used to count as two."""
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), "Rewe"),
    ])
    assert len(gdf) == 2
    assert len(fs.dedupe_features(gdf)) == 1


def test_the_surviving_copy_is_the_node_by_default():
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), "Rewe"),
    ])
    assert list(fs.dedupe_features(gdf).geometry.geom_type) == ["Point"]


def test_an_unnamed_building_still_merges_with_a_named_node():
    """The usual shape of the duplicate — the name is only on the POI node."""
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), None),
    ])
    assert len(fs.dedupe_features(gdf)) == 1


def test_a_node_inside_a_large_building_merges_regardless_of_centroid_distance():
    """Distance is measured to the geometry, not the centroid.

    A node by the entrance of a 200 m hypermarket is nowhere near its centroid,
    so a centroid-distance rule would miss exactly the biggest shops.
    """
    gdf = poi_gdf([
        (Point(ORIGIN_X + 95, ORIGIN_Y + 95), "Kaufland"),
        (building(ORIGIN_X, ORIGIN_Y, half=100.0), "Kaufland"),
    ])
    assert len(fs.dedupe_features(gdf)) == 1


def test_differently_named_neighbours_are_never_merged():
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Aldi"),
        (building(ORIGIN_X, ORIGIN_Y), "Lidl"),
    ])
    assert len(fs.dedupe_features(gdf)) == 2


def test_two_separate_buildings_are_left_alone():
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X + 400, ORIGIN_Y), "Edeka"),
    ])
    assert len(fs.dedupe_features(gdf)) == 2


def test_bus_stops_on_opposite_sides_of_a_road_both_survive():
    """Node-vs-node is deliberately never merged.

    A stop pair 20 m apart is two real stops; collapsing them would be the same
    inflation bug pointing the other way.
    """
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Hauptstr."),
        (Point(ORIGIN_X, ORIGIN_Y + 20), "Hauptstr."),
    ])
    assert len(fs.dedupe_features(gdf)) == 2


def test_a_distant_node_does_not_merge_with_a_building():
    gdf = poi_gdf([
        (Point(ORIGIN_X + 200, ORIGIN_Y + 200), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), "Rewe"),
    ])
    assert len(fs.dedupe_features(gdf)) == 2


def test_green_space_keeps_the_polygon_not_the_node():
    """Inverted preference: the polygon carries the m² the green score needs."""
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Volkspark"),
        (building(ORIGIN_X, ORIGIN_Y, half=50.0), "Volkspark"),
    ])
    deduped = fs.dedupe_features(gdf, keep="areas")
    assert list(deduped.geometry.geom_type) == ["Polygon"]


def test_green_dedupe_removes_the_double_counted_point_bonus():
    """A park mapped both ways used to earn its area *and* a 0.5 point bonus."""
    park = building(ORIGIN_X, ORIGIN_Y, half=50.0)

    lat, lon = 52.5200, 13.4050
    origin = fs.to_point(lat, lon, BERLIN_CRS)
    shifted = poi_gdf([
        (Point(origin.x, origin.y), "Volkspark"),
        (building(origin.x, origin.y, half=50.0), "Volkspark"),
    ])

    _, points_before = fs.green_area_and_points(lat, lon, shifted, BERLIN_CRS, dist=500)
    area_after, points_after = fs.green_area_and_points(
        lat, lon, fs.dedupe_features(shifted, keep="areas"), BERLIN_CRS, dist=500
    )
    assert points_before == 1
    assert points_after == 0
    assert area_after == pytest.approx(park.area)


def test_dedupe_lowers_the_count_that_scoring_actually_uses():
    """End-to-end on count_nearby, the function the inflated numbers fed."""
    lat, lon = 52.5200, 13.4050
    origin = fs.to_point(lat, lon, BERLIN_CRS)
    gdf = poi_gdf([
        (Point(origin.x, origin.y), "Rewe"),
        (building(origin.x, origin.y), "Rewe"),
        (Point(origin.x + 100, origin.y), "Edeka"),
    ])
    assert fs.count_nearby(lat, lon, gdf, BERLIN_CRS, dist=500) == 3
    deduped = fs.dedupe_features(gdf)
    assert fs.count_nearby(lat, lon, deduped, BERLIN_CRS, dist=500) == 2


def test_a_zero_tolerance_still_merges_a_node_inside_its_building():
    gdf = poi_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "Rewe"),
        (building(ORIGIN_X, ORIGIN_Y), "Rewe"),
    ])
    assert len(fs.dedupe_features(gdf, tolerance_m=0.0)) == 1


def test_layers_with_only_one_kind_of_geometry_are_untouched():
    points_only = poi_gdf([(Point(ORIGIN_X, ORIGIN_Y), "A"), (Point(ORIGIN_X + 1, ORIGIN_Y), "B")])
    areas_only = poi_gdf([(building(ORIGIN_X, ORIGIN_Y), "A"), (building(ORIGIN_X + 1, ORIGIN_Y), "B")])
    assert len(fs.dedupe_features(points_only)) == 2
    assert len(fs.dedupe_features(areas_only)) == 2


def test_dedupe_handles_empty_and_missing_layers():
    assert fs.dedupe_features(None) is None
    empty = gpd.GeoDataFrame(geometry=[], crs=BERLIN_CRS)
    assert len(fs.dedupe_features(empty)) == 0


def test_dedupe_survives_a_layer_with_no_name_column():
    """bus + tram are concatenated; a layer can arrive with no `name` at all."""
    gdf = gpd.GeoDataFrame(
        geometry=[Point(ORIGIN_X, ORIGIN_Y), building(ORIGIN_X, ORIGIN_Y)],
        crs=BERLIN_CRS,
    )
    assert len(fs.dedupe_features(gdf)) == 1


def test_dedupe_does_not_confuse_repeated_index_labels():
    """pd.concat of bus + tram can repeat index labels; dropping must be positional."""
    left = poi_gdf([(Point(ORIGIN_X, ORIGIN_Y), "Stop"), (building(ORIGIN_X, ORIGIN_Y), "Stop")])
    right = poi_gdf([(Point(ORIGIN_X + 500, ORIGIN_Y), "Far stop")])
    combined = pd.concat([left, right])
    assert list(combined.index) == [0, 1, 0], "test needs a repeated label to be meaningful"

    deduped = fs.dedupe_features(combined)
    assert len(deduped) == 2
    assert sorted(deduped["name"]) == ["Far stop", "Stop"]


def test_poi_dedupe_tolerance_is_validated():
    config = valid_config(parameters={"poi_dedupe_tolerance_m": -1})
    assert "poi_dedupe_tolerance_m" in only_problem(config)


def test_a_zero_poi_dedupe_tolerance_is_allowed():
    """Unlike the normalization anchors, 0 is meaningful here."""
    assert fs.validate_config(valid_config(parameters={"poi_dedupe_tolerance_m": 0})) == []


# --- identifying ourselves to OSM services ----------------------------------
#
# Nominatim's policy: "Provide a valid HTTP Referer or User-Agent identifying
# the application (stock User-Agents as set by http libraries will not do)."
# These bite because the obligation was silently unmet for the whole osmnx 2.x
# pin - see `osm._apply_setting`.


def test_flatscorer_identifies_itself_not_osmnx():
    """The settings are applied at import of `flatscorer.osm`, which conftest did."""
    from flatscorer import osm

    assert "FlatScorer" in ox.settings.http_user_agent
    assert "github.com/Aduneer/FlatScorer" in ox.settings.http_user_agent
    # The exact failure this replaces: sending a library's own stock UA, which
    # the policy names as insufficient.
    assert "OSMnx" not in ox.settings.http_user_agent
    assert ox.settings.http_user_agent == osm.USER_AGENT


def test_the_referer_identifies_us_too():
    """osmnx sends a Referer of its own; claiming to be osmnx there is the same bug."""
    assert "FlatScorer" in ox.settings.http_referer
    assert "OSMnx" not in ox.settings.http_referer


def test_the_user_agent_carries_a_version():
    """A bare project name can't be told apart across releases when triaging load."""
    from flatscorer import __version__, osm

    assert __version__ in osm.USER_AGENT


def test_applying_an_unknown_osmnx_setting_raises():
    """The guard itself. Without it a renamed setting assigns to nothing at all.

    `ox.settings` is a plain module, so `setattr` always succeeds - which is how
    `ox.settings.useragent = ...` kept "working" after osmnx 2.x renamed it to
    `http_user_agent`, leaving every request identified as osmnx.
    """
    from flatscorer import osm

    with pytest.raises(AttributeError) as excinfo:
        osm._apply_setting("useragent", "FlatScorer")
    assert "useragent" in str(excinfo.value)
    # And nothing was written, so a typo can't half-apply.
    assert not hasattr(ox.settings, "useragent")


def test_every_setting_we_configure_exists_upstream():
    """Re-running the real configuration must not raise, on this osmnx version."""
    from flatscorer import osm

    osm._configure_osmnx()  # idempotent by design
    assert ox.settings.http_user_agent == osm.USER_AGENT


# --- Transit stops are not interchangeable -----------------------------------
#
# Every stop counted as 1 before this: a metro station and a once-hourly bus
# stop were the same feature to the score.

def transit_gdf(rows: list[tuple[Any, str, str, str | None]]) -> gpd.GeoDataFrame:
    """Build a projected transit layer from (geometry, tag, value, name) rows."""
    frame = {
        "highway": [value if tag == "highway" else None for _, tag, value, _ in rows],
        "railway": [value if tag == "railway" else None for _, tag, value, _ in rows],
        "name": [name for *_, name in rows],
    }
    return gpd.GeoDataFrame(
        frame, geometry=[geometry for geometry, *_ in rows], crs=BERLIN_CRS
    )


def test_transit_tags_cover_every_class_grouped_by_tag_key():
    tags = fs.osm.transit_tags()
    assert tags == {"railway": ["station", "halt", "tram_stop"], "highway": ["bus_stop"]}
    # Every declared layer is reachable from the query it generates.
    for layer in fs.osm.TRANSIT_LAYERS:
        assert layer.value in tags[layer.tag]


def test_a_station_outweighs_a_bus_stop():
    """The whole point: the weights are ordered by service level."""
    weights = {layer.value: layer.weight for layer in fs.osm.TRANSIT_LAYERS}
    assert weights["station"] > weights["halt"] > weights["tram_stop"] > weights["bus_stop"]


def test_a_bus_stop_is_exactly_one_equivalent():
    """Weights are in bus-stop equivalents, which is what keeps DEFAULT_SATURATION
    ['transit'] meaning what it was calibrated to mean. Rescaling this table
    silently re-tunes that anchor."""
    bus = next(layer for layer in fs.osm.TRANSIT_LAYERS if layer.value == "bus_stop")
    assert bus.weight == 1.0


def test_with_transit_weight_stamps_every_row():
    gdf = transit_gdf([(Point(ORIGIN_X, ORIGIN_Y), "highway", "bus_stop", None)])
    weighted = fs.osm.with_transit_weight(gdf, 1.0)
    assert list(weighted[fs.osm.TRANSIT_WEIGHT_COLUMN]) == [1.0]
    # The input is not mutated - the caller still holds an unweighted layer.
    assert fs.osm.TRANSIT_WEIGHT_COLUMN not in gdf.columns


def test_with_transit_weight_tolerates_an_empty_layer():
    empty = transit_gdf([]).iloc[0:0]
    assert fs.osm.with_transit_weight(empty, 3.0) is not None
    assert fs.osm.with_transit_weight(None, 3.0) is None


def test_a_bus_stop_node_inside_a_station_polygon_keeps_both_weights():
    """The trap this feature had to clear.

    `dedupe_features(keep="points")` discards the redundant *area*. Run over
    bus and station together, a station polygon sitting on a bus-stop node is
    the copy that gets dropped - silently turning a 3.0 feature into a 1.0 one.
    It was harmless while every stop counted as 1, which is why none of the
    existing dedupe tests would catch it.
    """
    bus_node = (Point(ORIGIN_X, ORIGIN_Y), "highway", "bus_stop", None)
    station_area = (building(ORIGIN_X, ORIGIN_Y), "railway", "station", "Hauptbahnhof")

    # What the old pipeline did: concatenate first, dedupe once.
    combined = fs.dedupe_features(transit_gdf([bus_node, station_area]))
    assert len(combined) == 1
    assert list(combined.geometry.geom_type) == ["Point"]  # the station is gone

    # What it does now: dedupe each class on its own, then weight, then combine.
    bus_layer = fs.osm.with_transit_weight(fs.dedupe_features(transit_gdf([bus_node])), 1.0)
    station_layer = fs.osm.with_transit_weight(fs.dedupe_features(transit_gdf([station_area])), 3.0)
    together = pd.concat([bus_layer, station_layer])
    assert len(together) == 2
    assert together[fs.osm.TRANSIT_WEIGHT_COLUMN].sum() == 4.0


def test_a_station_mapped_as_both_node_and_building_still_counts_once():
    """Per-class dedupe must not lose the within-class duplicate it was built for."""
    gdf = transit_gdf([
        (Point(ORIGIN_X, ORIGIN_Y), "railway", "station", "Hauptbahnhof"),
        (building(ORIGIN_X, ORIGIN_Y), "railway", "station", "Hauptbahnhof"),
    ])
    deduped = fs.dedupe_features(gdf)
    assert len(deduped) == 1
    weighted = fs.osm.with_transit_weight(deduped, 3.0)
    assert weighted[fs.osm.TRANSIT_WEIGHT_COLUMN].sum() == 3.0


# --- A dead mirror must cost seconds, not minutes ----------------------------

class FakeResponse:
    ok = True


def test_a_probe_that_gets_any_response_counts_as_reachable(monkeypatch):
    """404 or 504 still means the host is there, which is the only question."""
    calls = {}

    def fake_get(url, **kwargs):
        calls["url"] = url
        calls["timeout"] = kwargs.get("timeout")
        return FakeResponse()

    monkeypatch.setattr(fs.osm.requests, "get", fake_get)
    assert REAL_MIRROR_PROBE("https://overpass-api.de/api/interpreter") is True
    # Probed the instance's status endpoint, not the interpreter.
    assert calls["url"] == "https://overpass-api.de/api/status"
    # A (connect, read) pair - the shape osmnx's own timeout setting cannot take.
    assert isinstance(calls["timeout"], tuple) and len(calls["timeout"]) == 2


def test_a_probe_that_cannot_connect_counts_as_dead(monkeypatch):
    def refuse(url, **kwargs):
        raise requests.exceptions.ConnectionError("no route to host")

    monkeypatch.setattr(fs.osm.requests, "get", refuse)
    assert REAL_MIRROR_PROBE("https://mirror-a/api") is False


def test_an_unreachable_mirror_is_skipped_without_being_queried(monkeypatch, no_sleep):
    """The hang this fixes: osmnx spends requests_timeout (180 s by default) on
    every attempt, so two dead mirrors at two attempts each is ~12 minutes of
    silence. A skipped mirror must not be queried at all."""
    queried = []
    monkeypatch.setattr(fs.osm, "_mirror_is_reachable", lambda mirror, **kw: mirror != MIRRORS[0])

    def fn():
        queried.append(ox.settings.overpass_url)
        return "data"

    assert fs.query_with_retry(fn, mirrors=MIRRORS) == "data"
    assert queried == [MIRRORS[1]]


def test_every_mirror_unreachable_raises_rather_than_hanging(monkeypatch, no_sleep):
    monkeypatch.setattr(fs.osm, "_mirror_is_reachable", lambda mirror, **kw: False)

    def fn():
        raise AssertionError("an unreachable mirror must never be queried")

    with pytest.raises(RuntimeError, match="All Overpass mirrors failed"):
        fs.query_with_retry(fn, mirrors=MIRRORS)


def test_the_mirror_setting_is_restored_after_every_mirror_is_skipped(monkeypatch, no_sleep, pinned_mirror):
    """The skip path is a `continue`, so it has to honour the same restore
    guarantee the failure path does."""
    monkeypatch.setattr(fs.osm, "_mirror_is_reachable", lambda mirror, **kw: False)
    with pytest.raises(RuntimeError):
        fs.query_with_retry(lambda: "data", mirrors=MIRRORS)
    assert ox.settings.overpass_url == pinned_mirror
