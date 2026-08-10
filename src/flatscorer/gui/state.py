"""Everything that reads or writes `st.session_state`.

Kept apart from the pages so the config round-trip - load a JSON file into the
widgets, build a JSON config back out of them - can be followed in one place.
"""

from __future__ import annotations

import copy
from typing import Any

import pandas as pd
import streamlit as st

from flatscorer import paths
from flatscorer.config import DEFAULT_CONFIG
from flatscorer.routing import DEFAULT_TRAVEL_MODE, TRAVEL_MODES

# `st.data_editor` only offers the columns its frame actually has, so every
# candidate frame is reindexed onto this - otherwise a config written before
# `url` existed would silently stop offering the field.
CANDIDATE_COLUMNS = ("name", "address", "rent", "url", "image")

# Renders the domain rather than the raw link, so a 140-character listing URL
# shows as `immobilienscout24.de` and the table stays readable.
LISTING_URL_DISPLAY = r"https?://(?:www\.)?([^/]+)"

DEFAULT_WEIGHTS = DEFAULT_CONFIG["weights"]
DEFAULT_PARAMS = DEFAULT_CONFIG["parameters"]

def _candidate_frame(candidates: list[dict[str, Any]]) -> pd.DataFrame:
    """Candidates as a frame carrying exactly `CANDIDATE_COLUMNS`.

    Reindexed rather than trusted: `url` and `image` are optional and omitted
    when unset, so an incoming config can legitimately have neither key on any
    candidate — and the editor would then not show the column at all.

    The blanks have to be empty strings, not NaN, for every optional column.
    Reindexing in a column nothing supplies gives it float64 dtype, and the
    editor refuses to edit a float column as text — the whole page dies with a
    StreamlitAPIException, not just the cell.
    """
    frame = pd.DataFrame(candidates).reindex(columns=list(CANDIDATE_COLUMNS))
    for optional in ("url", "image"):
        frame[optional] = frame[optional].fillna("").astype(str)
    return frame


def _init_state():
    """Seed session_state with the built-in demo config on first load."""
    if "candidates_df" in st.session_state:
        return

    st.session_state.candidates_df = _candidate_frame(DEFAULT_CONFIG["candidates"])

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
    st.session_state.candidates_df = _candidate_frame(config.get("candidates", []))

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
        candidate = {
            "name": row["name"],
            "address": row["address"],
            "rent": 0.0 if pd.isna(rent) else float(rent),
        }
        # Same NaN trap as rent: a cleared cell is truthy, so `or ""` would put
        # the string "nan" in as the listing link. Omitted entirely when blank,
        # so a config with no links round-trips exactly as it did before the
        # field existed.
        url = row.get("url")
        url = "" if pd.isna(url) else str(url).strip()
        if url:
            candidate["url"] = url
        # Same NaN trap as url and rent: a cleared cell is truthy, so `or ""`
        # would write the string "nan" in as a photo path. Omitted when blank,
        # so a config with no photos round-trips exactly as it did before.
        image = row.get("image")
        image = "" if pd.isna(image) else str(image).strip()
        if image:
            candidate["image"] = image
        candidates.append(candidate)

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
        # Same defaults a generated config carries. The Run page overrides both
        # with a temp directory before scoring, so these only matter for the
        # config.json the sidebar exports — which is meant to run identically
        # under the CLI.
        "output": {
            "csv_file": paths.output_path("apartment_scores.csv"),
            "html_file": paths.output_path("apartment_map.html"),
            "overview_file": paths.output_path("apartment_overview.html"),
        },
    }
