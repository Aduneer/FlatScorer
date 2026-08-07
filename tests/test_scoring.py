"""Unit tests for FlatScorer's pure scoring and geometry helpers.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import re

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

import FlatScorer as fs

WEIGHTS = {
    "supermarket": 1.0,
    "bakery": 2.0,
    "pharmacy": 3.0,
    "gym": 4.0,
    "transit": 5.0,
    "green": 6.0,
    "noise": 7.0,
    "rent": 8.0,
}

METRICS = {
    "supermarket_count": 2,
    "bakery_count": 1,
    "pharmacy_count": 3,
    "gym_count": 1,
    "transit_count": 4,
    "green_score": 10.0,
    "noise_benefit": 5.0,
    "rent_time_equiv": 60.0,
    "destinations_min": {},
}


def make_scorer(**config) -> fs.FlatScorer:
    """A scorer with no candidates — enough to exercise compute_score/sensitivity."""
    base = {"candidates": [], "destinations": {}, "weights": WEIGHTS, "parameters": {}}
    base.update(config)
    return fs.FlatScorer(base, verbose=True)


# ------------------------------------------------------------- compute_score --

def test_compute_score_sums_weighted_metrics():
    scorer = make_scorer()
    expected = (
        1.0 * 2 + 2.0 * 1 + 3.0 * 3 + 4.0 * 1 + 5.0 * 4 + 6.0 * 10.0 + 7.0 * 5.0
        - 8.0 * 60.0
    )
    assert scorer.compute_score(METRICS, WEIGHTS) == pytest.approx(expected)


def test_compute_score_treats_missing_metrics_as_zero():
    scorer = make_scorer()
    assert scorer.compute_score({}, WEIGHTS) == pytest.approx(0.0)


def test_rent_is_subtracted_not_added():
    scorer = make_scorer()
    cheap = dict(METRICS, rent_time_equiv=10.0)
    pricey = dict(METRICS, rent_time_equiv=100.0)
    assert scorer.compute_score(cheap, WEIGHTS) > scorer.compute_score(pricey, WEIGHTS)


def test_commute_time_is_subtracted_using_destination_config_weight():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    m = dict(METRICS, destinations_min={"Work": 20.0})
    baseline = scorer.compute_score(METRICS, WEIGHTS)
    assert scorer.compute_score(m, WEIGHTS) == pytest.approx(baseline - 0.5 * 20.0)


def test_explicit_dest_weight_overrides_destination_config():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    m = dict(METRICS, destinations_min={"Work": 20.0})
    weights = dict(WEIGHTS, dest_Work=2.0)
    baseline = scorer.compute_score(METRICS, weights)
    assert scorer.compute_score(m, weights) == pytest.approx(baseline - 2.0 * 20.0)


def test_unconfigured_destination_falls_back_to_default_weight():
    scorer = make_scorer()  # no destinations configured at all
    m = dict(METRICS, destinations_min={"Ghost": 10.0})
    baseline = scorer.compute_score(METRICS, WEIGHTS)
    assert scorer.compute_score(m, WEIGHTS) == pytest.approx(baseline - 0.15 * 10.0)


# ------------------------------------------------------- rent_time_equivalent --

def test_rent_time_equivalent_converts_euros_to_minutes():
    assert fs.rent_time_equivalent(1200, 20) == pytest.approx(60.0)


@pytest.mark.parametrize("euros_per_minute", [0, -5])
def test_rent_time_equivalent_guards_against_zero_or_negative_rate(euros_per_minute):
    assert fs.rent_time_equivalent(1200, euros_per_minute) == 0.0


# ------------------------------------------------------------ _winner_margin --

def test_winner_margin_is_none_below_two_candidates():
    assert fs._winner_margin({}) is None
    assert fs._winner_margin({"only": 12.0}) is None


def test_winner_margin_uses_top_two_regardless_of_dict_order():
    assert fs._winner_margin({"a": 1.0, "b": 9.0, "c": 5.0}) == pytest.approx(4.0)


def test_winner_margin_is_zero_for_an_exact_tie():
    assert fs._winner_margin({"a": 3.0, "b": 3.0}) == pytest.approx(0.0)


# ------------------------------------------------------ run_sensitivity_check --

def sensitivity_metrics(gap: float) -> dict[str, dict]:
    return {
        "Winner": dict(METRICS, supermarket_count=10),
        "Runner-up": dict(METRICS, supermarket_count=10 - gap),
    }


def test_sensitivity_check_reports_baseline_margin_and_stability(capsys):
    scorer = make_scorer()
    scorer.run_sensitivity_check(sensitivity_metrics(gap=5))
    out = capsys.readouterr().out
    assert "baseline winner: Winner" in out
    assert "Baseline margin over runner-up: 5.00 points" in out
    assert "Ranking is stable" in out
    assert "winner changes!" not in out


def test_sensitivity_check_flags_a_photo_finish(capsys):
    scorer = make_scorer()
    scorer.run_sensitivity_check(sensitivity_metrics(gap=0.2))
    out = capsys.readouterr().out
    assert "Baseline margin over runner-up: 0.20 points" in out
    # Shrinking the supermarket weight by 20% narrows the only term that
    # separates these two, so the reported narrowest gap is 0.2 * 0.8.
    assert "Narrowest winner/runner-up gap seen: 0.16 points" in out
    assert "effectively tied" in out


def test_sensitivity_check_detects_a_winner_flip(capsys):
    # Same score overall, but earned differently: one flat wins on supermarkets,
    # the other on transit, so nudging either weight swaps the top pick.
    scorer = make_scorer()
    metrics = {
        "Groceries": dict(METRICS, supermarket_count=20, transit_count=0),
        "Transit": dict(METRICS, supermarket_count=0, transit_count=4),
    }
    scorer.run_sensitivity_check(metrics)
    out = capsys.readouterr().out
    assert "winner changes!" in out
    assert "flip the top pick" in out


def test_sensitivity_check_on_empty_metrics_is_a_no_op(capsys):
    make_scorer().run_sensitivity_check({})
    assert capsys.readouterr().out == ""


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


# ---------------------------------------------------------------- resolve_crs --

def test_resolve_crs_honours_an_explicit_override():
    scorer = make_scorer(parameters={"projected_crs": "EPSG:25832"})
    assert scorer.resolve_crs([52.5], [13.4]) == "EPSG:25832"


def test_resolve_crs_auto_detects_the_utm_zone():
    scorer = make_scorer(parameters={"projected_crs": "auto"})
    # Berlin sits in UTM zone 33N -> EPSG:32633.
    assert scorer.resolve_crs([52.52, 52.50], [13.40, 13.38]).endswith("32633")


# ------------------------------------------------------- spatial measurements --

BERLIN_CRS = "EPSG:32633"


def test_count_nearby_counts_only_features_inside_the_buffer():
    near = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    far = fs.to_point(52.5600, 13.4050, BERLIN_CRS)  # ~4.4 km north
    gdf = gpd.GeoDataFrame(geometry=[near, far], crs=BERLIN_CRS)
    assert fs.count_nearby(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500) == 1


def test_count_nearby_on_empty_input_is_zero():
    assert fs.count_nearby(52.52, 13.405, None, BERLIN_CRS) == 0
    assert fs.count_nearby(52.52, 13.405, gpd.GeoDataFrame(geometry=[]), BERLIN_CRS) == 0


def test_nearest_distance_m_measures_in_meters():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    road = LineString([(origin.x + 100, origin.y - 500), (origin.x + 100, origin.y + 500)])
    gdf = gpd.GeoDataFrame(geometry=[road], crs=BERLIN_CRS)
    assert fs.nearest_distance_m(52.5200, 13.4050, gdf, BERLIN_CRS) == pytest.approx(100, abs=1)


def test_nearest_distance_m_is_none_without_features():
    assert fs.nearest_distance_m(52.52, 13.405, gpd.GeoDataFrame(geometry=[]), BERLIN_CRS) is None


def test_green_area_and_points_separates_polygons_from_points():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    # A 100x100 m park fully inside the buffer, plus one point in and one out.
    park = Polygon([
        (origin.x, origin.y), (origin.x + 100, origin.y),
        (origin.x + 100, origin.y + 100), (origin.x, origin.y + 100),
    ])
    inside = Point(origin.x + 50, origin.y + 50)
    outside = Point(origin.x + 5000, origin.y)
    gdf = gpd.GeoDataFrame(geometry=[park, inside, outside], crs=BERLIN_CRS)

    area, count = fs.green_area_and_points(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500)
    assert area == pytest.approx(10_000, rel=0.01)
    assert count == 1


def test_green_area_and_points_clips_polygons_to_the_buffer():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    # A huge forest - only the part within the 500 m buffer should count.
    forest = Polygon([
        (origin.x - 10_000, origin.y - 10_000), (origin.x + 10_000, origin.y - 10_000),
        (origin.x + 10_000, origin.y + 10_000), (origin.x - 10_000, origin.y + 10_000),
    ])
    gdf = gpd.GeoDataFrame(geometry=[forest], crs=BERLIN_CRS)
    area, count = fs.green_area_and_points(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500)
    assert area == pytest.approx(3.14159 * 500 ** 2, rel=0.01)
    assert count == 0


def test_green_area_and_points_on_empty_input():
    assert fs.green_area_and_points(52.52, 13.405, None, BERLIN_CRS) == (0.0, 0)


# ------------------------------------------------ straight-line walk fallback --

def test_straight_line_distance_in_projected_crs_is_metric():
    # 1 degree of longitude at 52 deg N is ~68.5 km, not 111 km.
    dist = fs.straight_line_distance_m((52.0, 13.0), (52.0, 14.0), BERLIN_CRS)
    assert dist == pytest.approx(68_500, rel=0.02)


def test_degree_fallback_overstates_east_west_distance():
    # Documents the bug the CRS argument exists to avoid: without a CRS the
    # east-west leg is inflated by ~60% at this latitude, which silently
    # penalises any candidate that hits the no-path fallback.
    naive = fs.straight_line_distance_m((52.0, 13.0), (52.0, 14.0))
    projected = fs.straight_line_distance_m((52.0, 13.0), (52.0, 14.0), BERLIN_CRS)
    assert naive == pytest.approx(111_000, rel=0.01)
    assert naive > projected * 1.5


def test_straight_line_distance_agrees_north_south():
    # Latitude degrees really are ~111 km, so both paths should roughly agree.
    naive = fs.straight_line_distance_m((52.0, 13.4), (53.0, 13.4))
    projected = fs.straight_line_distance_m((52.0, 13.4), (53.0, 13.4), BERLIN_CRS)
    assert projected == pytest.approx(naive, rel=0.01)


def disconnected_graph() -> nx.MultiDiGraph:
    """Two isolated nodes 1 degree of longitude apart at 52 deg N."""
    G = nx.MultiDiGraph(crs="EPSG:4326")
    G.add_node(1, x=13.0, y=52.0)
    G.add_node(2, x=14.0, y=52.0)
    return G


def test_walk_route_fallback_uses_the_projected_crs_when_given():
    minutes, route = fs.walk_route(disconnected_graph(), (52.0, 13.0), (52.0, 14.0),
                                   projected_crs=BERLIN_CRS)
    assert minutes == pytest.approx(68_500 / 83.33, rel=0.02)
    assert route == [(52.0, 13.0), (52.0, 14.0)]


def test_walk_route_fallback_without_a_crs_keeps_the_legacy_estimate():
    minutes, _ = fs.walk_route(disconnected_graph(), (52.0, 13.0), (52.0, 14.0))
    assert minutes == pytest.approx(111_000 / 83.33, rel=0.01)


def test_walk_route_uses_the_network_when_a_path_exists():
    G = nx.MultiDiGraph(crs="EPSG:4326")
    G.add_node(1, x=13.0, y=52.0)
    G.add_node(2, x=13.01, y=52.0)
    G.add_edge(1, 2, length=1000.0)
    minutes, route = fs.walk_route(G, (52.0, 13.0), (52.0, 13.01), projected_crs=BERLIN_CRS)
    assert minutes == pytest.approx(1000.0 / 83.33, rel=1e-6)
    assert route == [(52.0, 13.0), (52.0, 13.01)]


# ----------------------------------------------------------- query_with_retry --

MIRRORS = ["https://mirror-a/api", "https://mirror-b/api"]
CONFIGURED_MIRROR = "https://configured-by-the-user/api"


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(fs.time, "sleep", lambda _s: None)


@pytest.fixture
def pinned_mirror(monkeypatch):
    """Pin overpass_url to a known sentinel so leak assertions can't pass by accident."""
    monkeypatch.setattr(fs.ox.settings, "overpass_url", CONFIGURED_MIRROR)
    return CONFIGURED_MIRROR


