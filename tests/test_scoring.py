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
    "noise_distance_m": 150.0,
    "rent_eur": 1200.0,
    "destinations_min": {},
}


def make_scorer(**config) -> fs.FlatScorer:
    """A scorer with no candidates — enough to exercise compute_score/sensitivity."""
    base = {"candidates": [], "destinations": {}, "weights": WEIGHTS, "parameters": {}}
    base.update(config)
    return fs.FlatScorer(base, verbose=True)


# ------------------------------------------------------------- normalization --

def test_benefit_fraction_awards_half_credit_at_the_half_value():
    assert fs.benefit_fraction(2, 2) == pytest.approx(0.5)
    assert fs.benefit_fraction(0, 2) == pytest.approx(0.0)


def test_benefit_fraction_has_diminishing_returns():
    # The whole point of the curve: the 6th supermarket is worth far less than
    # the 2nd, which a raw count could never express.
    second = fs.benefit_fraction(2, 2) - fs.benefit_fraction(1, 2)
    sixth = fs.benefit_fraction(6, 2) - fs.benefit_fraction(5, 2)
    assert second > sixth * 4


def test_benefit_fraction_is_bounded_and_never_reaches_one():
    assert fs.benefit_fraction(10_000, 2) < 1.0
    assert fs.benefit_fraction(-5, 2) == 0.0


def test_benefit_fraction_with_a_non_positive_half_value_is_all_or_nothing():
    assert fs.benefit_fraction(1, 0) == 1.0
    assert fs.benefit_fraction(0, 0) == 0.0


def test_cost_credit_runs_from_full_at_zero_to_none_at_the_cap():
    assert fs.cost_credit(0, 45) == pytest.approx(1.0)
    assert fs.cost_credit(45, 45) == pytest.approx(0.0)
    assert fs.cost_credit(90, 45) == pytest.approx(0.0)  # clamped, never negative
    assert fs.cost_credit(15, 45) == pytest.approx(2 / 3)


def test_capped_fraction_ramps_to_one_at_the_cap():
    assert fs.capped_fraction(100, 200) == pytest.approx(0.5)
    assert fs.capped_fraction(500, 200) == pytest.approx(1.0)
    assert fs.capped_fraction(100, 0) == 0.0


def test_weight_shares_normalize_to_one():
    shares = fs.weight_shares({"a": 1.0, "b": 3.0})
    assert shares == {"a": pytest.approx(0.25), "b": pytest.approx(0.75)}


