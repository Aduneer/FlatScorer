"""Address to coordinates, under Nominatim's usage policy.

Importing `osm` is not incidental: the `http_user_agent` / `http_referer` it
configures are the policy obligation that lets this module talk to Nominatim at
all, and `osm._apply_setting` exists to make sure that obligation cannot go
quietly unmet again.
"""

from __future__ import annotations

import threading
import time

import osmnx as ox
from osmnx._errors import InsufficientResponseError

# Imported for two reasons: the osmnx settings applied at its import (the
# user-agent, which is what permits talking to Nominatim at all), and
# `_apply_setting`, which `use_nominatim` uses below.
from . import osm as _osm

# Nominatim's usage policy caps clients at 1 request/second, and osmnx does not
# throttle for us: https://operations.osmfoundation.org/policies/nominatim/
NOMINATIM_MIN_INTERVAL_S = 1.0


# The public instance, and the default. Configurable because the same policy
# requires it: "apps must make sure that they can switch the service at our
# request at any time (in particular, switching should be possible without
# requiring a software update)". A hard-coded endpoint cannot honour that, and
# the OSMF's ability to shed load from a misbehaving client should not depend on
# us shipping a release.
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/"


# Geocoding, like Overpass, fails transiently. Retry a couple of times before
# dropping a candidate from the ranking entirely.
GEOCODE_ATTEMPTS = 3


GEOCODE_BACKOFF_S = 2.0


# Monotonic timestamp of the last Nominatim request, shared process-wide so the
# rate limit holds across the candidate and destination loops (and across runs
# in the long-lived Streamlit process).
_last_geocode_at = 0.0


# Held across the wait, not just around the timestamp. Streamlit runs every
# session's script in its own thread, so two browser tabs scoring at once are two
# threads in this function - and read-then-sleep-then-write let all of them
# compute their wait against the *same* stale timestamp and fire together. That
# measured as four requests in the same instant against a policy whose words are
# "an absolute maximum of 1 request per second".
#
# Serializing here is the intended behaviour, not a cost: the limit is global to
# the application, so callers queuing behind each other is exactly right.
_geocode_lock = threading.Lock()


def _throttle_geocode():
    """Block until at least NOMINATIM_MIN_INTERVAL_S has passed since the last geocode."""
    global _last_geocode_at
    with _geocode_lock:
        wait = NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _last_geocode_at)
        if wait > 0:
            time.sleep(wait)
        _last_geocode_at = time.monotonic()


def use_nominatim(url: str = DEFAULT_NOMINATIM_URL):
    """Point geocoding at a Nominatim instance, for the rest of the process.

    Deliberately *not* saved and restored around each call, unlike
    `osm.query_with_retry`'s handling of `overpass_url`. That dance exists to
    stop a mirror chosen mid-run leaking into later runs; here every run applies
    its own configured value before geocoding anything - the default included -
    so there is no path by which a stale endpoint survives into the next run.
    """
    _osm._apply_setting("nominatim_url", url)


def geocode_safe(address: str, label: str, attempts: int = GEOCODE_ATTEMPTS) -> tuple[float, float] | None:
    """Safely geocode an address into (latitude, longitude) tuple, with rate limiting and retries."""
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        _throttle_geocode()
        try:
            return ox.geocode(address)
        except InsufficientResponseError as e:
            # Nominatim answered, it just has no match - retrying cannot help.
            last_err = e
            break
        except Exception as e:  # noqa: BLE001 - geocoding can fail from network errors or osmnx's own exceptions; drop this candidate instead of crashing the run
            last_err = e
            if attempt < attempts:
                print(f"[!] Geocoding '{label}' failed (attempt {attempt}/{attempts}): {e} - retrying...")
                time.sleep(GEOCODE_BACKOFF_S * attempt)

    print(f"[!] Couldn't geocode '{label}': {address} ({last_err})")
    print("    Ensure the address includes street, house number, postal code, and city.")
    return None
