#!/usr/bin/env python3
"""
FlatScorer GUI — a Streamlit front end for FlatScorer.py.

Lets you build a config visually (candidates, destinations, weights,
parameters), run the scorer, and view the ranked table + interactive map
without touching JSON or the command line.

Usage:
    pip install -r requirements-gui.txt
    streamlit run streamlit_app.py

This file only *calls into* FlatScorer.py — it does not duplicate or
modify any scoring logic, so behavior always matches the CLI tool exactly.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
from typing import Any

import pandas as pd
import streamlit as st

from FlatScorer import (
    DEFAULT_CONFIG,
    DEFAULT_DEST_WEIGHT,
    DEFAULT_TRAVEL_MODE,
    NARROW_MARGIN_THRESHOLD,
    SCORE_SCALE_MAX,
    TRAVEL_MODES,
    FlatScorer,
    SearchAreaError,
    validate_config,
    weight_shares,
)

st.set_page_config(
    page_title="FlatScorer — Apartment Scoring Tool",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---------------------------------------------------------------- Constants --

ICON_CHOICES = [
    "home", "briefcase", "landmark", "train", "university", "hospital-o",
    "shopping-cart", "graduation-cap", "subway", "bus", "tree", "star",
]
COLOR_CHOICES = [
    "blue", "red", "green", "orange", "purple", "darkred", "darkblue",
    "darkgreen", "cadetblue", "darkpurple", "pink", "lightblue",
    "lightgreen", "gray", "black",
]

DEFAULT_WEIGHTS = DEFAULT_CONFIG["weights"]
DEFAULT_PARAMS = DEFAULT_CONFIG["parameters"]

# Travel modes come from the engine rather than a literal here, so the dropdown
# can never offer a mode validate_config would reject.
MODE_CHOICES = list(TRAVEL_MODES)
MODE_EMOJI = {"walk": "🚶", "bike": "🚴"}


# ----------------------------------------------------------- Helper Functions --

def _init_state():
    """Seed session_state with the built-in demo config on first load."""
    if "candidates_df" in st.session_state:
        return

    st.session_state.candidates_df = pd.DataFrame(DEFAULT_CONFIG["candidates"])

    dest_rows = []
    for name, info in DEFAULT_CONFIG["destinations"].items():
        dest_rows.append({
            "name": name,
            "address": info["address"],
            "weight": info["weight"],
            "mode": info.get("mode", DEFAULT_TRAVEL_MODE),
            "icon": info.get("icon", "star"),
            "color": info.get("color", "blue"),
        })
    st.session_state.destinations_df = pd.DataFrame(dest_rows)

    st.session_state.weights = dict(DEFAULT_WEIGHTS)
    # Deep copy: `params` nests the saturation dict, and a shallow copy would let
    # the widgets edit the module-level default in place.
    st.session_state.params = copy.deepcopy(DEFAULT_PARAMS)


def _editor_baseline(state_key: str, editor_key: str) -> pd.DataFrame:
    """Return the stable frame to hand `st.data_editor` as its baseline.

    `st.data_editor` tracks edits as a delta against the exact frame it was
    given, so that frame has to stay identical for as long as the widget stays
    mounted — assigning the edited result back over it desyncs the delta and
    makes entries revert (streamlit/streamlit#7354). The widget key is absent
    only on a fresh mount, since Streamlit drops widget state whenever the
    widget isn't rendered (e.g. while another nav page is open), so that is the
    one safe moment to adopt the latest edited data as the new baseline.
    """
    baseline_key = f"{state_key}_baseline"
    if editor_key not in st.session_state:
        st.session_state[baseline_key] = st.session_state[state_key].reset_index(drop=True)
    return st.session_state[baseline_key]


def _reset_editor_state():
    """Drop editor deltas so a freshly loaded config isn't re-edited by stale ones."""
    for key in ("candidates_editor", "destinations_editor",
                "candidates_df_baseline", "destinations_df_baseline"):
        if key in st.session_state:
            del st.session_state[key]


def _load_config_into_state(config: dict[str, Any]):
    _reset_editor_state()
    st.session_state.candidates_df = pd.DataFrame(config.get("candidates", []))

    dest_rows = []
    for name, info in config.get("destinations", {}).items():
        dest_rows.append({
            "name": name,
            "address": info.get("address", ""),
            "weight": info.get("weight", 0.15),
            "mode": info.get("mode", DEFAULT_TRAVEL_MODE),
            "icon": info.get("icon", "star"),
            "color": info.get("color", "blue"),
        })
    st.session_state.destinations_df = pd.DataFrame(dest_rows)

    st.session_state.weights = dict(DEFAULT_WEIGHTS, **config.get("weights", {}))
    loaded_params = config.get("parameters", {})
    params = copy.deepcopy(DEFAULT_PARAMS) | {k: v for k, v in loaded_params.items() if k != "saturation"}
    # Merge saturation rather than replacing it, so a config that overrides one
    # half-credit point doesn't drop the defaults for the other five.
    params["saturation"] = dict(DEFAULT_PARAMS["saturation"], **loaded_params.get("saturation", {}))
    st.session_state.params = params


def _build_config() -> dict[str, Any]:
    """Assemble a FlatScorer-compatible config dict from the current form state."""
    candidates = []
    for _, row in st.session_state.candidates_df.iterrows():
        if not str(row.get("name", "")).strip() or not str(row.get("address", "")).strip():
            continue
        # A cleared rent cell arrives as NaN, which is truthy - so `or 0` misses it
        # and validate_config would report the cryptic "got nan" instead of the
        # friendlier "rent is 0, enter the actual monthly rent".
        rent = pd.to_numeric(row.get("rent", 0), errors="coerce")
        candidates.append({
            "name": row["name"],
            "address": row["address"],
            "rent": 0.0 if pd.isna(rent) else float(rent),
        })

    destinations = {}
    for _, row in st.session_state.destinations_df.iterrows():
        if not str(row.get("name", "")).strip() or not str(row.get("address", "")).strip():
            continue
        destinations[row["name"]] = {
            "address": row["address"],
            "weight": float(row.get("weight", 0.15) or 0.15),
            # A cleared mode cell arrives as None/NaN; fall back rather than
            # writing a mode the engine would reject.
            "mode": row.get("mode") if row.get("mode") in TRAVEL_MODES else DEFAULT_TRAVEL_MODE,
            "icon": row.get("icon", "star") or "star",
            "color": row.get("color", "blue") or "blue",
        }

    return {
        "candidates": candidates,
        "destinations": destinations,
        "weights": dict(st.session_state.weights),
        "parameters": dict(st.session_state.params),
        "output": {"csv_file": "apartment_scores.csv", "html_file": "apartment_map.html"},
    }


# ------------------------------------------------------------- Custom CSS Theme --

def _inject_custom_theme():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap');

        /* Light theme (default) — Field Guide: a cartographer's notebook, paper variant */
        :root {
            --paper: #f6f1e4;
            --card: #fffdf7;
            --sidebar: #efe8d4;
            --inset: #ece3cc;
            --ink: #3a3226;
            --ink-muted: #7a6f5c;
            --ink-on-primary: #f6f1e4;
            --pine: #2f5233;
            --pine-hover: #1f3a22;
            --pine-rgb: 47, 82, 51;
            --rust: #b5651d;
            --rust-hover: #8f4d15;
            --rust-rgb: 181, 101, 29;
            --moss: #6b7a3f;
            --border: #ddd2b4;
            --border-strong: #c4b596;
            --watermark-compass: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'%3E%3Cg stroke='%232f5233' stroke-width='1.2' fill='none'%3E%3Ccircle cx='120' cy='120' r='66' stroke-opacity='0.16'/%3E%3Ccircle cx='120' cy='120' r='40' stroke-width='0.9' stroke-opacity='0.12'/%3E%3Cline x1='120' y1='54' x2='120' y2='40' stroke-opacity='0.16'/%3E%3Cline x1='186' y1='120' x2='200' y2='120' stroke-opacity='0.16'/%3E%3Cline x1='120' y1='186' x2='120' y2='200' stroke-opacity='0.16'/%3E%3Cline x1='54' y1='120' x2='40' y2='120' stroke-opacity='0.16'/%3E%3C/g%3E%3Cpath d='M120,84 L132,120 L108,120 Z' fill='%23b5651d' fill-opacity='0.14'/%3E%3Cpath d='M108,120 L132,120 L120,156 Z' fill='%232f5233' fill-opacity='0.14'/%3E%3C/svg%3E");
            --watermark-route: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60' viewBox='0 0 60 60'%3E%3Cpath d='M-10,50 L20,20 M10,70 L50,30 M40,80 L80,40' stroke='%232f5233' stroke-width='1.1' stroke-dasharray='1.5 4.5' stroke-linecap='round' stroke-opacity='0.22'/%3E%3C/svg%3E");
        }

        /* Dark theme — the same field guide, read by lantern light */
        @media (prefers-color-scheme: dark) {
            :root {
                --paper: #1c1810;
                --card: #262116;
                --sidebar: #201b10;
                --inset: #2e2818;
                --ink: #f0e6d2;
                --ink-muted: #b8ab8c;
                --ink-on-primary: #1c1810;
                --pine: #6fae74;
                --pine-hover: #8fc794;
                --pine-rgb: 111, 174, 116;
                --rust: #e0894a;
                --rust-hover: #f0a468;
                --rust-rgb: 224, 137, 74;
                --moss: #9db56a;
                --border: #3a3225;
                --border-strong: #4a4030;
                --watermark-compass: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'%3E%3Cg stroke='%236fae74' stroke-width='1.2' fill='none'%3E%3Ccircle cx='120' cy='120' r='66' stroke-opacity='0.18'/%3E%3Ccircle cx='120' cy='120' r='40' stroke-width='0.9' stroke-opacity='0.14'/%3E%3Cline x1='120' y1='54' x2='120' y2='40' stroke-opacity='0.18'/%3E%3Cline x1='186' y1='120' x2='200' y2='120' stroke-opacity='0.18'/%3E%3Cline x1='120' y1='186' x2='120' y2='200' stroke-opacity='0.18'/%3E%3Cline x1='54' y1='120' x2='40' y2='120' stroke-opacity='0.18'/%3E%3C/g%3E%3Cpath d='M120,84 L132,120 L108,120 Z' fill='%23e0894a' fill-opacity='0.16'/%3E%3Cpath d='M108,120 L132,120 L120,156 Z' fill='%236fae74' fill-opacity='0.16'/%3E%3C/svg%3E");
                --watermark-route: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60' viewBox='0 0 60 60'%3E%3Cpath d='M-10,50 L20,20 M10,70 L50,30 M40,80 L80,40' stroke='%236fae74' stroke-width='1.1' stroke-dasharray='1.5 4.5' stroke-linecap='round' stroke-opacity='0.24'/%3E%3C/svg%3E");
            }
        }

        /* Belt-and-suspenders base layer: Streamlit's own theme.light/theme.dark
           resolution doesn't reliably reach every native chrome element (notably
           the top header/toolbar), so pin the raw document background too —
           this is what was showing through as a stubborn light strip. */
        html, body {
            background-color: var(--paper) !important;
        }

        /* Main container background — faint trail hatching + a compass rose watermark */
        .stApp {
            background-color: var(--paper);
            background-image: var(--watermark-route), var(--watermark-compass);
            background-repeat: repeat, no-repeat;
            background-position: top left, bottom -60px right -60px;
            background-size: 60px 60px, 420px 420px;
            background-attachment: fixed, fixed;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            color: var(--ink);
        }

        /* Force the native header/toolbar bar (hamburger menu, "Deploy", the
           "..." menu) to follow our palette directly — Streamlit's dual-theme
           feature (added in 1.51) doesn't consistently repaint this bar itself,
           so we don't rely on it here at all. */
        [data-testid="stHeader"],
        [data-testid="stAppHeader"],
        [data-testid="stToolbar"],
        [data-testid="stAppToolbar"],
        [data-testid="stToolbarActions"],
        [data-testid="stDecoration"] {
            background-color: var(--paper) !important;
            background-image: none !important;
        }

        [data-testid="stHeader"] *,
        [data-testid="stAppHeader"] *,
        [data-testid="stToolbar"] *,
        [data-testid="stAppToolbar"] *,
        [data-testid="stToolbarActions"] * {
            color: var(--ink) !important;
        }

        [data-testid="stHeader"] svg,
        [data-testid="stAppHeader"] svg,
        [data-testid="stToolbar"] svg,
        [data-testid="stAppToolbar"] svg,
        [data-testid="stToolbarActions"] svg {
            fill: currentColor !important;
        }

        /* Sidebar Styling */
        [data-testid="stSidebar"] {
            background-color: var(--sidebar);
            background-image: var(--watermark-route);
            background-repeat: repeat;
            background-size: 60px 60px;
            border-right: 1px solid var(--border);
        }

        /* Widen sidebar to comfortably fit larger navigation tabs */
        [data-testid="stSidebar"] > div:first-child {
            min-width: 340px;
            max-width: 340px;
        }

        /* Prominent Sidebar Navigation Radio Buttons ("tabs") */
        [data-testid="stSidebar"] div[role="radiogroup"] {
            gap: 14px;
            padding: 8px 0;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] > label {
            background-color: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 22px 22px;
            margin: 0;
            transition: all 0.2s ease-in-out;
            cursor: pointer;
            width: 100%;
            min-height: 64px;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
            border-color: var(--pine);
            background-color: var(--inset);
            transform: translateX(2px);
            box-shadow: 0 3px 10px rgba(var(--pine-rgb), 0.15);
        }

        /* Selected State for Sidebar Tabs */
        [data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] {
            background-color: var(--pine) !important;
            border-color: var(--pine-hover) !important;
            box-shadow: 0 4px 12px rgba(var(--pine-rgb), 0.3) !important;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] p,
        [data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] span {
            color: var(--ink-on-primary) !important;
            font-weight: 700 !important;
            font-size: 1.35rem !important;
        }

        [data-testid="stSidebar"] div[role="radiogroup"] > label p,
        [data-testid="stSidebar"] div[role="radiogroup"] > label span {
            font-size: 1.3rem !important;
            font-weight: 600;
            color: var(--ink);
            margin: 0;
            line-height: 1.3;
        }

        /* Hide standard small radio circle icon inside sidebar nav, without
           touching the label text/emoji that shares its wrapper container */
        [data-testid="stSidebar"] div[role="radiogroup"] label div:has(+ [data-testid="stMarkdownContainer"]) {
            display: none !important;
        }

        /* Header typography matching SVG Banner */
        .fs-header {
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }

        .fs-title {
            font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
            font-weight: 600;
            font-size: 2.6rem;
            color: var(--ink);
            letter-spacing: -0.5px;
            margin: 0;
            line-height: 1.1;
        }

        .fs-subtitle {
            font-family: 'Space Mono', monospace;
            font-size: 0.88rem;
            color: var(--ink-muted);
            margin-top: 6px;
            margin-bottom: 12px;
        }

        .fs-tags {
            font-family: 'Space Mono', monospace;
            font-size: 0.85rem;
        }

        .fs-tag-python { color: var(--pine); font-weight: 600; }
        .fs-tag-osm { color: var(--moss); font-weight: 600; }
        .fs-tag-gis { color: var(--rust); font-weight: 600; }
        .fs-tag-cli { color: var(--ink-muted); font-weight: 600; }
        .fs-tag-sep { color: var(--border-strong); margin: 0 6px; }

        /* Card Container */
        .fs-card {
            background-color: var(--card);
            border: 1px solid var(--border);
            border-left: 3px solid var(--pine);
            border-radius: 10px;
            padding: 22px 26px;
            margin-bottom: 24px;
        }

        .fs-card-title {
            font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
            font-size: 1.3rem;
            font-weight: 600;
            color: var(--ink);
            margin-bottom: 8px;
        }

        .fs-card-desc {
            font-size: 0.92rem;
            color: var(--ink-muted);
            margin-bottom: 16px;
        }

        /* Buttons Styling */
        div.stButton > button[kind="primary"] {
            background-color: var(--pine) !important;
            color: var(--ink-on-primary) !important;
            border: 1px solid var(--pine-hover) !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            padding: 10px 24px !important;
            transition: all 0.2s ease !important;
            box-shadow: 0 4px 10px rgba(var(--pine-rgb), 0.25) !important;
        }

        div.stButton > button[kind="primary"]:hover {
            background-color: var(--pine-hover) !important;
            color: var(--ink-on-primary) !important;
            transform: translateY(-1px) !important;
            box-shadow: 0 6px 14px rgba(var(--pine-rgb), 0.4) !important;
        }

        div.stDownloadButton > button {
            border: 1px solid var(--border-strong) !important;
            border-radius: 8px !important;
            background-color: var(--card) !important;
            color: var(--ink) !important;
            font-weight: 600 !important;
            transition: all 0.2s ease !important;
        }

        div.stDownloadButton > button:hover {
            border-color: var(--pine) !important;
            color: var(--pine) !important;
            background-color: var(--inset) !important;
        }

        /* Stat pill badge */
        .fs-stat-pill {
            background-color: var(--inset);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            font-family: 'Space Mono', monospace;
            font-size: 0.85rem;
            color: var(--ink-muted);
            margin-bottom: 16px;
        }

        .fs-stat-pill strong {
            color: var(--ink);
        }

        /* Winner panel — a rubber-stamped "top pick", like a passport stamp */
        .fs-winner-panel {
            display: flex;
            align-items: center;
            gap: 20px;
            background-color: var(--card);
            border: 1px dashed var(--border-strong);
            border-left: 3px solid var(--rust);
            border-radius: 10px;
            padding: 16px 22px;
            margin-bottom: 20px;
        }

        .fs-stamp {
            flex-shrink: 0;
            width: 104px;
            height: 104px;
            border-radius: 50%;
            border: 3px double var(--rust);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            transform: rotate(-8deg);
            color: var(--rust);
            font-family: 'Space Mono', monospace;
            text-align: center;
            background-color: rgba(var(--rust-rgb), 0.05);
        }

        .fs-stamp-label {
            font-size: 0.62rem;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        .fs-stamp-score {
            font-size: 1.55rem;
            font-weight: 700;
            margin-top: 2px;
        }

        .fs-winner-eyebrow {
            font-family: 'Space Mono', monospace;
            font-size: 0.8rem;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 4px;
        }

        .fs-winner-name {
            font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
            font-size: 1.4rem;
            font-weight: 600;
            color: var(--ink);
        }

        .fs-winner-sub {
            font-family: 'Space Mono', monospace;
            font-size: 0.85rem;
            color: var(--ink-muted);
            margin-top: 2px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- App Layout --

_init_state()
_inject_custom_theme()

# Sidebar Navigation & Setup
with st.sidebar:
    if os.path.exists("assets/banner.svg"):
        st.image("assets/banner.svg", width="stretch")

    st.markdown("### Navigation")

    nav_options = [
        "🏠 Candidates",
        "📍 Destinations",
        "⚖️ Weights & Parameters",
        "🚀 Run & Results",
    ]

    selected_nav = st.radio(
        label="Select Page",
        options=nav_options,
        index=0,
        label_visibility="collapsed",
        key="main_nav_radio",
    )

    n_cand = len(st.session_state.candidates_df)
    n_dest = len(st.session_state.destinations_df)

    st.markdown(
        f"""
        <div class="fs-stat-pill">
            <div><strong>Candidates:</strong> {n_cand} loaded</div>
            <div><strong>Destinations:</strong> {n_dest} loaded</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

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
        st.download_button(
            "Download config.json",
            data=json.dumps(_build_config(), indent=2, ensure_ascii=False),
            file_name="config.json",
            mime="application/json",
            width="stretch",
        )

    st.caption(
        "FlatScorer geocodes via Nominatim and pulls OSM data via Overpass. "
        "A run over 3-4 candidates typically takes 1-3 minutes depending on "
        "OSM server load — this is normal, not a hang."
    )


# ------------------------------------------------------------- Main Content --

# Header Block matching SVG Banner aesthetic
st.markdown(
    """
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
    """,
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------ TABS --

if selected_nav.startswith("🏠"):
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
        },
        key="candidates_editor",
    ).reset_index(drop=True)


elif selected_nav.startswith("📍"):
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


elif selected_nav.startswith("⚖️"):
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
             "Cycled legs are dashed. Always toggleable via the map's layer control.",
    )


elif selected_nav.startswith("🚀"):
    config = _build_config()
    n_candidates = len(config["candidates"])
    n_destinations = len(config["destinations"])

    st.markdown(
        f"""
        <div class="fs-card">
            <div class="fs-card-title">🚀 Run FlatScorer Engine</div>
            <div class="fs-card-desc">
                Ready to evaluate <strong>{n_candidates} candidate apartment(s)</strong> against <strong>{n_destinations} commute destination(s)</strong>.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Catch a config the engine would reject before the user waits through
    # geocoding and an OpenStreetMap download to find out.
    config_problems = validate_config(config)

    run_clicked = st.button(
        "▶ Start Evaluation & Run Scorer",
        type="primary",
        disabled=(n_candidates == 0 or bool(config_problems)),
        width="stretch",
    )

    if n_candidates == 0:
        st.warning("⚠️ Please add at least one candidate apartment in the Candidates tab before running.")
    elif config_problems:
        st.error(
            f"⚠️ **{len(config_problems)} problem(s) to fix before running:**\n\n"
            + "\n".join(f"- {problem}" for problem in config_problems)
        )

    if run_clicked:
        work_dir = tempfile.mkdtemp(prefix="flatscorer_")
        config["output"]["csv_file"] = os.path.join(work_dir, "apartment_scores.csv")
        config["output"]["html_file"] = os.path.join(work_dir, "apartment_map.html")

        log_capture = io.StringIO()
        with st.spinner("Geocoding addresses, downloading OpenStreetMap data, and calculating scores..."):
            try:
                with contextlib.redirect_stdout(log_capture):
                    scorer = FlatScorer(config, verbose=True)
                    df = scorer.run()
                with open(config["output"]["html_file"], encoding="utf-8") as f:
                    map_html = f.read()
                with open(config["output"]["csv_file"], "rb") as f:
                    csv_bytes = f.read()
                st.session_state.last_result = {
                    "df": df,
                    "log": log_capture.getvalue(),
                    "map_html": map_html,
                    "csv_bytes": csv_bytes,
                    "failed_candidates": scorer.failed_candidates,
                    "failed_destinations": scorer.failed_destinations,
                }
            except SearchAreaError as e:
                # Not a crash but a rejected input, and the message already names
                # the address to fix - so say that rather than "execution failed".
                st.session_state.last_result = {
                    "error": str(e),
                    "error_title": "Addresses are too far apart to search",
                    "log": log_capture.getvalue(),
                }
            except Exception as e:  # noqa: BLE001 - run() can raise from geopandas/osmnx/networkx; surface any failure in the UI instead of crashing
                st.session_state.last_result = {"error": str(e), "log": log_capture.getvalue()}

    result = st.session_state.get("last_result")
    if result:
        st.markdown("---")
        if "error" in result:
            title = result.get("error_title", "Execution failed")
            st.error(f"{title}: {result['error']}")
            with st.expander("Detailed Run Log"):
                st.text(result["log"])
        else:
            df = result["df"]

            # Geocoding failures drop rows from the ranking entirely - say so loudly,
            # otherwise a flat just disappears from the results without explanation.
            failed_candidates = result.get("failed_candidates") or []
            failed_destinations = result.get("failed_destinations") or []
            if failed_candidates:
                lines = "\n".join(f"- **{name}** — `{addr}`" for name, addr in failed_candidates)
                st.error(
                    f"⚠️ {len(failed_candidates)} candidate(s) could not be geocoded and are "
                    f"**missing from the ranking below**:\n\n{lines}\n\n"
                    "Check that each address includes street, house number, postal code, and city."
                )
            if failed_destinations:
                lines = "\n".join(f"- **{name}** — `{addr}`" for name, addr in failed_destinations)
                st.warning(
                    f"⚠️ {len(failed_destinations)} destination(s) could not be geocoded and were "
                    f"**excluded from commute scoring**:\n\n{lines}"
                )

            # Top Match Callout Badge
            if not df.empty and "score" in df.columns:
                top_row = df.iloc[0]
                top_score = top_row["score"]
                top_name = top_row.get("name", "Top Option")

                # The score is a weighted average of normalized metrics, so the
                # 0-10 scale is real: bounded, and comparable between runs.
                margin = float(top_score - df.iloc[1]["score"]) if len(df) > 1 else None
                if margin is None:
                    sub_text = "Only candidate scored — nothing to compare against"
                elif margin < NARROW_MARGIN_THRESHOLD:
                    sub_text = f"Just {margin:.2f} pts ahead of #2 — effectively a tie"
                else:
                    sub_text = f"{margin:.2f} pts ahead of #2"

                st.markdown(
                    f"""
                    <div class="fs-winner-panel">
                        <div class="fs-stamp">
                            <span class="fs-stamp-label">Top Pick</span>
                            <span class="fs-stamp-score">{top_score:.2f}</span>
                            <span class="fs-stamp-label">out of {SCORE_SCALE_MAX:.0f}</span>
                        </div>
                        <div>
                            <div class="fs-winner-eyebrow">🏆 Recommended Match</div>
                            <div class="fs-winner-name">{top_name}</div>
                            <div class="fs-winner-sub">Score {top_score:.2f} / {SCORE_SCALE_MAX:.0f} — {sub_text}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.markdown("### 📊 Ranked Comparison")
            st.dataframe(
                df.drop(columns=["lat", "lon"], errors="ignore").reset_index(drop=True),
                width="stretch",
            )

            c1, c2 = st.columns(2)
            with c1:
                st.download_button(
                    "📥 Download Scores (CSV)",
                    data=result["csv_bytes"],
                    file_name="apartment_scores.csv",
                    mime="text/csv",
                    width="stretch",
                )
            with c2:
                st.download_button(
                    "🗺️ Download Interactive Map (HTML)",
                    data=result["map_html"],
                    file_name="apartment_map.html",
                    mime="text/html",
                    width="stretch",
                )

            st.markdown("---")
            st.markdown("### 🗺️ Interactive GIS Map")
            st.iframe(result["map_html"], height=620)

            with st.expander("📋 Detailed Geocoding & Sensitivity Log"):
                st.text(result["log"])
