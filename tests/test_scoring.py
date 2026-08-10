"""Scoring maths: normalization, weighting, breakdown, sensitivity.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import pytest
from conftest import (
    METRICS,
    WEIGHTS,
    make_scorer,
)

import flatscorer as fs

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
