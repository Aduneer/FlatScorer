"""Regression tests for the Streamlit front end, driven headlessly by AppTest.

These cover the `st.data_editor` state bug fixed in 0e7e248/fa05285: edits used
to revert (or need applying twice) because the frame handed to the editor was
overwritten with the edited result, desyncing Streamlit's delta tracking.

No network is involved. The Run page is exercised with `FakeScorer` patched over
the engine, so the page's own wiring — the progress callback, reading the output
files back — is tested without a real run.
"""

from __future__ import annotations

import os

import pandas as pd
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


def _stat_pill(app: AppTest) -> str:
    return next(m.value for m in app.sidebar.markdown if "fs-stat-pill" in m.value)


def test_the_sidebar_config_download_reflects_the_edit_that_triggered_the_rerun():
    """The exported config must not be one edit behind.

    `st.download_button` hands Streamlit its payload at render time, and that
    payload is content-addressed — the URL changes if and only if the bytes do,
    which is the only handle AppTest gives us on it. The sidebar block runs
    before the candidates `data_editor` assigns its edited frame into
    session_state, so it used to export the *previous* state: make an edit, hit
    Download, get a file without it.
    """
    app = fresh_app()
    before = app.download_button[0].proto.url

    edit_cell(app, 0, "rent", 999)
    app.run()

    assert not app.exception, app.exception
    assert app.session_state.candidates_df.loc[0, "rent"] == 999, "the edit itself did not land"
    assert app.download_button[0].proto.url != before, (
        "the download payload is byte-identical after an edit — the sidebar is a rerun behind"
    )


def test_the_sidebar_count_reflects_a_row_added_in_the_same_rerun():
    """Same root cause as the download button, but visible on screen.

    Adding a row reruns the script, and the sidebar counted `candidates_df`
    before the editor had applied the addition — so the pill kept showing the
    old number, and nothing triggers a further rerun to correct it.
    """
    app = fresh_app()
    app.session_state["candidates_editor"] = {
        "edited_rows": {},
        "added_rows": [{"name": "Flat D", "address": "Karl-Marx-Allee 1, Berlin", "rent": 900}],
        "deleted_rows": [],
    }
    app.run()

    assert not app.exception, app.exception
    assert len(app.session_state.candidates_df) == 4, "the added row itself did not land"
    assert "<strong>Candidates:</strong> 4 loaded" in _stat_pill(app)


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
    assert params["walking_speed_m_per_min"] > 0
    assert set(params["saturation"]) == set(streamlit_app.DEFAULT_PARAMS["saturation"])


def test_the_walking_speed_widget_reaches_the_built_config():
    """The parameter is only tunable if the GUI's value actually gets scored."""
    import streamlit_app

    app = fresh_app()
    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()

    speed_input = next(w for w in app.number_input if "Walking speed" in w.label)
    speed_input.set_value(100.0).run()

    assert not app.exception, app.exception

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        params = streamlit_app._build_config()["parameters"]

    assert params["walking_speed_m_per_min"] == 100.0


def test_the_cycling_speed_widget_reaches_the_built_config():
    """Same contract as the walking pace: tunable only if the GUI's value scores."""
    import streamlit_app

    app = fresh_app()
    app.session_state["main_nav_radio"] = WEIGHTS
    app.run()

    speed_input = next(w for w in app.number_input if "Cycling speed" in w.label)
    speed_input.set_value(200.0).run()

    assert not app.exception, app.exception

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        params = streamlit_app._build_config()["parameters"]

    assert params["cycling_speed_m_per_min"] == 200.0


def test_the_mode_dropdown_can_only_offer_modes_the_engine_accepts():
    """A dropdown listing a mode validate_config rejects would be a dead end."""
    import streamlit_app
    from FlatScorer import TRAVEL_MODES

    assert streamlit_app.MODE_CHOICES == list(TRAVEL_MODES)


