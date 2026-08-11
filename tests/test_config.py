"""validate_config: every problem, reported in one pass.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import osmnx as ox
import pytest
from conftest import (
    make_scorer,
    only_problem,
    valid_config,
)

import flatscorer as fs

# ------------------------------------------------------------ config validation --


def test_a_valid_config_has_no_problems():
    assert fs.validate_config(valid_config()) == []


def test_the_shipped_default_config_is_valid():
    """--generate-config writes this; if it doesn't validate, first-run is broken."""
    assert fs.validate_config(fs.DEFAULT_CONFIG) == []


def test_the_shipped_example_config_is_valid():
    """config.example.json is what the README documents and what people copy."""
    import json
    import pathlib

    example = pathlib.Path(__file__).resolve().parent.parent / "config.example.json"
    assert fs.validate_config(json.loads(example.read_text(encoding="utf-8"))) == []


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


# -- listing URLs: optional, carried not scored --

def test_a_candidate_with_a_listing_url_is_valid():
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800,
                 "url": "https://www.immobilienscout24.de/expose/123"}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []


def test_a_blank_listing_url_means_no_link_rather_than_an_error():
    """The GUI omits the key when the cell is empty, but a hand-written config
    saying `"url": ""` means the same thing and shouldn't be a hard error."""
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800, "url": "   "}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []
    assert fs.candidate_url(candidate) is None


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "ftp://example.com/flat",
    "www.immobilienscout24.de/expose/123",
    "//example.com/flat",
])
def test_a_listing_url_without_an_http_scheme_is_rejected(url):
    """The URL ends up as an href in the map popup, and config.json is shared."""
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800, "url": url}
    problem = only_problem(valid_config(candidates=[candidate]))
    assert "'url'" in problem
    assert "http" in problem


@pytest.mark.parametrize("url", [123, ["https://example.com"], {"href": "x"}])
def test_a_non_string_listing_url_is_rejected(url):
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800, "url": url}
    assert "'url'" in only_problem(valid_config(candidates=[candidate]))


def test_a_bad_listing_url_is_reported_alongside_the_other_problems():
    """Validation reports everything in one pass - the URL check must not
    short-circuit the rent check that follows it."""
    config = valid_config(candidates=[
        {"name": "Flat A", "address": "1 Main St", "url": "javascript:alert(1)"},
    ])
    problems = fs.validate_config(config)
    assert len(problems) == 2, problems
    assert any("'url'" in p for p in problems)
    assert any("'rent'" in p for p in problems)


def test_candidate_url_reports_none_when_the_key_is_absent():
    assert fs.candidate_url({"name": "Flat A", "address": "1 Main St", "rent": 1800}) is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://example.com", "example.com"])
def test_candidate_url_drops_a_link_that_is_not_http(url):
    """`FlatScorer(config).run()` is a public entry point, so `generate_map` can
    be reached without `validate_config` ever having run. The scheme is checked
    again here rather than assumed."""
    assert fs.candidate_url({"name": "Flat A", "url": url}) is None


def test_candidate_url_strips_surrounding_whitespace():
    candidate = {"name": "Flat A", "url": "  https://example.com/flat  "}
    assert fs.candidate_url(candidate) == "https://example.com/flat"


def test_a_candidate_with_an_image_url_is_valid():
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800,
                 "image": "https://example.com/photo.jpg"}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []
    assert fs.candidate_image(candidate) == "https://example.com/photo.jpg"


def test_an_image_path_that_does_not_exist_is_not_a_config_error():
    """A config is meant to be shared, so it will routinely carry image paths
    valid on the machine that wrote it and absent on the one reading it. The
    report falls back to the dial - that must not block a multi-minute run."""
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800,
                 "image": "/no/such/directory/photo.png"}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []
    assert fs.candidate_image(candidate) == "/no/such/directory/photo.png"


def test_a_blank_image_means_no_photo_rather_than_an_error():
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800, "image": "   "}
    assert fs.validate_config(valid_config(candidates=[candidate])) == []
    assert fs.candidate_image(candidate) is None


@pytest.mark.parametrize("image", [123, ["a.png"], {"src": "a.png"}, True])
def test_a_non_string_image_is_rejected(image):
    candidate = {"name": "Flat A", "address": "1 Main St", "rent": 1800, "image": image}
    assert "'image'" in only_problem(valid_config(candidates=[candidate]))


def test_candidate_image_reports_none_when_the_key_is_absent():
    assert fs.candidate_image({"name": "Flat A", "address": "1 Main St", "rent": 1800}) is None


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


@pytest.mark.parametrize("key", ["buffer_m", "noise_cap_m", "rent_budget_eur", "commute_cap_min",
                                 "walking_speed_m_per_min"])
def test_a_non_positive_normalization_anchor_is_rejected(key):
    assert f"parameters['{key}']" in only_problem(valid_config(parameters={key: 0}))


def test_a_non_positive_saturation_point_is_rejected():
    config = valid_config(parameters={"saturation": {"supermarket": 0}})
    assert "saturation" in only_problem(config)


# -- the engine refuses to start on a bad config --

def test_run_raises_config_error_before_touching_the_network(monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("geocoding started despite an invalid config")

    monkeypatch.setattr(ox, "geocode", must_not_be_called)
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


# --- routing_mode / detour_factor -------------------------------------------


def test_an_unknown_routing_mode_is_rejected():
    """Caught before the download, since it decides whether to download at all."""
    problem = only_problem(valid_config(parameters={"routing_mode": "as_the_crow_flies"}))
    assert "routing_mode" in problem
    assert "straight_line" in problem and "network" in problem


@pytest.mark.parametrize("mode", ["network", "straight_line"])
def test_both_routing_modes_are_accepted(mode):
    assert fs.validate_config(valid_config(parameters={"routing_mode": mode})) == []


def test_a_detour_factor_below_one_is_rejected():
    """A real path is never shorter than the straight line between its ends."""
    problem = only_problem(valid_config(parameters={"detour_factor": 0.8}))
    assert "detour_factor" in problem
    assert "at least 1" in problem


def test_a_non_numeric_detour_factor_is_rejected():
    problem = only_problem(valid_config(parameters={"detour_factor": "brisk"}))
    assert "detour_factor" in problem
    assert "must be a number" in problem


def test_a_detour_factor_of_exactly_one_is_allowed():
    """Pure straight-line distance is a meaningful choice, not an error."""
    assert fs.validate_config(valid_config(parameters={"detour_factor": 1.0})) == []


def test_a_non_http_geocoding_endpoint_is_rejected():
    """Caught offline; otherwise it fails deep inside osmnx, mid-run."""
    problem = only_problem(valid_config(parameters={"nominatim_url": "nominatim.example.org"}))
    assert "nominatim_url" in problem
    assert "http://" in problem


def test_an_empty_geocoding_endpoint_is_rejected():
    problem = only_problem(valid_config(parameters={"nominatim_url": "   "}))
    assert "nominatim_url" in problem


def test_a_custom_geocoding_endpoint_is_accepted():
    assert fs.validate_config(valid_config(parameters={
        "nominatim_url": "https://nominatim.example.org/"})) == []
