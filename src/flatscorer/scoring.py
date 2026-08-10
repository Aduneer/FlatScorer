"""The scoring maths: normalization, weighting, and the per-term breakdown.

Read `normalize_metrics` -> `resolve_weight` -> `weight_shares` ->
`score_breakdown` -> `compute_score` together; each depends on the one before
and the ordering carries the design.

Everything here is a pure function of its arguments plus an `Anchors` bundle, so
the maths can be tested without constructing a `FlatScorer`. `FlatScorer` keeps
delegating methods for the same operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class Anchors:
    """The absolute anchors every metric is normalized against.

    Normalization is deliberately *not* min-max across the candidate set: every
    value here is a configured constant, so a score means the same thing in every
    run and doesn't shift when an unrelated candidate is added or removed.
    Bundling them keeps the scoring functions pure without passing five
    positional arguments through four call layers.
    """

    saturation: dict[str, float]
    noise_cap_m: float
    rent_budget_eur: float
    commute_cap_min: float
    destinations: dict[str, Any]


def normalize_metrics(m: dict[str, Any], anchors: Anchors) -> dict[str, float]:
    """Put every raw metric on a common 0..1 scale, keyed by its weight name.

    Raw metrics arrive in wildly different units - a supermarket count, m² of
    park, euros of rent, minutes of walking - so weighting them directly meant
    whichever term happened to have the largest magnitude dominated regardless
    of its weight. Normalizing first is what makes the weights mean what the
    GUI implies they mean.
    """
    norm = {
        "supermarket": benefit_fraction(m.get("supermarket_count", 0), anchors.saturation["supermarket"]),
        "bakery":      benefit_fraction(m.get("bakery_count", 0), anchors.saturation["bakery"]),
        "pharmacy":    benefit_fraction(m.get("pharmacy_count", 0), anchors.saturation["pharmacy"]),
        "gym":         benefit_fraction(m.get("gym_count", 0), anchors.saturation["gym"]),
        "transit":     benefit_fraction(m.get("transit_count", 0), anchors.saturation["transit"]),
        "green":       benefit_fraction(m.get("green_score", 0.0), anchors.saturation["green"]),
        # Further from a busy road is quieter, up to the cap beyond which extra
        # distance buys nothing. Dividing by the cap (rather than a magic 20.0)
        # is what stops raising the cap from silently inflating noise's weight.
        "noise":       capped_fraction(m.get("noise_distance_m", 0.0), anchors.noise_cap_m),
        "rent":        cost_credit(m.get("rent_eur", 0.0), anchors.rent_budget_eur),
    }
    for dest_name, mins in m.get("destinations_min", {}).items():
        norm[f"dest_{dest_name}"] = cost_credit(mins, anchors.commute_cap_min)
    return norm


def resolve_weight(key: str, weights: dict[str, float], anchors: Anchors) -> float:
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
            weight = anchors.destinations.get(dest_name, {}).get("weight", DEFAULT_DEST_WEIGHT)
    else:
        weight = weights.get(key, DEFAULT_WEIGHTS.get(key, 0.0))
    return max(float(weight), 0.0)


def score_breakdown(m: dict[str, Any], weights: dict[str, float], anchors: Anchors) -> dict[str, dict[str, float]]:
    """Per-term weight, influence share, normalized value and points contributed.

    The contributions sum to exactly the value `compute_score` returns, which
    makes it possible to answer "why did this flat win?" rather than just
    "this flat won".
    """
    norm = normalize_metrics(m, anchors)
    resolved = {key: resolve_weight(key, weights, anchors) for key in norm}
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


def compute_score(m: dict[str, Any], weights: dict[str, float], anchors: Anchors) -> float:
    """Score a candidate on a fixed 0..SCORE_SCALE_MAX scale.

    A weighted average of the normalized metrics: every term sits in 0..1 and
    the weights are normalized to sum to 1, so the result is genuinely bounded
    - unlike the old raw weighted sum, which subtracted unbounded rent and
    commute terms and could land anywhere from negative to 40+.
    """
    return sum(term["contribution"] for term in score_breakdown(m, weights, anchors).values())
