"""The Destinations page: the places each flat is scored against."""

from __future__ import annotations

import streamlit as st

from flatscorer.gui.state import (
    _editor_baseline,
)
from flatscorer.gui.widgets import COLOR_CHOICES, ICON_CHOICES, MODE_CHOICES


def render():
    st.markdown(
        """
        <div class="fs-card">
            <div class="fs-card-title">📍 Commute Destinations</div>
            <div class="fs-card-desc">
                Places you frequently travel to (e.g. work, university, gym). Each destination incurs a travel-time penalty scaled by its weight,
                routed over the real network for its travel mode — walking or cycling. Mixing modes adds one extra OpenStreetMap download.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.session_state.destinations_df = st.data_editor(
        _editor_baseline("destinations_df", "destinations_editor"),
        num_rows="dynamic",
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Destination Name", required=True),
            "address": st.column_config.TextColumn("Address", required=True, width="large"),
            "weight": st.column_config.NumberColumn(
                "Importance Weight", min_value=0.0, max_value=2.0, step=0.05,
                help="Relative importance of this commute, competing in the same pool as the "
                     "amenity weights. See the influence table on the Weights page.",
            ),
            "mode": st.column_config.SelectboxColumn(
                "Travel Mode", options=MODE_CHOICES, required=True,
                help="How you get there. Each mode routes over its own street network at its own "
                     "pace (set on the Weights & Parameters page) — a 35-minute walk is often a "
                     "12-minute cycle. Using both modes costs one extra OpenStreetMap download.",
            ),
            "icon": st.column_config.SelectboxColumn("Map Icon", options=ICON_CHOICES),
            "color": st.column_config.SelectboxColumn("Map Color", options=COLOR_CHOICES),
        },
        key="destinations_editor",
    ).reset_index(drop=True)


