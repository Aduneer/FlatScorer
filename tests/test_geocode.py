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


def test_the_nominatim_throttle_holds_across_threads():
    """Streamlit runs every session's script in its own thread.

    Read-then-sleep-then-write let concurrent callers all measure their wait
    against the same stale timestamp and fire together - four requests in one
    instant against a policy whose words are "an absolute maximum of 1 request
    per second". The lock is what makes the limit global rather than per-thread.
    """
    import threading
    import time

    from flatscorer import geocode

    geocode._last_geocode_at = 0.0
    fired: list[float] = []
    barrier = threading.Barrier(4)

    def slot():
        barrier.wait()  # maximise the overlap rather than hoping for it
        geocode._throttle_geocode()
        fired.append(time.monotonic())

    threads = [threading.Thread(target=slot) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    fired.sort()
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert len(gaps) == 3
    assert all(gap >= geocode.NOMINATIM_MIN_INTERVAL_S * 0.98 for gap in gaps), gaps


# --- configurable endpoint ---------------------------------------------------
#
# Nominatim's policy: "apps must make sure that they can switch the service at
# our request at any time (in particular, switching should be possible without
# requiring a software update)". A hard-coded endpoint cannot honour that.


def test_the_geocoding_endpoint_is_configurable():
    import osmnx as ox

    from flatscorer import geocode

    try:
        geocode.use_nominatim("https://nominatim.example.org/")
        assert ox.settings.nominatim_url == "https://nominatim.example.org/"
    finally:
        geocode.use_nominatim()
    assert ox.settings.nominatim_url == geocode.DEFAULT_NOMINATIM_URL


def test_a_run_applies_its_configured_endpoint(offline_run):
    """It has to reach the engine, not just validate - so drive a whole run."""
    import osmnx as ox
    from conftest import one_destination_config

    try:
        offline_run(one_destination_config(nominatim_url="https://geocoder.example.org/"))
        assert ox.settings.nominatim_url == "https://geocoder.example.org/"
    finally:
        from flatscorer import geocode
        geocode.use_nominatim()


def test_a_run_without_the_parameter_uses_the_public_instance(offline_run):
    """No stale endpoint may survive from a previous run - every run sets one."""
    import osmnx as ox
    from conftest import one_destination_config

    from flatscorer import geocode

    geocode.use_nominatim("https://leftover.example.org/")
    offline_run(one_destination_config())
    assert ox.settings.nominatim_url == geocode.DEFAULT_NOMINATIM_URL
