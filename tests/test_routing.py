"""Travel modes, route_time, and the per-mode networks.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import networkx as nx
import osmnx as ox
import pandas as pd
import pytest
from conftest import (
    BERLIN_CRS,
    CHAIN_COORDS,
    bike_graph,
    mixed_mode_config,
    one_destination_config,
    only_problem,
    valid_config,
)

import flatscorer as fs

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


def test_route_time_fallback_uses_the_projected_crs_when_given():
    minutes, route = fs.route_time(disconnected_graph(), (52.0, 13.0), (52.0, 14.0),
                                   projected_crs=BERLIN_CRS)
    assert minutes == pytest.approx(68_500 / 83.33, rel=0.02)
    assert route == [(52.0, 13.0), (52.0, 14.0)]


def test_route_time_fallback_without_a_crs_keeps_the_legacy_estimate():
    minutes, _ = fs.route_time(disconnected_graph(), (52.0, 13.0), (52.0, 14.0))
    assert minutes == pytest.approx(111_000 / 83.33, rel=0.01)


def test_route_time_uses_the_network_when_a_path_exists():
    G = nx.MultiDiGraph(crs="EPSG:4326")
    G.add_node(1, x=13.0, y=52.0)
    G.add_node(2, x=13.01, y=52.0)
    G.add_edge(1, 2, length=1000.0)
    minutes, route = fs.route_time(G, (52.0, 13.0), (52.0, 13.01), projected_crs=BERLIN_CRS)
    assert minutes == pytest.approx(1000.0 / 83.33, rel=1e-6)
    assert route == [(52.0, 13.0), (52.0, 13.01)]

# ------------------------------------------------------ configurable walk speed --


def test_the_configured_walking_speed_reaches_the_commute_times(offline_run):
    """The whole point: tunable from config, without editing route_time's default."""
    df = offline_run(one_destination_config(walking_speed_m_per_min=100.0))
    assert df.iloc[0]["near_office_walk_min"] == pytest.approx(10.0, abs=0.05)


def test_an_absent_walking_speed_falls_back_to_the_default(offline_run):
    """Every config written before this parameter existed has to score identically."""
    df = offline_run(one_destination_config())
    assert df.iloc[0]["near_office_walk_min"] == pytest.approx(
        1000.0 / fs.DEFAULT_WALKING_SPEED_M_PER_MIN, abs=0.05)


def test_a_slower_pace_moves_the_score_not_just_the_reported_minutes(offline_run):
    """A pace change has to propagate through the commute term into the score.

    3 km at 83.33 m/min is 36 minutes, inside the default 45-minute cap and so
    worth something; at 40 m/min it is 75 minutes, past the cap and worth nothing.
    """
    far = {"Far Office": {"address": "3 Far Office Rd", "weight": 0.2}}
    brisk = offline_run(valid_config(destinations=far, parameters={}))
    slow = offline_run(valid_config(destinations=far, parameters={"walking_speed_m_per_min": 40.0}))

    assert brisk.iloc[0]["far_office_walk_min"] == pytest.approx(36.0, abs=0.1)
    assert slow.iloc[0]["far_office_walk_min"] == pytest.approx(75.0, abs=0.1)
    assert slow.iloc[0]["score"] < brisk.iloc[0]["score"]


def test_route_times_default_speed_is_the_documented_constant():
    """The default is a standalone-use fallback; run() overrides it either way."""
    import inspect
    default = inspect.signature(fs.route_time).parameters["speed_m_per_min"].default
    assert default == fs.DEFAULT_WALKING_SPEED_M_PER_MIN
    assert fs.DEFAULT_WALKING_SPEED_M_PER_MIN * 60 / 1000 == pytest.approx(5.0, abs=0.01)


def test_the_shipped_default_config_carries_a_walking_speed():
    assert fs.DEFAULT_CONFIG["parameters"]["walking_speed_m_per_min"] == fs.DEFAULT_WALKING_SPEED_M_PER_MIN

# ----------------------------------------------------------------- cycling mode --

def test_a_destination_may_declare_either_travel_mode():
    for mode in ("walk", "bike"):
        config = valid_config(destinations={"Work": {"address": "2 Office Rd", "weight": 0.2, "mode": mode}})
        assert fs.validate_config(config) == [], mode


