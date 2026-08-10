"""Every filesystem location FlatScorer resolves at runtime.

This is the *only* module allowed to do `__file__` arithmetic or to name a bare
relative default. Everything else asks here.

The reason is the eventual frozen desktop build. A PyInstaller bundle unpacks its
data files into a temporary directory named by `sys._MEIPASS`, and a frozen app's
working directory is whatever the OS felt like — `/` when launched from the macOS
Finder. Worse, the bundle directory is read-only in two of the three targets (a
signed `.app`, an AppImage's squashfs), so "write next to the executable" is not
a fallback either. Reads and writes therefore need opposite treatment: reads come
from the bundle, writes go to a user-owned directory.

`cache_dir()` and `output_dir()` currently return exactly what the code returned
before this module existed. They are the seam, not yet the change.
"""

from __future__ import annotations

import os
import sys

import platformdirs

# Used for the per-user cache directory. Capitalized because on macOS and Windows
# this lands somewhere the user actually browses.
APP_NAME = "FlatScorer"

# Where read-only resources shipped with the code actually live. Under
# PyInstaller that is the unpacked bundle; otherwise it is this package
# directory, which covers both an editable install and site-packages.
_BUNDLE_ROOT = getattr(sys, "_MEIPASS", None)


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than a source tree."""
    return _BUNDLE_ROOT is not None


def resource_path(*parts: str) -> str:
    """Absolute path to a resource shipped alongside the package.

    Covers `gui/app.py` — which `streamlit run` needs as a *path*, not an
    importable name — and `gui/assets/banner.svg`. Under a frozen build both are
    `--add-data` entries landing under `_MEIPASS/flatscorer/`, which is why the
    package name is prepended there and not here.
    """
    if _BUNDLE_ROOT is not None:
        return os.path.join(_BUNDLE_ROOT, "flatscorer", *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def cache_dir() -> str:
    """Directory for the osmnx HTTP cache.

    Per-user, not per-working-directory. osmnx's own default is `./cache`, which
    means the cache is only reused when you happen to run from the same place —
    and it drops a directory into whatever you were standing in. A downloaded
    street network is expensive and identical wherever you ask for it, so it
    belongs somewhere shared.

    osmnx creates this itself on first write, so nothing here has to.
    """
    return platformdirs.user_cache_dir(APP_NAME)


def output_dir() -> str:
    """Directory the score table and map are written into.

    Relative to the working directory, which is the right default for a CLI:
    results are about the run you just did, so they land where you are. `output/`
    rather than the bare directory only so a run doesn't scatter files into a
    checkout or a home directory.
    """
    return "output"


def output_path(filename: str) -> str:
    """Where a generated file goes, when the config doesn't say."""
    return os.path.normpath(os.path.join(output_dir(), filename))


def ensure_parent(path: str) -> str:
    """Create the directory `path` will be written into, and return `path`.

    Needed because `output_dir()` is no longer guaranteed to exist. Applied to
    whatever path is actually being written, not just the default, so an explicit
    `output.csv_file` pointing somewhere new works too rather than failing at the
    very end of a multi-minute run.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    return path