def test_a_cycling_destination_survives_the_round_trip_into_a_config():
    import streamlit_app
    from FlatScorer import validate_config

    app = fresh_app()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        app.session_state.destinations_df = pd.DataFrame([
            {"name": "Office", "address": "Alexanderplatz 1, Berlin", "weight": 0.2,
             "mode": "bike", "icon": "briefcase", "color": "blue"},
        ])
        config = streamlit_app._build_config()

    assert config["destinations"]["Office"]["mode"] == "bike"
    assert validate_config(config) == []


def test_a_destination_row_with_no_mode_column_still_builds_a_walking_destination():
    """An uploaded pre-cycling config has no 'mode' at all — it must still run."""
    import streamlit_app
    from FlatScorer import validate_config

    app = fresh_app()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        app.session_state.destinations_df = pd.DataFrame([
            {"name": "Office", "address": "Alexanderplatz 1, Berlin", "weight": 0.2},
        ])
        config = streamlit_app._build_config()

    assert config["destinations"]["Office"]["mode"] == "walk"
    assert validate_config(config) == []


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


def test_the_candidates_frame_always_offers_a_url_column():
    """The editor only shows columns the frame has, so a config predating the
    field would silently stop offering it."""
    app = fresh_app()
    assert "url" in app.session_state.candidates_df.columns


def test_loading_a_config_without_links_still_offers_the_url_column():
    import streamlit_app

    app = fresh_app()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(streamlit_app.st, "session_state", app.session_state)
        streamlit_app._load_config_into_state({
            "candidates": [{"name": "Flat A", "address": "1 Main St", "rent": 1800}],
            "destinations": {},
        })

    assert list(app.session_state.candidates_df.columns) == list(streamlit_app.CANDIDATE_COLUMNS)


def test_a_listing_url_typed_into_the_editor_reaches_the_built_config():
    from FlatScorer import validate_config

    app = fresh_app()
    edit_cell(app, 0, "url", "https://example.com/expose/1")
    app.run()

    assert not app.exception, app.exception
    config = build_config_from(app)
    assert config["candidates"][0]["url"] == "https://example.com/expose/1"
    assert validate_config(config) == []


def test_a_candidate_with_no_link_carries_no_url_key_at_all():
    """A config with no links must round-trip exactly as it did before the field."""
    app = fresh_app()
    assert all("url" not in c for c in build_config_from(app)["candidates"])


def test_a_cleared_listing_url_is_omitted_rather_than_exported_as_nan():
    """A cleared cell arrives as NaN, which is truthy — `or ""` would export the
    string "nan" as the listing link."""
    app = fresh_app()
    edit_cell(app, 0, "url", "https://example.com/expose/1")
    app.run()
    edit_cell(app, 0, "url", None)
    app.run()

    assert not app.exception, app.exception
    assert "url" not in build_config_from(app)["candidates"][0]


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


def test_the_gui_launcher_resolves_the_app_it_ships_with():
    """The `flatscorer-gui` entry point locates streamlit_app.py by path.

    `streamlit run` needs a file, and after `pip install` that file lives in
    site-packages rather than the cwd — so a launcher that guessed a relative
    path would work from a checkout and fail everywhere else.
    """
    import flatscorer_gui

    assert os.path.isfile(flatscorer_gui.app_path())
    assert os.path.samefile(flatscorer_gui.app_path(), APP)


REPORTED: list[tuple[float, str]] = []


