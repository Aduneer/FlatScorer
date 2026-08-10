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