def test_weight_shares_clamp_negatives_and_tolerate_an_all_zero_vector():
    assert fs.weight_shares({"a": -1.0, "b": 1.0}) == {"a": 0.0, "b": 1.0}
    assert fs.weight_shares({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}


def test_noise_normalization_is_decoupled_from_the_cap():
    # The old `min(dist, cap) / 20.0` meant raising noise_cap_m silently
    # multiplied the noise term's influence; dividing by the cap fixes that.
    at_cap_200 = make_scorer(parameters={"noise_cap_m": 200}).normalize_metrics(
        dict(METRICS, noise_distance_m=200)
    )["noise"]
    at_cap_500 = make_scorer(parameters={"noise_cap_m": 500}).normalize_metrics(
        dict(METRICS, noise_distance_m=500)
    )["noise"]
    assert at_cap_200 == pytest.approx(1.0)
    assert at_cap_500 == pytest.approx(1.0)


# ------------------------------------------------------------- compute_score --

def test_compute_score_is_a_weighted_average_of_normalized_metrics():
    scorer = make_scorer()
    norm = scorer.normalize_metrics(METRICS)
    total_weight = sum(WEIGHTS.values())
    expected = fs.SCORE_SCALE_MAX * sum(
        WEIGHTS[key] * norm[key] for key in WEIGHTS
    ) / total_weight
    assert scorer.compute_score(METRICS, WEIGHTS) == pytest.approx(expected)


@pytest.mark.parametrize("metrics", [
    {},
    METRICS,
    dict(METRICS, supermarket_count=10_000, transit_count=10_000, green_score=1e9,
         noise_distance_m=1e6, rent_eur=0, destinations_min={"Work": 0.0}),
    dict(METRICS, supermarket_count=0, bakery_count=0, pharmacy_count=0, gym_count=0,
         transit_count=0, green_score=0, noise_distance_m=0, rent_eur=99_999,
         destinations_min={"Work": 500.0}),
])
def test_scores_are_bounded_to_the_scale(metrics):
    # The headline promise of normalization: no input can push the score outside
    # 0..10, so "Score X / 10" in the GUI is finally an honest claim.
    score = make_scorer().compute_score(metrics, WEIGHTS)
    assert 0.0 <= score <= fs.SCORE_SCALE_MAX


def test_only_relative_weights_matter():
    # A weighted *average* is scale-invariant, so doubling every slider is a no-op.
    scorer = make_scorer()
    doubled = {k: v * 2 for k, v in WEIGHTS.items()}
    assert scorer.compute_score(METRICS, doubled) == pytest.approx(
        scorer.compute_score(METRICS, WEIGHTS)
    )


def test_an_all_zero_weight_vector_scores_zero_instead_of_dividing_by_zero():
    zeroed = dict.fromkeys(WEIGHTS, 0.0)
    assert make_scorer().compute_score(METRICS, zeroed) == pytest.approx(0.0)


def test_negative_weights_are_clamped_rather_than_breaking_the_bounds():
    scorer = make_scorer()
    sneaky = dict(WEIGHTS, rent=-100.0)
    assert 0.0 <= scorer.compute_score(METRICS, sneaky) <= fs.SCORE_SCALE_MAX


def test_a_metric_no_longer_dominates_purely_by_having_large_units():
    # The bug this branch exists to fix: with raw units, 12 supermarkets
    # out-contributed the entire rent term regardless of the weights. Rent is
    # weighted 8x supermarkets here, so it must dominate.
    scorer = make_scorer()
    many_shops_pricey = dict(METRICS, supermarket_count=50, rent_eur=2500)
    no_shops_cheap = dict(METRICS, supermarket_count=0, rent_eur=0)
    assert scorer.compute_score(no_shops_cheap, WEIGHTS) > scorer.compute_score(many_shops_pricey, WEIGHTS)


def test_cheaper_rent_scores_higher():
    scorer = make_scorer()
    cheap = dict(METRICS, rent_eur=500)
    pricey = dict(METRICS, rent_eur=2000)
    assert scorer.compute_score(cheap, WEIGHTS) > scorer.compute_score(pricey, WEIGHTS)


def test_a_longer_commute_scores_lower():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    near = dict(METRICS, destinations_min={"Work": 5.0})
    far = dict(METRICS, destinations_min={"Work": 40.0})
    assert scorer.compute_score(near, WEIGHTS) > scorer.compute_score(far, WEIGHTS)


def test_commute_uses_the_destination_config_weight():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    m = dict(METRICS, destinations_min={"Work": 20.0})
    breakdown = scorer.score_breakdown(m, WEIGHTS)
    assert breakdown["dest_Work"]["weight"] == pytest.approx(0.5)
    assert breakdown["dest_Work"]["normalized"] == pytest.approx(fs.cost_credit(20.0, 45.0))


def test_explicit_dest_weight_overrides_destination_config():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    m = dict(METRICS, destinations_min={"Work": 20.0})
    weights = dict(WEIGHTS, dest_Work=2.0)
    assert scorer.score_breakdown(m, weights)["dest_Work"]["weight"] == pytest.approx(2.0)


def test_unconfigured_destination_falls_back_to_the_default_weight():
    scorer = make_scorer()  # no destinations configured at all
    m = dict(METRICS, destinations_min={"Ghost": 10.0})
    breakdown = scorer.score_breakdown(m, WEIGHTS)
    assert breakdown["dest_Ghost"]["weight"] == pytest.approx(fs.DEFAULT_DEST_WEIGHT)


def test_a_weight_absent_from_the_vector_falls_back_to_the_default():
    scorer = make_scorer()
    partial = {k: v for k, v in WEIGHTS.items() if k != "transit"}
    assert scorer.score_breakdown(METRICS, partial)["transit"]["weight"] == pytest.approx(
        fs.DEFAULT_WEIGHTS["transit"]
    )


# ----------------------------------------------------------- score_breakdown --

def test_breakdown_contributions_sum_to_the_score():
    scorer = make_scorer(destinations={"Work": {"address": "x", "weight": 0.5}})
    m = dict(METRICS, destinations_min={"Work": 20.0})
    breakdown = scorer.score_breakdown(m, WEIGHTS)
    assert sum(t["contribution"] for t in breakdown.values()) == pytest.approx(
        scorer.compute_score(m, WEIGHTS)
    )


def test_breakdown_shares_sum_to_one():
    breakdown = make_scorer().score_breakdown(METRICS, WEIGHTS)
    assert sum(t["share"] for t in breakdown.values()) == pytest.approx(1.0)


def test_saturation_half_values_are_configurable():
    lenient = make_scorer(parameters={"saturation": {"supermarket": 1.0}})
    strict = make_scorer(parameters={"saturation": {"supermarket": 10.0}})
    m = dict(METRICS, supermarket_count=2)
    assert lenient.normalize_metrics(m)["supermarket"] > strict.normalize_metrics(m)["supermarket"]
    # Overriding one key must not drop the defaults for the others.
    assert lenient.saturation["transit"] == fs.DEFAULT_SATURATION["transit"]


def test_log_score_breakdown_reports_shares_and_contributions(capsys):
    scorer = make_scorer()
    scorer.log_score_breakdown({"Flat A": METRICS})
    out = capsys.readouterr().out
    assert "Score breakdown" in out
    assert "TOTAL" in out
    assert "Flat A" in out


def test_log_score_breakdown_on_empty_metrics_is_a_no_op(capsys):
    make_scorer().log_score_breakdown({})
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------ _winner_margin --

def test_winner_margin_is_none_below_two_candidates():
    assert fs._winner_margin({}) is None
    assert fs._winner_margin({"only": 12.0}) is None


def test_winner_margin_uses_top_two_regardless_of_dict_order():
    assert fs._winner_margin({"a": 1.0, "b": 9.0, "c": 5.0}) == pytest.approx(4.0)


def test_winner_margin_is_zero_for_an_exact_tie():
    assert fs._winner_margin({"a": 3.0, "b": 3.0}) == pytest.approx(0.0)


# ------------------------------------------------------ run_sensitivity_check --

def sensitivity_metrics(**winner_edge) -> dict[str, dict]:
    return {
        "Winner": dict(METRICS, **winner_edge),
        "Runner-up": dict(METRICS),
    }


def expected_margin(scorer: fs.FlatScorer, metrics: dict[str, dict]) -> float:
    scores = sorted((scorer.compute_score(m, scorer.weights) for m in metrics.values()), reverse=True)
    return scores[0] - scores[1]


def test_sensitivity_check_reports_baseline_margin_and_stability(capsys):
    scorer = make_scorer()
    # Transit carries ~14% of the weight, so a runaway lead on it is decisive.
    metrics = sensitivity_metrics(transit_count=400)
    margin = expected_margin(scorer, metrics)
    assert margin > fs.NARROW_MARGIN_THRESHOLD

    scorer.run_sensitivity_check(metrics)
    out = capsys.readouterr().out
    assert "baseline winner: Winner" in out
    assert f"Baseline margin over runner-up: {margin:.2f} points" in out
    assert "Ranking is stable" in out
    assert "winner changes!" not in out


def test_sensitivity_check_flags_a_photo_finish(capsys):
    scorer = make_scorer()
    # One extra supermarket, on the lowest-weighted term, past the point where
    # the saturating curve has mostly flattened - a genuinely marginal win.
    metrics = sensitivity_metrics(supermarket_count=3)
    margin = expected_margin(scorer, metrics)
    assert margin < fs.NARROW_MARGIN_THRESHOLD

    scorer.run_sensitivity_check(metrics)
    out = capsys.readouterr().out
    assert f"Baseline margin over runner-up: {margin:.2f} points" in out
    assert "Narrowest winner/runner-up gap seen:" in out
    assert "effectively tied" in out


def test_sensitivity_check_detects_a_winner_flip(capsys):
    # Effectively the same score, earned differently: one flat maxes out the
    # supermarket term, the other earns as much from a single transit stop on a
    # 5x heavier weight. Nudging either weight swaps the top pick.
    scorer = make_scorer()
    metrics = {
        "Groceries": dict(METRICS, supermarket_count=100_000, transit_count=0),
        "Transit": dict(METRICS, supermarket_count=0, transit_count=1),
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


@pytest.mark.parametrize("score,colour", [
    (9.5, "green"), (7.0, "green"), (6.0, "orange"), (4.0, "orange"),
    (3.0, "red"), (0.0, "red"),
])
def test_score_colour_uses_absolute_bands(score, colour):
    assert fs.score_colour(score) == colour


def test_map_colours_pins_by_absolute_score(tmp_path):
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([8.0, 5.0, 2.0]), {}, str(out))
    assert marker_colours(out.read_text()) == ["green", "orange", "red"]


def test_map_no_longer_stretches_a_narrow_field_across_the_whole_scale(tmp_path):
    # The old min-max colouring painted the worst candidate red and the best
    # green even for a 0.2-point spread, contradicting the sensitivity report
    # calling the same gap a tie. Alike scores must now look alike.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([5.1, 5.0, 4.9]), {}, str(out))
    assert set(marker_colours(out.read_text())) == {"orange"}


def test_map_notes_a_narrow_field_in_the_log(tmp_path, capsys):
    make_scorer().generate_map(map_frame([5.1, 5.0]), {}, str(tmp_path / "map.html"))
    out = capsys.readouterr().out
    assert "coloured by absolute score" in out
    assert "because they are alike" in out


def test_map_with_a_single_candidate_colours_it_on_the_same_absolute_scale(tmp_path):
    # No spread to normalize against - previously that made every lone candidate
    # red regardless of how good it was. A 9.0 is a green pin on its own.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([9.0]), {}, str(out))
    assert marker_colours(out.read_text()) == ["green"]


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


# ------------------------------------------------------------ config validation --

def valid_config(**overrides) -> dict:
    """A minimal config that validate_config accepts, for one-field-at-a-time breakage."""
    config = {
        "candidates": [{"name": "Flat A", "address": "1 Main St", "rent": 1800}],
        "destinations": {"Work": {"address": "2 Office Rd", "weight": 0.2}},
        "weights": dict(fs.DEFAULT_WEIGHTS),
        "parameters": {},
    }
    config.update(overrides)
    return config


def only_problem(config: dict) -> str:
    """Assert exactly one problem and return it, so a test can't pass on an unrelated error."""
    problems = fs.validate_config(config)
    assert len(problems) == 1, problems
    return problems[0]


def test_a_valid_config_has_no_problems():
    assert fs.validate_config(valid_config()) == []


def test_the_shipped_default_config_is_valid():
    """--generate-config writes this; if it doesn't validate, first-run is broken."""
    assert fs.validate_config(fs.DEFAULT_CONFIG) == []


def test_an_empty_weights_dict_is_valid_because_defaults_fill_in():
    # _resolve_weight falls back to DEFAULT_WEIGHTS, so omitting weights is fine.
    assert fs.validate_config(valid_config(weights={})) == []


# -- 1.2: missing/zero rent must not silently win --

def test_missing_rent_is_rejected_and_says_why_it_matters():
    candidate = {"name": "Flat A", "address": "1 Main St"}
    problem = only_problem(valid_config(candidates=[candidate]))
    assert "Flat A" in problem
    assert "'rent' is missing" in problem


def test_a_candidate_with_no_rent_would_otherwise_take_full_credit():
    """The bug this validation exists to prevent, pinned as behaviour."""
    scorer = make_scorer(parameters={"rent_budget_eur": 2000.0})
    assert scorer.normalize_metrics({"rent_eur": 0})["rent"] == pytest.approx(1.0)
    assert scorer.normalize_metrics({"rent_eur": 2000.0})["rent"] == pytest.approx(0.0)


@pytest.mark.parametrize("rent", [0, 0.0, -50])
def test_zero_or_negative_rent_is_rejected(rent):
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": rent}
    assert "'rent'" in only_problem(valid_config(candidates=[candidate]))


@pytest.mark.parametrize("rent", ["not a number", None, True, float("nan")])
def test_non_numeric_rent_is_rejected(rent):
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": rent}
    assert "'rent'" in only_problem(valid_config(candidates=[candidate]))


def test_rent_written_as_a_numeric_string_is_accepted():
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": "1800"}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []


# -- 5.1: report every problem at once, naming its owner --

def test_every_problem_is_reported_in_one_pass():
    config = valid_config(candidates=[
        {"name": "Flat A", "address": "1 Main St"},          # no rent
        {"name": "", "address": "2 Main St", "rent": 900},   # no name
        {"name": "Flat C", "rent": 0},                       # no address, bad rent
    ])
    problems = fs.validate_config(config)
    assert len(problems) == 4, problems
    joined = "\n".join(problems)
    assert "candidates[1]" in joined
    assert joined.count("Flat C") == 2


def test_problems_name_the_candidate_not_just_the_index():
    problem = only_problem(valid_config(
        candidates=[{"name": "Flat by the park", "address": "1 Main St", "rent": "free"}]
    ))
    assert "Flat by the park" in problem


def test_duplicate_candidate_names_are_rejected():
    """run() keys candidates by name, so a duplicate silently overwrites the first."""
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800}
    problem = only_problem(valid_config(candidates=[candidate, dict(candidate, rent=1900)]))
    assert "duplicate name" in problem


