"""Nominatim throttling and retry.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import time

import osmnx as ox
import pytest
from osmnx._errors import InsufficientResponseError

import flatscorer as fs
from flatscorer import geocode

# ------------------------------------------------------- geocode rate limiting --

def test_geocode_safe_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def flaky(address):
        calls.append(address)
        if len(calls) < 3:
            raise TimeoutError("boom")
        return (52.5, 13.4)

    monkeypatch.setattr(ox, "geocode", flaky)
    assert fs.geocode_safe("Somewhere", "Flat A") == (52.5, 13.4)
    assert len(calls) == 3


def test_geocode_safe_gives_up_after_the_attempt_budget(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def always_fails(address):
        calls.append(address)
        raise TimeoutError("boom")

    monkeypatch.setattr(ox, "geocode", always_fails)
    assert fs.geocode_safe("Somewhere", "Flat A") is None
    assert len(calls) == fs.GEOCODE_ATTEMPTS


def test_geocode_safe_does_not_retry_an_address_nominatim_cannot_match(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def not_found(address):
        calls.append(address)
        raise InsufficientResponseError("no match")

    monkeypatch.setattr(ox, "geocode", not_found)
    assert fs.geocode_safe("Nowhere at all", "Flat B") is None
    assert len(calls) == 1, "a definitive 'no match' is not worth retrying"


def test_geocode_calls_are_spaced_to_respect_the_nominatim_policy(monkeypatch):
    """Nominatim allows 1 req/sec; the throttle must sleep for the shortfall."""
    clock = {"now": 100.0}
    slept = []

    monkeypatch.setattr(geocode, "_last_geocode_at", 0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(ox, "geocode", lambda address: (52.5, 13.4))

    fs.geocode_safe("First", "A")     # long idle - no wait needed
    fs.geocode_safe("Second", "B")    # immediately after - must wait a full second
    clock["now"] += 0.25
    fs.geocode_safe("Third", "C")     # 0.25 s later - waits the remaining 0.75 s

    assert slept[0] == pytest.approx(fs.NOMINATIM_MIN_INTERVAL_S)
    assert slept[1] == pytest.approx(0.75)