def test_an_unknown_travel_mode_is_rejected_and_names_the_destination():
    problem = only_problem(valid_config(
        destinations={"Work": {"address": "2 Office Rd", "weight": 0.2, "mode": "drive"}}))
    assert "destinations['Work']" in problem
    assert "'drive'" in problem
    assert "bike" in problem and "walk" in problem


def test_a_destination_without_a_mode_walks():
    """Every config written before cycling existed is an all-walk config."""
    assert fs.validate_config(valid_config()) == []
    assert fs.destination_mode({"address": "2 Office Rd"}) == "walk"
    assert fs.destination_mode({"address": "2 Office Rd", "mode": "bike"}) == "bike"


def test_a_non_positive_cycling_speed_is_rejected():
    problem = only_problem(valid_config(parameters={"cycling_speed_m_per_min": 0}))
    assert "cycling_speed_m_per_min" in problem
    assert "greater than 0" in problem


def test_the_shipped_default_config_carries_a_cycling_speed():
    assert fs.DEFAULT_CONFIG["parameters"]["cycling_speed_m_per_min"] == fs.DEFAULT_CYCLING_SPEED_M_PER_MIN
    assert fs.DEFAULT_CYCLING_SPEED_M_PER_MIN * 60 / 1000 == pytest.approx(15.0, abs=0.01)


def test_a_commute_column_carries_its_mode():
    """A cycling commute must never be reported in a column that says 'walk'."""
    assert fs.commute_column("Near Office") == "near_office_walk_min"
    assert fs.commute_column("Near Office", "walk") == "near_office_walk_min"
    assert fs.commute_column("Near Office", "bike") == "near_office_bike_min"


# The cycling network, laid out so that reusing a *walking* node id on it lands
# somewhere plausible instead of raising: the ids overlap, the places do not.
#   node 3 @ 13.000 (the flat) --500m-- node 4 @ 13.010 (Near Office)
#   node 1 @ 13.050 --1500m-- node 2 @ 13.030 --1500m-- node 4
# So the honest bike route is 500 m, while every way of getting the nodes from
# the wrong graph yields 1500, 2000 or 3000 m - wrong, and wrong quietly.


def test_each_mode_resolves_its_nodes_in_its_own_graph(offline_run):
    """The per-mode sibling of the per-destination node cache test.

    A node id means nothing outside the graph it came from, so caching one
    across modes doesn't raise - it silently answers with another junction's
    commute. Both endpoints are covered: on the walk graph the flat is node 1
    and the office node 2, on the bike graph they are 3 and 4.
    """
    df = offline_run(mixed_mode_config(cycling_speed_m_per_min=250.0),
                     graphs={"bike": bike_graph()})
    row = df.iloc[0]
    assert row["near_office_walk_min"] == pytest.approx(1000.0 / 83.33, abs=0.05)
    assert row["bike_office_bike_min"] == pytest.approx(500.0 / 250.0, abs=0.05)


def test_the_configured_cycling_speed_reaches_the_bike_commute_times(offline_run):
    df = offline_run(mixed_mode_config(cycling_speed_m_per_min=100.0),
                     graphs={"bike": bike_graph()})
    assert df.iloc[0]["bike_office_bike_min"] == pytest.approx(5.0, abs=0.05)


def test_an_absent_cycling_speed_falls_back_to_the_default(offline_run):
    df = offline_run(mixed_mode_config(), graphs={"bike": bike_graph()})
    assert df.iloc[0]["bike_office_bike_min"] == pytest.approx(
        500.0 / fs.DEFAULT_CYCLING_SPEED_M_PER_MIN, abs=0.05)


def test_the_walking_pace_does_not_leak_into_a_bike_commute(offline_run):
    """Each mode divides by its own pace; sharing one would be invisible."""
    df = offline_run(mixed_mode_config(walking_speed_m_per_min=250.0, cycling_speed_m_per_min=250.0),
                     graphs={"bike": bike_graph()})
    assert df.iloc[0]["near_office_walk_min"] == pytest.approx(4.0, abs=0.05)
    assert df.iloc[0]["bike_office_bike_min"] == pytest.approx(2.0, abs=0.05)