@pytest.mark.parametrize("candidates", [None, [], "Flat A", {}])
def test_a_config_with_no_usable_candidates_is_rejected(candidates):
    assert "candidates" in only_problem(valid_config(candidates=candidates))


def test_a_non_object_config_is_rejected_without_crashing():
    assert len(fs.validate_config(["not", "a", "config"])) == 1


def test_a_candidate_that_is_not_an_object_is_rejected_without_crashing():
    assert "candidates[0]" in only_problem(valid_config(candidates=["1 Main St"]))


def test_a_destination_without_an_address_is_rejected():
    problem = only_problem(valid_config(destinations={"Work": {"weight": 0.2}}))
    assert "Work" in problem and "'address'" in problem


def test_a_negative_destination_weight_is_rejected():
    config = valid_config(destinations={"Work": {"address": "2 Office Rd", "weight": -1}})
    assert "negative" in only_problem(config)


@pytest.mark.parametrize("weight", [-0.5, "heavy"])
def test_a_bad_metric_weight_is_rejected(weight):
    config = valid_config(weights=dict(fs.DEFAULT_WEIGHTS, rent=weight))
    assert "weights['rent']" in only_problem(config)


def test_all_zero_weights_are_rejected_because_every_candidate_would_score_zero():
    config = valid_config(
        weights=dict.fromkeys(fs.DEFAULT_WEIGHTS, 0.0),
        destinations={"Work": {"address": "2 Office Rd", "weight": 0}},
    )
    assert "nothing carries any weight" in only_problem(config)


