"""Where FlatScorer reads resources from and writes results to.

Everything here runs offline — no Overpass, no Nominatim.
"""

from __future__ import annotations

import os

import osmnx as ox
import platformdirs

from flatscorer import paths
from flatscorer.config import DEFAULT_CONFIG


def test_the_osmnx_cache_is_per_user_not_per_working_directory():
    """osmnx's own default is `./cache`, which is the thing being replaced.

    A downloaded street network is expensive and identical wherever you ask for
    it, so caching it per-cwd both wastes the download and drops a directory into
    whatever the user happened to be standing in.
    """
    cache = paths.cache_dir()
    assert os.path.isabs(cache), cache
    assert cache != "cache"
    assert cache == platformdirs.user_cache_dir(paths.APP_NAME)


def test_osmnx_is_actually_configured_to_use_it():
    """Setting the folder is only worth anything if osmnx has been told."""
    assert ox.settings.cache_folder == paths.cache_dir()


def test_generated_files_default_into_an_output_directory():
    assert paths.output_dir() == "output"
    assert paths.output_path("apartment_scores.csv") == os.path.join("output", "apartment_scores.csv")


def test_the_output_default_is_relative_to_the_working_directory():
    """Results describe the run you just did, so they land where you are."""
    assert not os.path.isabs(paths.output_path("apartment_map.html"))


def test_ensure_parent_creates_a_missing_directory(tmp_path):
    target = tmp_path / "deep" / "nested" / "scores.csv"
    assert not target.parent.exists()

    returned = paths.ensure_parent(str(target))

    assert target.parent.is_dir()
    assert returned == str(target)


def test_ensure_parent_is_happy_when_the_directory_already_exists(tmp_path):
    target = tmp_path / "scores.csv"
    paths.ensure_parent(str(target))
    paths.ensure_parent(str(target))          # must not raise
    assert tmp_path.is_dir()


def test_ensure_parent_handles_a_bare_filename(tmp_path, monkeypatch):
    """`dirname("scores.csv")` is empty; makedirs("") raises."""
    monkeypatch.chdir(tmp_path)
    assert paths.ensure_parent("scores.csv") == "scores.csv"


def test_the_generated_config_points_at_the_output_directory():
    """`--generate-config` has to agree with the defaults.

    The config template sets `output` explicitly, so a bare filename here would
    silently override `output_dir()` and land results in the cwd for everyone who
    started from a generated config — which is everyone.
    """
    assert DEFAULT_CONFIG["output"]["csv_file"] == paths.output_path("apartment_scores.csv")
    assert DEFAULT_CONFIG["output"]["html_file"] == paths.output_path("apartment_map.html")


def test_a_resource_path_stays_inside_the_package():
    """Reads come from the package; only writes move to user directories."""
    banner = paths.resource_path("gui", "assets", "banner.svg")
    assert os.path.isfile(banner)
    assert banner.startswith(os.path.dirname(os.path.abspath(paths.__file__)))


def test_default_config_points_the_overview_report_at_the_output_directory():
    assert DEFAULT_CONFIG["output"]["overview_file"] == paths.output_path("apartment_overview.html")