def test_a_mixed_config_costs_one_node_lookup_per_candidate_per_mode(monkeypatch, offline_run):
    lookups = []
    real = ox.distance.nearest_nodes

    def counting(G, x, y, **kwargs):
        lookups.append((x, y))
        return real(G, x, y, **kwargs)

    monkeypatch.setattr(ox.distance, "nearest_nodes", counting)
    config = mixed_mode_config()
    config["candidates"].append({"name": "Flat B", "address": "3 Far Office Rd", "rent": 1500})
    offline_run(config, graphs={"bike": bike_graph()})

    # 2 candidates x 2 modes + 2 destinations = 6. Two candidates, not one, so
    # the number actually discriminates: resolving a node per leg instead would
    # be 2*2*2 = 8, which at one candidate collides with the correct answer.
    assert len(lookups) == 6


def test_cycling_a_far_destination_scores_better_than_walking_it(offline_run):
    """The reason the feature exists: a 36-minute walk is a 12-minute cycle."""
    far = {"address": "3 Far Office Rd", "weight": 0.5}
    walked = offline_run(valid_config(destinations={"Far Office": dict(far)}))
    cycled = offline_run(valid_config(destinations={"Far Office": dict(far, mode="bike")}))

    assert walked.iloc[0]["far_office_walk_min"] == pytest.approx(36.0, abs=0.1)
    assert cycled.iloc[0]["far_office_bike_min"] == pytest.approx(12.0, abs=0.1)
    assert cycled.iloc[0]["score"] > walked.iloc[0]["score"]


def test_an_all_walk_config_downloads_exactly_one_network(run_recording_downloads):
    """The whole point of downloading lazily: existing configs pay nothing extra."""
    requested, _ = run_recording_downloads(valid_config(destinations={
        "Near Office": {"address": "2 Office Rd", "weight": 0.2},
        "Far Office": {"address": "3 Far Office Rd", "weight": 0.2, "mode": "walk"},
    }))
    assert requested == ["walk"]


def test_a_mixed_config_downloads_one_network_per_mode(run_recording_downloads):
    requested, df = run_recording_downloads(mixed_mode_config(cycling_speed_m_per_min=250.0))
    assert requested == ["walk", "bike"]
    # And the bike leg really came off the bike graph, not a second walk one.
    assert df.iloc[0]["bike_office_bike_min"] == pytest.approx(2.0, abs=0.05)


def test_an_all_bike_config_downloads_only_the_bike_network(run_recording_downloads):
    requested, _ = run_recording_downloads(valid_config(destinations={
        "Bike Office": {"address": "2 Office Rd", "weight": 0.2, "mode": "bike"},
    }))
    assert requested == ["bike"]


def test_a_config_with_no_destinations_downloads_no_street_network(run_recording_downloads):
    """Nothing to route means nothing to route over - the graph was never used."""
    requested, df = run_recording_downloads(valid_config(destinations={}))
    assert requested == []
    assert not [col for col in df.columns if col.endswith(("_walk_min", "_bike_min"))]


def test_a_mixed_map_names_its_route_layer_for_both_modes(offline_run, tmp_path):
    offline_run(mixed_mode_config(), graphs={"bike": bike_graph()})
    mixed = (tmp_path / "map.html").read_text(encoding="utf-8")

    offline_run(one_destination_config())
    walk_only = (tmp_path / "map.html").read_text(encoding="utf-8")

    # An all-walk map keeps the layer name it has always had.
    assert "Predicted walking routes" in walk_only
    assert "Predicted commute routes" in mixed
    assert "Predicted walking routes" not in mixed


# --- routing_mode: estimating a commute instead of routing one ---------------
#
# The measured case for this mode is written up in TODO.md's `feat/routing-mode`
# entry: across three cities the ranking barely moved, so what these tests pin
# is that the *mechanism* is right - no download, honest column names, and the
# network path untouched.


def test_straight_line_time_applies_the_detour_factor():
    """1000 m apart on the chain graph, at the default walking pace."""
    orig, dest = CHAIN_COORDS["1 Main St"], CHAIN_COORDS["2 Office Rd"]
    speed = fs.DEFAULT_WALKING_SPEED_M_PER_MIN

    plain = fs.straight_line_time(orig, dest, speed_m_per_min=speed, detour_factor=1.0,
                                  projected_crs=BERLIN_CRS)
    detoured = fs.straight_line_time(orig, dest, speed_m_per_min=speed, detour_factor=1.5,
                                     projected_crs=BERLIN_CRS)

    # ~687 m east-west at 52°N, so the absolute value is a projection fact; the
    # relationship is the thing being asserted.
    assert detoured == pytest.approx(plain * 1.5)
    assert plain == pytest.approx(
        fs.straight_line_distance_m(orig, dest, BERLIN_CRS) / speed)