def test_a_destination_weight_alone_is_enough_to_carry_the_score():
    config = valid_config(
        weights=dict.fromkeys(fs.DEFAULT_WEIGHTS, 0.0),
        destinations={"Work": {"address": "2 Office Rd", "weight": 0.5}},
    )
    assert fs.validate_config(config) == []


@pytest.mark.parametrize("key", ["buffer_m", "noise_cap_m", "rent_budget_eur", "commute_cap_min"])
def test_a_non_positive_normalization_anchor_is_rejected(key):
    assert f"parameters['{key}']" in only_problem(valid_config(parameters={key: 0}))


def test_a_non_positive_saturation_point_is_rejected():
    config = valid_config(parameters={"saturation": {"supermarket": 0}})
    assert "saturation" in only_problem(config)


# -- the engine refuses to start on a bad config --

def test_run_raises_config_error_before_touching_the_network(monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("geocoding started despite an invalid config")

    monkeypatch.setattr(fs.ox, "geocode", must_not_be_called)
    scorer = fs.FlatScorer(valid_config(
        candidates=[{"name": "Flat A", "address": "1 Main St"}]
    ), verbose=False)

    with pytest.raises(fs.ConfigError) as excinfo:
        scorer.run()
    assert "'rent' is missing" in str(excinfo.value)


def test_config_error_carries_and_renders_every_problem():
    error = fs.ConfigError(["first thing", "second thing"])
    assert error.problems == ["first thing", "second thing"]
    assert "first thing" in str(error) and "second thing" in str(error)


def test_config_error_is_a_value_error():
    """The GUI catches broad Exception; the CLI and tests rely on ValueError."""
    assert issubclass(fs.ConfigError, ValueError)