def test_query_with_retry_returns_the_first_success(no_sleep):
    assert fs.query_with_retry(lambda: "data", mirrors=MIRRORS) == "data"


def test_query_with_retry_falls_through_to_the_next_mirror(no_sleep):
    seen = []

    def fails_on_mirror_a():
        seen.append(fs.ox.settings.overpass_url)
        if fs.ox.settings.overpass_url == MIRRORS[0]:
            raise TimeoutError("down")
        return "data"

    assert fs.query_with_retry(fails_on_mirror_a, mirrors=MIRRORS, retries_per_mirror=2) == "data"
    assert seen == [MIRRORS[0], MIRRORS[0], MIRRORS[1]]


def test_query_with_retry_restores_the_mirror_setting_after_success(no_sleep, pinned_mirror):
    # ox.settings.overpass_url is process-wide; the Streamlit app runs many
    # queries in one process, so a leaked mirror would persist across runs.
    fs.query_with_retry(lambda: "data", mirrors=MIRRORS)
    assert fs.ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_restores_the_mirror_setting_after_partial_failure(no_sleep, pinned_mirror):
    def fails_on_mirror_a():
        if fs.ox.settings.overpass_url == MIRRORS[0]:
            raise TimeoutError("down")
        return "data"

    fs.query_with_retry(fails_on_mirror_a, mirrors=MIRRORS, retries_per_mirror=1)
    assert fs.ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_restores_the_mirror_setting_when_all_mirrors_fail(no_sleep, pinned_mirror):
    def always_fails():
        raise TimeoutError("down")

    with pytest.raises(RuntimeError, match="All Overpass mirrors failed"):
        fs.query_with_retry(always_fails, mirrors=MIRRORS, retries_per_mirror=1)
    assert fs.ox.settings.overpass_url == pinned_mirror


