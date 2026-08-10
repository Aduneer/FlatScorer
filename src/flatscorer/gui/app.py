"""FlatScorer GUI — the Streamlit front end.

Build a config visually (candidates, destinations, weights, parameters), run the
scorer, and view the ranked table plus interactive map without touching JSON or
the command line. This is a *second entry point to the same engine*: it assembles
a config dict and calls `FlatScorer(config).run()` exactly as the CLI does, and
never reimplements scoring.

Usage:
    flatscorer-gui                  # installed
    streamlit run src/flatscorer/gui/app.py

## Three rules for this file specifically

**Absolute imports only.** `streamlit run` takes a *path*, and Streamlit `exec`s
the file with `__name__ == "__main__"` and no package context, so a relative
import here raises ImportError. Every module under `flatscorer/gui/` uses
absolute imports for the same reason: it makes moving code in or out of this
file safe. This is also the one file the eventual PyInstaller build ships as a
data file rather than a frozen module.

**Keep it thin.** Streamlit re-executes this script top to bottom on every single
interaction, while imported modules are cached. Logic put here is paid for on
every click; logic in a view module is paid for once.

**Never put a directory called `pages/` next to this file.** That is Streamlit's
multipage convention: it auto-discovers `pages/*.py` and renders its own nav
links in the sidebar, which appeared *underneath* our radio as a duplicate menu.
The view modules live in `views/` for exactly this reason, and `test_api.py`
fails if a `pages/` directory reappears.
"""

from __future__ import annotations

import os
import sys

# Make `streamlit run path/to/app.py` work from a checkout where the package was
# never installed. Streamlit puts *this* file's directory on sys.path, not the
# `src/` root three levels up, so `import flatscorer` would otherwise fail with a
# message that tells the user nothing about what to do. This cannot go through
# `paths` — that would need the import this is here to make possible.
if "flatscorer" not in sys.modules:
    try:
        import flatscorer  # noqa: F401
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import streamlit as st

st.set_page_config(
    page_title="FlatScorer — Apartment Scoring Tool",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded"
)

from flatscorer.gui import sidebar, state, theme  # noqa: E402  - must follow set_page_config
from flatscorer.gui.views import candidates, destinations, run, weights  # noqa: E402

# Keyed by the nav label `sidebar.NAV_OPTIONS` offers.
PAGES = {
    "🏠": candidates,
    "📍": destinations,
    "⚖️": weights,
    "🚀": run,
}

HEADER_HTML = """
<div class="fs-header">
    <div class="fs-title">FlatScorer</div>
    <div class="fs-subtitle">multi-criteria apartment scoring &middot; openstreetmap data</div>
    <div class="fs-tags">
        <span class="fs-tag-python">python</span>
        <span class="fs-tag-sep">&middot;</span>
        <span class="fs-tag-osm">osm</span>
        <span class="fs-tag-sep">&middot;</span>
        <span class="fs-tag-gis">gis</span>
        <span class="fs-tag-sep">&middot;</span>
        <span class="fs-tag-cli">cli &amp; gui</span>
    </div>
</div>
"""


def main():
    state._init_state()
    theme.inject()

    slots = sidebar.render()

    st.markdown(HEADER_HTML, unsafe_allow_html=True)

    page = next(mod for prefix, mod in PAGES.items() if slots.nav.startswith(prefix))
    page.render()

    # Last, always: these read state the page body just wrote. See sidebar.fill.
    sidebar.fill(slots)


main()
