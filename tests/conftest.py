"""Fixtures and helpers shared by more than one test module.

`offline_run` is the important one: it patches `geocode_safe` and
`query_with_retry` and hands `run()` a three-node `chain_graph()` plus an empty
POI frame, which covers the whole loop — routing, scoring, CSV, map — with no
network. It refills its response queue per call on purpose, so one test can run
two configs and compare.

Patch targets name the *owning module* (`flatscorer.geocode`, `flatscorer.osm`)
rather than the `flatscorer` package. The engine calls its seams through their
module, so a patch on the package that merely re-exports them is not seen.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import pytest

import flatscorer as fs
from flatscorer import geocode, osm

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


BERLIN_CRS = "EPSG:32633"


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


CHAIN_COORDS = {
    "1 Main St": (52.0, 13.0),
    "2 Office Rd": (52.0, 13.01),
    "3 Far Office Rd": (52.0, 13.02),
}


def chain_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph(crs="EPSG:4326")
    for node, (lat, lon) in zip((1, 2, 3), CHAIN_COORDS.values()):
        G.add_node(node, x=lon, y=lat)
    for a, b, length in ((1, 2, 1000.0), (2, 3, 2000.0)):
        G.add_edge(a, b, length=length)
        G.add_edge(b, a, length=length)
    return G


@pytest.fixture
def offline_run(monkeypatch, tmp_path):
    """Drive run() end to end with a tiny graph and no POIs, no network at all.

    Callable more than once per test, so two configs can be compared - hence the
    queue being refilled per run rather than a one-shot iterator.

    `graphs` supplies a specific graph for a mode; anything not named there gets
    a fresh `chain_graph()`. Since both modes otherwise get the *same* geometry,
    a test that needs to prove a route ran over the right network has to pass its
    own - identical graphs can't tell the two apart.
    """
    monkeypatch.setattr(geocode, "geocode_safe", lambda addr, label, **kw: CHAIN_COORDS[addr])

    responses: list[Any] = []
    monkeypatch.setattr(osm, "query_with_retry", lambda fn, **kw: responses.pop(0))

    def run(config: dict, graphs: dict[str, nx.MultiDiGraph] | None = None,
            progress=None, override_output: bool = True) -> pd.DataFrame:
        # Outputs are redirected into tmp_path so a run doesn't litter the
        # checkout. `override_output=False` leaves the config alone, which is how
        # the default output location itself gets tested - pair it with
        # `monkeypatch.chdir`, since the default is relative to the cwd.
        if override_output:
            config["output"] = {
                "csv_file": str(tmp_path / "scores.csv"),
                "html_file": str(tmp_path / "map.html"),
                # Third artifact, third redirect - without it every test run
                # drops output/apartment_overview.html into the checkout.
                "overview_file": str(tmp_path / "overview.html"),
            }
        # run() downloads one graph per travel mode present in `destinations`, in
        # first-mentioned order, and then the POIs - so the queue has to match.
        # `routing_mode="straight_line"` downloads no graph at all, and an
        # unconsumed graph left in the queue would be handed to the POI call
        # instead, which fails somewhere far away from the cause.
        modes = list(dict.fromkeys(
            fs.destination_mode(info) for info in config.get("destinations", {}).values()
        ))
        if config.get("parameters", {}).get("routing_mode") == "straight_line":
            modes = []
        responses[:] = [(graphs or {}).get(mode) or chain_graph() for mode in modes]
        responses.append(gpd.GeoDataFrame())
        return fs.FlatScorer(config, verbose=False, progress=progress).run()

    return run


def one_destination_config(**parameters) -> dict:
    """A candidate 1000 m along the chain graph from its only destination."""
    return valid_config(
        destinations={"Near Office": {"address": "2 Office Rd", "weight": 0.2}},
        parameters=parameters,
    )


def bike_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph(crs="EPSG:4326")
    for node, lon in ((1, 13.05), (2, 13.03), (3, 13.0), (4, 13.01)):
        G.add_node(node, x=lon, y=52.0)
    for a, b, length in ((1, 2, 1500.0), (2, 4, 1500.0), (4, 3, 500.0)):
        G.add_edge(a, b, length=length)
        G.add_edge(b, a, length=length)
    return G


def mixed_mode_config(**parameters) -> dict:
    """One walked and one cycled destination, both at the same address.

    Same address on purpose: the two commutes can then only differ because they
    were routed over different networks, not because they went somewhere else.
    """
    return valid_config(
        destinations={
            "Near Office": {"address": "2 Office Rd", "weight": 0.2},
            "Bike Office": {"address": "2 Office Rd", "weight": 0.2, "mode": "bike"},
        },
        parameters=parameters,
    )


@pytest.fixture
def run_recording_downloads(monkeypatch, tmp_path):
    """Like `offline_run`, but lets the Overpass-backed callables actually run.

    `offline_run` replaces `query_with_retry` with the canned answer, so it never
    executes the closure and can't see which `network_type` was asked for. This
    one stubs osmnx itself instead, which is the only way to prove the cycling
    graph is a *bike* download rather than a second walk one.
    """
    requested: list[str] = []

    def fake_graph_from_bbox(bbox=None, network_type=None, **kwargs):
        requested.append(network_type)
        return bike_graph() if network_type == "bike" else chain_graph()

    monkeypatch.setattr(geocode, "geocode_safe", lambda addr, label, **kw: CHAIN_COORDS[addr])
    monkeypatch.setattr(ox, "graph_from_bbox", fake_graph_from_bbox)
    monkeypatch.setattr(ox, "features_from_bbox", lambda **kwargs: gpd.GeoDataFrame())
    monkeypatch.setattr(osm, "query_with_retry", lambda fn, **kw: fn())

    def run(config: dict) -> tuple[list[str], pd.DataFrame]:
        config["output"] = {
            "csv_file": str(tmp_path / "scores.csv"),
            "html_file": str(tmp_path / "map.html"),
            "overview_file": str(tmp_path / "overview.html"),
        }
        return requested, fs.FlatScorer(config, verbose=False).run()

    return run