def test_query_with_retry_does_not_swallow_unexpected_errors(no_sleep):
    # Only network-shaped failures are worth retrying; a bug in the callable
    # should surface immediately rather than being retried six times.
    def broken():
        raise ValueError("bad tags")

    with pytest.raises(ValueError, match="bad tags"):
        fs.query_with_retry(broken, mirrors=MIRRORS)


# ------------------------------------------------------------- map pin colours --

def map_frame(scores: list[float]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "name": f"Flat {i}", "score": s, "rent_eur": 1000, "supermarkets": 1,
            "bakeries": 1, "pharmacies": 1, "gyms": 1, "transit_stops": 1,
            "green_area_m2": 100, "dist_busy_road_m": 200,
            "lat": 52.52 + i * 0.001, "lon": 13.40,
        }
        for i, s in enumerate(scores)
    ]).sort_values("score", ascending=False)


def marker_colours(html: str) -> list[str]:
    return re.findall(r'markerColor": "([a-z]+)"', html)


def test_map_uses_the_full_colour_scale_when_scores_really_differ(tmp_path):
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([30.0, 20.0, 10.0]), {}, str(out))
    colours = marker_colours(out.read_text())
    assert set(colours) == {"green", "orange", "red"}


def test_map_drops_the_colour_scale_when_the_field_is_effectively_tied(tmp_path):
    # Min-max colouring would otherwise paint a 0.2-point spread red-to-green,
    # contradicting the sensitivity report calling the same gap a tie.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([20.1, 20.0, 19.9]), {}, str(out))
    html = out.read_text()
    assert set(marker_colours(html)) == {"cadetblue"}
    assert "pin colour carries no ranking information" in html


