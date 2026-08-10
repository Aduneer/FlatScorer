"""The whole pipeline, driven end to end offline.

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
    bike_graph,
    mixed_mode_config,
    one_destination_config,
    valid_config,
)

import flatscorer as fs
from flatscorer import geocode

# ------------------------------------------------------- destination node cache --

def two_node_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph(crs="EPSG:4326")
    G.add_node(1, x=13.0, y=52.0)
    G.add_node(2, x=13.01, y=52.0)
    G.add_edge(1, 2, length=1000.0)
    return G


def test_a_precomputed_dest_node_gives_the_identical_route():
    """The cache is an optimisation: passing nodes must change nothing at all."""
    G = two_node_graph()
    orig, dest = (52.0, 13.0), (52.0, 13.01)

    without = fs.route_time(G, orig, dest, projected_crs=BERLIN_CRS)
    with_nodes = fs.route_time(G, orig, dest, projected_crs=BERLIN_CRS,
                               orig_node=fs.nearest_node(G, orig),
                               dest_node=fs.nearest_node(G, dest))
    assert with_nodes == without


def test_precomputed_nodes_skip_the_lookup_entirely(monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("nearest_nodes ran despite both endpoints being supplied")

    monkeypatch.setattr(ox.distance, "nearest_nodes", must_not_be_called)
    minutes, _ = fs.route_time(two_node_graph(), (52.0, 13.0), (52.0, 13.01),
                               orig_node=1, dest_node=2)
    assert minutes == pytest.approx(1000.0 / 83.33, rel=1e-6)


def test_omitting_the_nodes_still_resolves_them(monkeypatch):
    """Existing callers pass coordinates only; that path has to keep working."""
    calls = []
    real = ox.distance.nearest_nodes

    def counting(G, x, y, **kwargs):
        calls.append((x, y))
        return real(G, x, y, **kwargs)

    monkeypatch.setattr(ox.distance, "nearest_nodes", counting)
    fs.route_time(two_node_graph(), (52.0, 13.0), (52.0, 13.01))
    assert len(calls) == 2


# A candidate and two destinations strung along one line, at known edge lengths:
#   flat --1000m-- Near Office --2000m-- Far Office


def test_each_destination_keeps_its_own_cached_node(offline_run):
    """The cache is keyed per destination; sharing one node silently swaps times.

    That is the failure mode worth pinning - a wrong node produces a plausible
    number rather than an error, so nothing else in the run would notice.
    """
    df = offline_run(valid_config(destinations={
        "Near Office": {"address": "2 Office Rd", "weight": 0.2},
        "Far Office": {"address": "3 Far Office Rd", "weight": 0.2},
    }))
    row = df.iloc[0]
    assert row["near_office_walk_min"] == pytest.approx(1000.0 / 83.33, abs=0.05)
    assert row["far_office_walk_min"] == pytest.approx(3000.0 / 83.33, abs=0.05)


def test_a_listing_url_reaches_the_score_table_and_the_csv(offline_run, tmp_path):
    df = offline_run(valid_config(candidates=[
        {"name": "Flat A", "address": "1 Main St", "rent": 1800,
         "url": "https://example.com/expose/1"},
    ]))
    assert df.set_index("name").loc["Flat A", "url"] == "https://example.com/expose/1"
    assert pd.read_csv(tmp_path / "scores.csv").loc[0, "url"] == "https://example.com/expose/1"


def test_a_run_with_no_listing_urls_has_no_url_column_at_all(offline_run, tmp_path):
    """Configs predating the field must produce exactly the output they used to."""
    df = offline_run(valid_config())
    assert "url" not in df.columns
    assert "url" not in pd.read_csv(tmp_path / "scores.csv").columns


def test_a_candidate_without_a_link_is_blank_rather_than_missing(offline_run, tmp_path):
    """One linked flat brings the column in; the others just have nothing in it."""
    df = offline_run(valid_config(candidates=[
        {"name": "Flat A", "address": "1 Main St", "rent": 1800, "url": "https://example.com/a"},
        {"name": "Flat B", "address": "2 Office Rd", "rent": 1700},
    ]))
    urls = df.set_index("name")["url"]
    assert urls["Flat A"] == "https://example.com/a"
    assert urls["Flat B"] == ""
    assert pd.isna(pd.read_csv(tmp_path / "scores.csv").set_index("name").loc["Flat B", "url"])


def test_the_node_cache_does_not_change_the_commute_times(monkeypatch, offline_run):
    """Acceptance criterion for the optimisation: identical output, fewer lookups."""
    lookups = []
    real = ox.distance.nearest_nodes

    def counting(G, x, y, **kwargs):
        lookups.append((x, y))
        return real(G, x, y, **kwargs)

    monkeypatch.setattr(ox.distance, "nearest_nodes", counting)

    config = valid_config(destinations={
        "Near Office": {"address": "2 Office Rd", "weight": 0.2},
        "Far Office": {"address": "3 Far Office Rd", "weight": 0.2},
    })
    df = offline_run(config)

    # 1 candidate + 2 destinations = 3 lookups, not the 2*1*2 = 4 of one per leg.
    assert len(lookups) == 3
    assert df.iloc[0]["near_office_walk_min"] == pytest.approx(1000.0 / 83.33, abs=0.05)

# ------------------------------------------------------------- progress reporting --

class ProgressLog:
    """Collects every (fraction, label) the engine reports."""

    def __init__(self):
        self.calls: list[tuple[float, str]] = []

    def __call__(self, fraction: float, label: str):
        self.calls.append((fraction, label))

    @property
    def fractions(self) -> list[float]:
        return [fraction for fraction, _ in self.calls]

    @property
    def labels(self) -> list[str]:
        return [label for _, label in self.calls]

    def mentioning(self, needle: str) -> list[str]:
        return [label for label in self.labels if needle.lower() in label.lower()]


def test_progress_runs_from_zero_to_one_without_going_backwards(offline_run):
    """A bar that jumps backwards is worse than no bar - pin the invariant."""
    seen = ProgressLog()
    offline_run(one_destination_config(), progress=seen)

    assert seen.fractions, "no progress was reported at all"
    assert seen.fractions == sorted(seen.fractions), seen.fractions
    assert 0.0 <= seen.fractions[0] < 1.0
    assert seen.fractions[-1] == 1.0
    assert all(0.0 <= fraction <= 1.0 for fraction in seen.fractions)


def test_progress_labels_name_the_step_that_is_starting(offline_run):
    """The label is the whole point: "downloading" is what stops it reading as a hang."""
    seen = ProgressLog()
    offline_run(one_destination_config(), progress=seen)

    assert seen.mentioning("Flat A"), seen.labels
    assert seen.mentioning("Near Office"), seen.labels
    assert seen.mentioning("street network"), seen.labels
    assert seen.mentioning("points of interest") or seen.mentioning("shops"), seen.labels
    assert seen.mentioning("Scoring"), seen.labels


def test_the_slow_steps_carry_most_of_the_bar(offline_run):
    """Weighted, not counted: equal steps would park the bar during the downloads.

    The two OpenStreetMap downloads dominate a real run's wall clock, so together
    they have to own most of the bar's travel - otherwise it races to 80% and
    then sits there for a minute, which is the failure mode being fixed.
    """
    seen = ProgressLog()
    offline_run(one_destination_config(), progress=seen)

    at = {label: fraction for fraction, label in seen.calls}
    network = next(f for label, f in at.items() if "street network" in label)
    pois = next(f for label, f in at.items() if "OpenStreetMap" in label and "network" not in label)
    scoring = next(f for label, f in at.items() if label.startswith("Scoring"))
    assert (scoring - network) > 0.5, f"downloads own only {scoring - network:.0%} of the bar"
    assert network < pois < scoring


def test_progress_reaches_one_even_when_a_candidate_fails_to_geocode(monkeypatch, offline_run):
    """The plan is sized from the config, so a dropped candidate over-estimates it.

    Without the explicit finish, the bar would stop short of full on exactly the
    runs where the user is most likely to be staring at it.
    """
    real = fs.geocode_safe
    monkeypatch.setattr(geocode, "geocode_safe",
                        lambda addr, label, **kw: None if label == "Flat B" else real(addr, label, **kw))

    seen = ProgressLog()
    config = one_destination_config()
    config["candidates"].append({"name": "Flat B", "address": "3 Far Office Rd", "rent": 1500})
    offline_run(config, progress=seen)

    assert seen.fractions[-1] == 1.0
    assert seen.fractions == sorted(seen.fractions)


def test_a_mixed_config_announces_both_network_downloads(offline_run):
    seen = ProgressLog()
    offline_run(mixed_mode_config(), graphs={"bike": bike_graph()}, progress=seen)

    assert seen.mentioning("walking street network"), seen.labels
    assert seen.mentioning("cycling street network"), seen.labels


def test_progress_is_optional_and_changes_nothing(offline_run):
    """The CLI passes no callback; that path has to stay byte-for-byte the same."""
    silent = offline_run(one_destination_config())
    watched = offline_run(one_destination_config(), progress=ProgressLog())
    pd.testing.assert_frame_equal(silent, watched)


# ------------------------------------------------------------- output location --

def test_the_results_default_into_an_output_directory_under_the_cwd(offline_run, monkeypatch, tmp_path):
    """A run with no `output` block writes into ./output, creating it.

    Before this, both files landed loose in whatever directory you ran from.
    """
    monkeypatch.chdir(tmp_path)
    config = one_destination_config()
    config.pop("output", None)

    offline_run(config, override_output=False)

    assert (tmp_path / "output" / "apartment_scores.csv").is_file()
    assert (tmp_path / "output" / "apartment_map.html").is_file()
    assert not (tmp_path / "apartment_scores.csv").exists(), "still writing into the cwd"


def test_an_explicit_output_path_still_wins(offline_run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = one_destination_config()
    config["output"] = {"csv_file": "mine.csv", "html_file": "mine.html"}

    offline_run(config, override_output=False)

    assert (tmp_path / "mine.csv").is_file()
    assert (tmp_path / "mine.html").is_file()
    assert not (tmp_path / "output").exists()


def test_an_explicit_path_into_a_missing_directory_is_created(offline_run, monkeypatch, tmp_path):
    """Otherwise the failure lands at the very end of a multi-minute run."""
    monkeypatch.chdir(tmp_path)
    config = one_destination_config()
    config["output"] = {
        "csv_file": str(tmp_path / "nowhere" / "scores.csv"),
        "html_file": str(tmp_path / "nowhere" / "map.html"),
    }

    offline_run(config, override_output=False)

    assert (tmp_path / "nowhere" / "scores.csv").is_file()
    assert (tmp_path / "nowhere" / "map.html").is_file()
