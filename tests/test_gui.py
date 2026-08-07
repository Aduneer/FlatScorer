"""Regression tests for the Streamlit front end, driven headlessly by AppTest.

These cover the `st.data_editor` state bug fixed in 0e7e248/fa05285: edits used
to revert (or need applying twice) because the frame handed to the editor was
overwritten with the edited result, desyncing Streamlit's delta tracking.
No network is involved — nothing here runs the scorer.
"""

from __future__ import annotations

import os

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamlit_app.py")

CANDIDATES = "🏠 Candidates"
WEIGHTS = "⚖️ Weights & Parameters"


def fresh_app() -> AppTest:
    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def edit_cell(app: AppTest, row: int, column: str, value) -> None:
    """Inject an edit the way the real data_editor widget reports one.

    The widget reports a delta against its baseline frame, and that delta
    accumulates for as long as the widget stays mounted — so edits are merged
    in rather than replacing whatever was already pending.
    """
    if "candidates_editor" in app.session_state:
        state = app.session_state["candidates_editor"]
    else:
        state = {"edited_rows": {}, "added_rows": [], "deleted_rows": []}
    state["edited_rows"].setdefault(row, {})[column] = value
    app.session_state["candidates_editor"] = state


def test_app_starts_with_the_demo_candidates():
    app = fresh_app()
    assert len(app.session_state.candidates_df) == 3


def test_a_single_edit_is_applied_after_one_rerun():
    # The original bug needed the same edit twice before it stuck.
    app = fresh_app()
    edit_cell(app, 0, "rent", 999)
    app.run()

    assert not app.exception, app.exception
    assert app.session_state.candidates_df.loc[0, "rent"] == 999


def test_an_edit_survives_navigating_away_and_back():
    # Leaving the page unmounts the editor, which drops its widget state; the
    # baseline must then adopt the edited frame rather than the original one,
    # or the edit silently reverts on return.
    app = fresh_app()
    edit_cell(app, 0, "rent", 999)
    app.run()

    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()
    app.session_state["main_nav_radio"] = CANDIDATES
    app.run()

    assert not app.exception, app.exception
    assert app.session_state.candidates_df.loc[0, "rent"] == 999


def test_a_second_edit_does_not_undo_the_first():
    app = fresh_app()
    edit_cell(app, 0, "rent", 999)
    app.run()
    edit_cell(app, 1, "rent", 111)
    app.run()

    assert not app.exception, app.exception
    assert app.session_state.candidates_df.loc[0, "rent"] == 999
    assert app.session_state.candidates_df.loc[1, "rent"] == 111


def test_edited_rent_reaches_the_config_handed_to_the_scorer():
    app = fresh_app()
    edit_cell(app, 0, "rent", 999)
    app.run()
    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()

    import streamlit_app

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        config = streamlit_app._build_config()

    assert config["candidates"][0]["rent"] == 999


def test_weights_page_carries_the_normalization_anchors_into_the_config():
    app = fresh_app()
    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()
    assert not app.exception, app.exception

    import streamlit_app

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        params = streamlit_app._build_config()["parameters"]

    assert params["rent_budget_eur"] > 0
    assert params["commute_cap_min"] > 0
    assert set(params["saturation"]) == set(streamlit_app.DEFAULT_PARAMS["saturation"])


def test_editing_a_saturation_widget_does_not_mutate_the_module_default():
    # `params` nests a dict, so a shallow copy of DEFAULT_PARAMS would let the
    # widgets rewrite the built-in defaults for the rest of the process.
    import streamlit_app
    from FlatScorer import DEFAULT_SATURATION

    app = fresh_app()
    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()
    app.session_state["saturation_transit"] = 99.0
    app.run()

    assert not app.exception, app.exception
    assert app.session_state.params["saturation"]["transit"] == 99.0
    assert streamlit_app.DEFAULT_PARAMS["saturation"]["transit"] == DEFAULT_SATURATION["transit"]
    assert DEFAULT_SATURATION["transit"] != 99.0


RUN = "🚀 Run & Results"


def build_config_from(app: AppTest) -> dict:
    import streamlit_app

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        return streamlit_app._build_config()


def test_a_cleared_rent_cell_becomes_zero_rather_than_nan():
    # NaN is truthy, so the old `or 0` let a cleared cell through as NaN.
    app = fresh_app()
    edit_cell(app, 0, "rent", None)
    app.run()

    assert not app.exception, app.exception
    assert build_config_from(app)["candidates"][0]["rent"] == 0


def test_a_candidate_with_no_rent_blocks_the_run_button():
    from FlatScorer import validate_config

    app = fresh_app()
    edit_cell(app, 0, "rent", None)
    app.run()
    app.session_state["main_nav_radio"] = RUN
    app.run()

    assert not app.exception, app.exception
    assert validate_config(build_config_from(app)), "a zero rent should not validate"
    assert app.button[0].disabled, "the run button must be disabled while the config is invalid"
    assert any("rent" in err.value for err in app.error), [err.value for err in app.error]


def test_the_demo_config_leaves_the_run_button_enabled():
    app = fresh_app()
    app.session_state["main_nav_radio"] = RUN
    app.run()

    assert not app.exception, app.exception
    assert not app.button[0].disabled
    assert not app.error