def test_map_tie_notice_is_logged(tmp_path, capsys):
    make_scorer().generate_map(map_frame([20.1, 20.0]), {}, str(tmp_path / "map.html"))
    assert "one neutral colour" in capsys.readouterr().out


def test_map_with_a_single_candidate_keeps_its_colour(tmp_path):
    # One candidate has no spread at all, but there is no tie to warn about.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([20.0]), {}, str(out))
    assert marker_colours(out.read_text()) == ["red"]


# ------------------------------------------------------- geocode rate limiting --

def test_geocode_safe_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(fs.time, "sleep", lambda _s: None)
    calls = []

    def flaky(address):
        calls.append(address)
        if len(calls) < 3:
            raise TimeoutError("boom")
        return (52.5, 13.4)

    monkeypatch.setattr(fs.ox, "geocode", flaky)
    assert fs.geocode_safe("Somewhere", "Flat A") == (52.5, 13.4)
    assert len(calls) == 3


def test_geocode_safe_gives_up_after_the_attempt_budget(monkeypatch):
    monkeypatch.setattr(fs.time, "sleep", lambda _s: None)
    calls = []

    def always_fails(address):
        calls.append(address)
        raise TimeoutError("boom")

    monkeypatch.setattr(fs.ox, "geocode", always_fails)
    assert fs.geocode_safe("Somewhere", "Flat A") is None
    assert len(calls) == fs.GEOCODE_ATTEMPTS


