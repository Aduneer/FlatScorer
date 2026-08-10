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

    Relative to the working directory, matching osmnx's own default, which is
    what this returned implicitly before it was set explicitly.
    """
    return "cache"


def output_dir() -> str:
    """Directory the score table and map are written into."""
    return "."


def output_path(filename: str) -> str:
    """Where a generated file goes, when the config doesn't say.

    `normpath` is what keeps this byte-identical to the bare filenames that were
    hard-coded before: joining "." with "apartment_scores.csv" and normalizing
    gives the name back unchanged, while a real `output_dir()` prefixes it.
    """
    return os.path.normpath(os.path.join(output_dir(), filename))
