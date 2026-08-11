"""The Weights & Parameters page: what the score is made of."""

from __future__ import annotations

import copy

import pandas as pd
import streamlit as st

from flatscorer.gui.state import (
    DEFAULT_PARAMS,
)
from flatscorer.gui.widgets import MODE_EMOJI
from flatscorer.routing import DEFAULT_TRAVEL_MODE, TRAVEL_MODES
from flatscorer.scoring import (
    DEFAULT_DEST_WEIGHT,
    SCORE_SCALE_MAX,
    weight_shares,
)


def render():
    st.markdown(
        """
        <div class="fs-card">
            <div class="fs-card-title">⚖️ Scoring Weights</div>
            <div class="fs-card-desc">
                Adjust how much each factor matters. Only the <em>relative</em> sizes count —
                every metric is normalized to 0–1 before weighting, and the weights are
                rescaled to sum to 100%, so doubling every slider changes nothing.
                The influence table below shows what each weight is actually worth.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    weight_labels = {
        "supermarket": "🛒 Supermarkets",
        "bakery": "🥖 Bakeries",
        "pharmacy": "💊 Pharmacies",
        "gym": "🏋️ Gyms",
        "transit": "🚌 Transit stops",
        "green": "🌳 Green space",
        "noise": "🤫 Quiet (dist from busy roads)",
        "rent": "💶 Rent penalty",
    }

    w_cols = st.columns(4)
    for i, (key, label) in enumerate(weight_labels.items()):
        with w_cols[i % 4]:
            st.session_state.weights[key] = st.slider(
                label,
                min_value=0.0,
                max_value=1.0,
                value=float(st.session_state.weights.get(key, 0.1)),
                step=0.01,
                key=f"weight_{key}",
            )

    with st.expander("🎚️ Amenity saturation — how fast each count stops helping", expanded=False):
        st.caption(
            "The count that earns half credit. Amenities have diminishing returns: with "
            "a half-credit point of 2, the first supermarket is worth far more than the sixth. "
            "Lower = easier to satisfy."
        )
        sat = st.session_state.params.setdefault("saturation", copy.deepcopy(DEFAULT_PARAMS["saturation"]))
        sat_labels = {
            "supermarket": "🛒 Supermarkets",
            "bakery": "🥖 Bakeries",
            "pharmacy": "💊 Pharmacies",
            "gym": "🏋️ Gyms",
            "transit": "🚌 Transit stops",
            "green": "🌳 Green score (m²/1000 + 0.5/point)",
        }
        s_cols = st.columns(3)
        for i, (key, label) in enumerate(sat_labels.items()):
            with s_cols[i % 3]:
                sat[key] = st.number_input(
                    label,
                    min_value=0.1,
                    value=float(sat.get(key, DEFAULT_PARAMS["saturation"][key])),
                    step=0.5,
                    key=f"saturation_{key}",
                )

    # Weights only act relative to one another, and destination weights compete in
    # the same pool — so a slider's number on its own says nothing about influence.
    combined = {key: float(st.session_state.weights.get(key, 0.0)) for key in weight_labels}
    dest_labels = {}
    for _, row in st.session_state.destinations_df.iterrows():
        dest_name = str(row.get("name", "")).strip()
        if not dest_name:
            continue
        combined[f"dest_{dest_name}"] = float(row.get("weight", DEFAULT_DEST_WEIGHT) or 0.0)
        mode = row.get("mode") if row.get("mode") in TRAVEL_MODES else DEFAULT_TRAVEL_MODE
        dest_labels[f"dest_{dest_name}"] = f"{MODE_EMOJI.get(mode, '🚶')} Commute to {dest_name}"

    shares = weight_shares(combined)
    if sum(combined.values()) <= 0:
        st.warning("⚠️ Every weight is zero — with nothing to weigh, all candidates score 0.")
    else:
        st.markdown("#### Influence share")
        st.caption(
            f"Each factor's share of the total weight — i.e. the most points out of "
            f"{SCORE_SCALE_MAX:.0f} it can contribute. Destination weights (set on the "
            "Destinations page) compete in the same pool."
        )
        share_df = pd.DataFrame(
            [
                {
                    "Factor": {**weight_labels, **dest_labels}[key],
                    "Weight": round(combined[key], 3),
                    "Share": shares[key],
                    "Max points": round(shares[key] * SCORE_SCALE_MAX, 2),
                }
                for key in sorted(combined, key=lambda k: -shares[k])
            ]
        )
        st.dataframe(
            share_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Share": st.column_config.ProgressColumn(
                    "Share of score", format="%.1f%%", min_value=0.0, max_value=float(max(shares.values())),
                ),
            },
        )

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    st.markdown(
        """
        <div class="fs-card">
            <div class="fs-card-title">⚙️ Scoring Anchors & Spatial Parameters</div>
            <div class="fs-card-desc">
                These set what "full marks" means for each metric. They are absolute,
                not relative to the candidate set, which is what keeps a score
                comparable between runs — change one and every score moves.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        st.session_state.params["rent_budget_eur"] = st.number_input(
            "Rent budget (€/month)",
            min_value=1,
            value=int(st.session_state.params.get("rent_budget_eur", DEFAULT_PARAMS["rent_budget_eur"])),
            step=50,
            help="Rent at or above this scores 0 on the rent term; free rent scores 1. Set it to the most you would pay.",
        )
    with p2:
        st.session_state.params["commute_cap_min"] = st.number_input(
            "Commute cap (min)",
            min_value=1,
            value=int(st.session_state.params.get("commute_cap_min", DEFAULT_PARAMS["commute_cap_min"])),
            step=5,
            help="A walk this long (or longer) to a destination scores 0; arriving instantly scores 1.",
        )
    with p3:
        st.session_state.params["buffer_m"] = st.number_input(
            "Amenity radius (m)",
            min_value=50,
            value=int(st.session_state.params.get("buffer_m", 500)),
            step=50,
        )
    with p4:
        st.session_state.params["noise_cap_m"] = st.number_input(
            "Noise benefit cap (m)",
            min_value=50,
            value=int(st.session_state.params.get("noise_cap_m", 200)),
            step=50,
            help="Distance from a busy road at which the quiet term maxes out. Raising it no longer inflates noise's influence — the term is scaled by the cap.",
        )

    p5, p6, p7, p8 = st.columns([2, 1, 1, 1])
    with p5:
        st.session_state.params["projected_crs"] = st.text_input(
            "Projected CRS",
            value=st.session_state.params.get("projected_crs", "auto"),
            help="'auto' picks the correct UTM zone, or provide EPSG code (e.g. EPSG:25832).",
        )
    with p6:
        st.session_state.params["max_bbox_span_km"] = st.number_input(
            "Max search span (km)",
            min_value=1,
            value=int(st.session_state.params.get("max_bbox_span_km", DEFAULT_PARAMS["max_bbox_span_km"])),
            step=5,
            help="Refuses the OpenStreetMap download if the addresses spread further apart than this — which almost always means one of them geocoded to the wrong city. Raise it for a genuinely region-wide search.",
        )
    with p7:
        walking_speed = st.number_input(
            "Walking speed (m/min)",
            min_value=1.0,
            value=float(st.session_state.params.get(
                "walking_speed_m_per_min", DEFAULT_PARAMS["walking_speed_m_per_min"])),
            step=5.0,
            help="Turns routed distance into the minutes the commute cap is measured against. Lower it and every commute term drops, so it only means anything alongside the cap above.",
        )
        st.session_state.params["walking_speed_m_per_min"] = walking_speed
        st.caption(f"≈ {walking_speed * 60 / 1000:.1f} km/h")
    with p8:
        cycling_speed = st.number_input(
            "Cycling speed (m/min)",
            min_value=1.0,
            value=float(st.session_state.params.get(
                "cycling_speed_m_per_min", DEFAULT_PARAMS["cycling_speed_m_per_min"])),
            step=10.0,
            help="Pace used for destinations set to 'bike' on the Destinations page. The default "
                 "250 m/min (15 km/h) is urban cycling including junctions and locking up, not "
                 "open-road speed.",
        )
        st.session_state.params["cycling_speed_m_per_min"] = cycling_speed
        st.caption(f"≈ {cycling_speed * 60 / 1000:.1f} km/h")

    st.session_state.params["show_walk_routes"] = st.checkbox(
        "Show predicted commute routes on map by default",
        value=bool(st.session_state.params.get("show_walk_routes", True)),
        help="Draws each candidate's shortest path to every destination on the map, over that destination's own network. "
             "Cycled legs are dashed. Always toggleable via the map's layer control. "
             "Has no effect in fast mode, which measures no routes to draw.",
    )

    st.session_state.params["nominatim_url"] = st.text_input(
        "Geocoding service (Nominatim)",
        value=st.session_state.params.get("nominatim_url", DEFAULT_PARAMS["nominatim_url"]),
        help="Where addresses are turned into coordinates. The public OpenStreetMap instance is "
             "the default; point this at your own Nominatim if you run one. It is configurable "
             "because Nominatim's usage policy requires that the service can be switched without "
             "shipping a new version of the software.",
    )

    st.divider()
    st.markdown("**How commutes are measured**")
    r1, r2 = st.columns(2)
    with r1:
        approximate = st.checkbox(
            "Fast mode (estimate commutes, skip the street-network download)",
            value=st.session_state.params.get("routing_mode", DEFAULT_PARAMS["routing_mode"]) == "straight_line",
            help="Estimates each commute from straight-line distance instead of routing over a downloaded "
                 "street network. That download is the slowest step of a run by a wide margin, so this turns "
                 "minutes into seconds. Measured across three cities the ranking barely moves - the top pick "
                 "never changed - but the minutes themselves carry a few percent of error and are labelled "
                 "'approx.' everywhere they appear.",
        )
        st.session_state.params["routing_mode"] = "straight_line" if approximate else "network"
    with r2:
        st.session_state.params["detour_factor"] = st.number_input(
            "Detour factor",
            min_value=1.0,
            value=float(st.session_state.params.get("detour_factor", DEFAULT_PARAMS["detour_factor"])),
            step=0.05,
            disabled=not approximate,
            help="How much longer a real route is than the straight line. It does not affect the ranking - a "
                 "constant cannot reorder anything - so it only decides whether the displayed minutes are "
                 "honest and which candidates fall past the commute cap. Measured city medians ran 1.21 to 1.35.",
        )