def test_geocode_safe_does_not_retry_an_address_nominatim_cannot_match(monkeypatch):
    monkeypatch.setattr(fs.time, "sleep", lambda _s: None)
    calls = []

    def not_found(address):
        calls.append(address)
        raise fs.InsufficientResponseError("no match")

    monkeypatch.setattr(fs.ox, "geocode", not_found)
    assert fs.geocode_safe("Nowhere at all", "Flat B") is None
    assert len(calls) == 1, "a definitive 'no match' is not worth retrying"


def test_geocode_calls_are_spaced_to_respect_the_nominatim_policy(monkeypatch):
    """Nominatim allows 1 req/sec; the throttle must sleep for the shortfall."""
    clock = {"now": 100.0}
    slept = []

    monkeypatch.setattr(fs, "_last_geocode_at", 0.0)
    monkeypatch.setattr(fs.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(fs.time, "sleep", slept.append)
    monkeypatch.setattr(fs.ox, "geocode", lambda address: (52.5, 13.4))

    fs.geocode_safe("First", "A")     # long idle - no wait needed
    fs.geocode_safe("Second", "B")    # immediately after - must wait a full second
    clock["now"] += 0.25
    fs.geocode_safe("Third", "C")     # 0.25 s later - waits the remaining 0.75 s

    assert slept[0] == pytest.approx(fs.NOMINATIM_MIN_INTERVAL_S)
    assert slept[1] == pytest.approx(0.75)
