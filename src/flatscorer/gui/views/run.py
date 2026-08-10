"""The Run & Results page: drives the engine and renders what it returns."""

from __future__ import annotations

import contextlib
import html
import io
import os
import tempfile

import pandas as pd
import streamlit as st

from flatscorer import scorer as engine
from flatscorer.config import validate_config
from flatscorer.gui.state import (
    LISTING_URL_DISPLAY,
    _build_config,
)
from flatscorer.scoring import (
    NARROW_MARGIN_THRESHOLD,
    SCORE_SCALE_MAX,
)
from flatscorer.spatial import SearchAreaError


def render():
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
    else:
        # Right under the button, where someone about to wait will actually read
        # it. The sidebar caption saying the same thing was easy to miss, and the
        # progress bar below then shows it live rather than just asserting it.
        st.caption(
            "⏱️ A run takes roughly 1–3 minutes: addresses are geocoded one per second "
            "(Nominatim's rate limit), then the street network and points of interest are "
            "downloaded from OpenStreetMap. The progress bar reports each step as it starts."
        )

    if run_clicked:
        work_dir = tempfile.mkdtemp(prefix="flatscorer_")
        config["output"]["csv_file"] = os.path.join(work_dir, "apartment_scores.csv")
        config["output"]["html_file"] = os.path.join(work_dir, "apartment_map.html")

        log_capture = io.StringIO()
        # A determinate bar rather than a spinner: the engine reports each step
        # as it begins, so the wait stops looking like a hang. `run()` is
        # synchronous, so these updates are pushed from this same script run.
        progress_bar = st.progress(0.0, text="Starting...")

        def report_progress(fraction: float, label: str):
            progress_bar.progress(fraction, text=label)

        try:
            try:
                with contextlib.redirect_stdout(log_capture):
                    scorer = engine.FlatScorer(config, verbose=True, progress=report_progress)
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
        finally:
            # The results (or the error) render below and say everything the bar
            # would; a bar left at 100% just pushes them down the page. `finally`
            # so a failed run doesn't strand it mid-way either.
            progress_bar.empty()

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

                # The most likely click once a run finishes: the winner's own
                # listing. NaN is truthy, so the `pd.isna` guard is what stops a
                # linkless run rendering an href to the string "nan"; escaped
                # because this panel is rendered with unsafe_allow_html and the
                # URL can have come out of an uploaded config.
                top_url = top_row.get("url")
                top_url = "" if pd.isna(top_url) else str(top_url).strip()
                top_link = (
                    f'<a class="fs-winner-link" href="{html.escape(top_url, quote=True)}" '
                    'target="_blank" rel="noopener noreferrer">🔗 View listing ↗</a>'
                ) if top_url else ""

                # `top_link` is interpolated onto the end of the line above rather
                # than onto its own, and that is load-bearing. Streamlit dedents
                # markdown, and textwrap.dedent normalizes a whitespace-only line
                # to empty — so a linkless winner used to leave a blank line here,
                # which terminates a raw-HTML block in Markdown and rendered the
                # closing </div> tags as literal text under the panel. Any optional
                # value interpolated into a block like this has the same trap.
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
                            <div class="fs-winner-sub">Score {top_score:.2f} / {SCORE_SCALE_MAX:.0f} — {sub_text}</div>{top_link}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.markdown("### 📊 Ranked Comparison")
            st.dataframe(
                df.drop(columns=["lat", "lon"], errors="ignore").reset_index(drop=True),
                width="stretch",
                # Ignored when the run produced no links, so a config without
                # them shows exactly the table it always did.
                column_config={
                    "url": st.column_config.LinkColumn(
                        "Listing", display_text=LISTING_URL_DISPLAY,
                    ),
                },
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
