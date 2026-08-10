"""The Candidates page: the apartments being compared."""

from __future__ import annotations

import streamlit as st

from flatscorer.gui.state import (
    LISTING_URL_DISPLAY,
    _editor_baseline,
)


def render():
    st.markdown(
        """
        <div class="fs-card">
            <div class="fs-card-title">🏠 Candidate Apartments</div>
            <div class="fs-card-desc">
                Enter the apartments you are evaluating. Use full, precise addresses (street name, house number, postal code, city, country) for optimal geocoding accuracy.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.session_state.candidates_df = st.data_editor(
        _editor_baseline("candidates_df", "candidates_editor"),
        num_rows="dynamic",
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Apartment Name / ID", required=True),
            "address": st.column_config.TextColumn("Full Address", required=True, width="large"),
            "rent": st.column_config.NumberColumn("Rent (€/month)", min_value=0, step=50, format="%d €"),
            "url": st.column_config.LinkColumn(
                "Listing",
                display_text=LISTING_URL_DISPLAY,
                validate=r"^https?://.+",
                width="medium",
                help="Optional link to the listing you found this flat on. It never affects "
                     "the score — it rides along into the CSV and onto the map popup so you "
                     "can click straight through from a result.",
            ),
        },
        key="candidates_editor",
    ).reset_index(drop=True)