def test_an_approximate_commute_column_says_so():
    """The caveat has to survive into a CSV mailed to someone else."""
    assert fs.commute_column("Near Office", "walk", "network") == "near_office_walk_min"
    assert fs.commute_column("Near Office", "walk", "straight_line") == "near_office_walk_min_approx"
    assert fs.commute_column("Near Office", "bike", "straight_line") == "near_office_bike_min_approx"
    # The default has to stay the routed name, or every existing config's CSV
    # changes shape.
    assert fs.commute_column("Near Office") == "near_office_walk_min"


def test_a_suffix_resolves_to_its_mode_approximate_or_not():
    """The map and the report both recover a mode from a column suffix."""
    assert fs.mode_for_suffix("_walk_min") == "walk"
    assert fs.mode_for_suffix("_walk_min_approx") == "walk"
    assert fs.mode_for_suffix("_bike_min_approx") == "bike"


def test_every_commute_column_matches_exactly_one_suffix():
    """What actually protects the destination label, unlike suffix ordering.

    `_walk_min` is a *prefix* of `_walk_min_approx`, not a suffix, so `endswith`
    separates them on its own. This pins the property the surfaces rely on -
    exactly one match per column - rather than the ordering, which a mutation
    test showed to be inert today.
    """
    for routing_mode in fs.ROUTING_MODES:
        for mode in fs.TRAVEL_MODES:
            col = fs.commute_column("Near Office", mode, routing_mode)
            matches = [s for s in fs.COMMUTE_COLUMN_SUFFIXES if col.endswith(s)]
            assert len(matches) == 1, (col, matches)
            assert col[:-len(matches[0])] == "near_office"


def test_straight_line_mode_downloads_no_street_network(run_recording_downloads):
    """The entire point of the mode: the heaviest Overpass call never happens."""
    config = one_destination_config(routing_mode="straight_line", detour_factor=1.3)
    requested, df = run_recording_downloads(config)

    assert requested == []
    assert "near_office_walk_min_approx" in df.columns
    assert "near_office_walk_min" not in df.columns


def test_network_mode_still_downloads_its_graph(run_recording_downloads):
    """The control for the test above - the default must be unchanged."""
    requested, df = run_recording_downloads(one_destination_config())

    assert requested == ["walk"]
    assert "near_office_walk_min" in df.columns


def test_switching_to_straight_line_touches_only_the_commute(offline_run):
    """The mode must reach the commute and nothing else.

    `score` is expected to move, because the commute it is built from moved.
    Every *measured* column must not: a changed supermarket count or rent would
    mean the routing mode had leaked somewhere it has no business being.

    (That `routing_mode="network"` still produces exactly what it produced
    before this parameter existed is pinned by the rest of the suite, which
    asserts those columns and values directly and was not edited.)
    """
    routed = offline_run(one_destination_config())
    estimated = offline_run(one_destination_config(routing_mode="straight_line"))

    assert list(estimated.columns) == [
        c if c != "near_office_walk_min" else "near_office_walk_min_approx"
        for c in routed.columns
    ]
    measured = [c for c in routed.columns if c not in ("score", "near_office_walk_min")]
    pd.testing.assert_frame_equal(routed[measured], estimated[measured])


def test_the_detour_factor_cannot_reorder_candidates(offline_run):
    """Measured claim, pinned: a constant multiplier cannot change a ranking.

    This is why TODO.md says not to build per-city calibration. Two runs at
    factors far apart must rank identically - only the minutes may differ.
    """
    low = offline_run(one_destination_config(routing_mode="straight_line", detour_factor=1.05))
    high = offline_run(one_destination_config(routing_mode="straight_line", detour_factor=1.9))

    assert list(low["name"]) == list(high["name"])
    assert (low["near_office_walk_min_approx"] < high["near_office_walk_min_approx"]).all()


def test_the_shipped_default_is_still_network_routing():
    """Fast mode is opt-in. An existing config must not silently start estimating."""
    assert fs.DEFAULT_ROUTING_MODE == "network"
    assert fs.DEFAULT_CONFIG["parameters"]["routing_mode"] == "network"
    assert fs.DEFAULT_CONFIG["parameters"]["detour_factor"] == fs.DEFAULT_DETOUR_FACTOR
    assert fs.DEFAULT_DETOUR_FACTOR >= 1.0
