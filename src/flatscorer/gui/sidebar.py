"""The sidebar: navigation, config import/export, and two deferred slots.

The deferral is the whole reason this module exists as its own thing. The
sidebar renders *before* the page body, but both the candidate/destination count
and the "Download config.json" payload are read out of `st.session_state` —
which `st.data_editor` only assigns as the page body runs. Rendering them in
place therefore showed the *previous* rerun's data: the count lagged an edit
behind, and a config exported right after an edit was missing it.

`st.empty()` fixes that without moving anything on screen: claim the slot where
it belongs, fill it once the page has run. `render()` returns the claims and
`fill()` consumes them, so the ordering is a signature rather than a convention
held up by a comment. Anything else added here that reads editor state belongs
in `fill()` too.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import streamlit as st

from flatscorer import paths
from flatscorer.config import DEFAULT_CONFIG
from flatscorer.gui.state import _build_config, _load_config_into_state

NAV_OPTIONS = [
    "🏠 Candidates",
    "📍 Destinations",
    "⚖️ Weights & Parameters",
    "🚀 Run & Results",
]


@dataclass
class Slots:
    """What `render()` decided, plus the slots `fill()` still owes the sidebar."""

    nav: str
    stat_pill: Any
    config_download: Any


def render() -> Slots:
    """Draw the sidebar, reserving the two slots that depend on the page body."""
    with st.sidebar:
        banner = paths.resource_path("gui", "assets", "banner.svg")
        if os.path.exists(banner):
            st.image(banner, width="stretch")

        st.markdown("### Navigation")

        selected_nav = st.radio(
            label="Select Page",
            options=NAV_OPTIONS,
            index=0,
            label_visibility="collapsed",
            key="main_nav_radio",
        )

        stat_pill_slot = st.empty()

        st.markdown("---")

        with st.expander("📁 Import / Export Config", expanded=False):
            uploaded = st.file_uploader("Load config.json", type="json")
            if uploaded is not None:
                try:
                    _load_config_into_state(json.load(uploaded))
                    st.success("Config loaded successfully.")
                except Exception as e:  # noqa: BLE001 - config upload can fail with any JSON/schema error; report it in the UI
                    st.error(f"Couldn't parse JSON file: {e}")

            if st.button("Reset to Demo Data (DC)", width="stretch"):
                _load_config_into_state(DEFAULT_CONFIG)
                st.rerun()

            st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)
            config_download_slot = st.empty()

        # The "this takes a few minutes" half of this note used to live here and
        # was routinely missed down in the corner. It now sits beside the Run
        # button, with a progress bar underneath it doing the actual reassuring.
        st.caption(
            "FlatScorer geocodes via Nominatim and pulls OSM data via Overpass, "
            "under those services' usage policies."
        )

    return Slots(nav=selected_nav, stat_pill=stat_pill_slot,
                 config_download=config_download_slot)


def fill(slots: Slots):
    """Render into the reserved slots. Must run after the page body, not before.

    `download_button` is handed its payload at *render* time, so calling
    `_build_config()` this late is the entire fix for the stale-export bug.
    Both of these are pinned by tests; keep them last.
    """
    slots.stat_pill.markdown(
        f"""
        <div class="fs-stat-pill">
            <div><strong>Candidates:</strong> {len(st.session_state.candidates_df)} loaded</div>
            <div><strong>Destinations:</strong> {len(st.session_state.destinations_df)} loaded</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    slots.config_download.download_button(
        "Download config.json",
        data=json.dumps(_build_config(), indent=2, ensure_ascii=False),
        file_name="config.json",
        mime="application/json",
        width="stretch",
    )