class FakeScorer:
    """Stands in for the engine so the Run page can be driven without a network.

    It reports progress the way `run()` does and writes the two output files the
    page reads back, which is everything the GUI actually depends on.
    """

    # Monkeypatched by the listing-link tests; None means the engine produced no
    # `url` column at all, which is what a run with no links looks like.
    url = None

    def __init__(self, config, verbose=True, progress=None):
        self.config = config
        self.progress = progress
        self.failed_candidates = []
        self.failed_destinations = []

    def run(self):
        for fraction, label in ((0.0, "Geocoding 'Flat A'..."),
                                (0.4, "Downloading the walking street network..."),
                                (1.0, "Finished")):
            if self.progress is not None:
                self.progress(fraction, label)
                REPORTED.append((fraction, label))

        row = {"name": "Flat A", "score": 7.5, "rent_eur": 1200,
               "office_walk_min": 12.0, "lat": 52.0, "lon": 13.0}
        if self.url:
            row["url"] = self.url
        df = pd.DataFrame([row])
        df.to_csv(self.config["output"]["csv_file"], index=False)
        with open(self.config["output"]["html_file"], "w", encoding="utf-8") as f:
            f.write("<html>map</html>")
        return df


def test_the_run_page_drives_a_progress_bar_from_the_engine(monkeypatch):
    """The callback has to survive the whole trip: engine -> page -> st.progress.

    A wrong argument name or an out-of-range fraction only shows up here, since
    `st.progress` is the one thing the engine tests can't exercise.
    """
    import FlatScorer as engine

    REPORTED.clear()
    monkeypatch.setattr(engine, "FlatScorer", FakeScorer)

    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    app.session_state["main_nav_radio"] = RUN
    app.run()
    app.button[0].click().run()

    assert not app.exception, app.exception
    assert REPORTED, "the engine was given no progress callback"
    assert [f for f, _ in REPORTED] == sorted(f for f, _ in REPORTED)
    assert REPORTED[-1][0] == 1.0
    # The run still produced its results, i.e. the bar didn't replace them.
    assert "df" in app.session_state.last_result


def _run_page_after_a_run(monkeypatch, url=None) -> AppTest:
    import FlatScorer as engine

    monkeypatch.setattr(engine, "FlatScorer", FakeScorer)
    monkeypatch.setattr(FakeScorer, "url", url)

    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    app.session_state["main_nav_radio"] = RUN
    app.run()
    app.button[0].click().run()
    assert not app.exception, app.exception
    return app


def _winner_panel(app: AppTest) -> str:
    # Matched on the opening div, not the bare class name — the injected CSS
    # block mentions `.fs-winner-panel` too and renders as markdown first.
    return next(m.value for m in app.markdown if '<div class="fs-winner-panel">' in m.value)


def test_the_results_table_keeps_the_listing_url_column():
    """lat/lon are dropped from the shown table — url must not be."""
    with pytest.MonkeyPatch.context() as mp:
        app = _run_page_after_a_run(mp, url="https://example.com/expose/1")
        assert "url" in app.dataframe[0].value.columns


def test_the_winner_panel_links_to_the_top_flats_listing():
    with pytest.MonkeyPatch.context() as mp:
        app = _run_page_after_a_run(mp, url="https://example.com/expose/1")
        panel = _winner_panel(app)

    assert "View listing" in panel
    assert 'href="https://example.com/expose/1"' in panel


def test_the_winner_panel_has_no_link_when_the_top_flat_has_none():
    with pytest.MonkeyPatch.context() as mp:
        app = _run_page_after_a_run(mp)
        assert "View listing" not in _winner_panel(app)


def test_the_winner_panel_escapes_the_listing_url():
    """The panel is rendered with unsafe_allow_html, and the URL can arrive from
    an uploaded config."""
    with pytest.MonkeyPatch.context() as mp:
        app = _run_page_after_a_run(mp, url='https://example.com/a"><script>alert(1)</script>')
        panel = _winner_panel(app)

    assert '"><script>' not in panel
    assert "&quot;&gt;&lt;script&gt;" in panel


def test_the_run_page_states_how_long_a_run_takes(monkeypatch):
    """The timing note used to live only in the sidebar, where it was missed."""
    import FlatScorer as engine

    monkeypatch.setattr(engine, "FlatScorer", FakeScorer)
    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    app.session_state["main_nav_radio"] = RUN
    app.run()

    assert not app.exception, app.exception
    captions = [c.value for c in app.caption]
    assert any("minute" in c for c in captions), captions
