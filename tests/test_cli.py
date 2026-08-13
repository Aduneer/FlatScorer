"""The `flatscorer` console script's argument handling.

These are the first tests of `cli.main` itself. The `package` CI job proves the
console script *exists* and starts; nothing until now proved it did the right
thing with a flag. Every test here has to stay offline, so each one either exits
before `FlatScorer` is constructed or asserts that it never was.
"""

import json

import pytest
from conftest import valid_config

from flatscorer import __version__, cli, osm


def run_cli(monkeypatch, *argv):
    """Invoke main() with argv, returning the SystemExit it raised.

    argparse and main() both exit rather than return, so the exit code is the
    result. FlatScorer is replaced by a bomb: reaching it means a code path that
    was supposed to stop offline would have hit Nominatim and Overpass.
    """
    def _no_network(*_args, **_kwargs):
        raise AssertionError("main() constructed FlatScorer - this path is not offline")

    monkeypatch.setattr(cli, "FlatScorer", _no_network)
    monkeypatch.setattr("sys.argv", ["flatscorer", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


# -- --version --


def test_version_reports_the_installed_distribution_version(monkeypatch, capsys):
    assert run_cli(monkeypatch, "--version") == 0
    assert capsys.readouterr().out.strip() == f"flatscorer {__version__}"


# -- --check-config --


def test_check_config_accepts_a_valid_file_and_exits_zero(monkeypatch, capsys, tmp_path):
    path = write_config(tmp_path, valid_config())

    assert run_cli(monkeypatch, "--config", path, "--check-config") == 0
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "1 candidate(s), 1 destination(s)" in out


def test_check_config_rejects_an_invalid_file_and_exits_one(monkeypatch, capsys, tmp_path):
    """The same non-zero exit the full run uses, so CI can gate on it."""
    broken = valid_config(candidates=[{"name": "Flat A", "address": "1 Main St"}])  # no rent
    path = write_config(tmp_path, broken)

    assert run_cli(monkeypatch, "--config", path, "--check-config") == 1
    assert "'rent' is missing" in capsys.readouterr().err


def test_check_config_validates_the_built_in_demo_when_given_no_file(monkeypatch, capsys):
    """With no --config there is still something to check, and it must pass -
    a shipped demo that fails validation breaks first-run for everyone."""
    assert run_cli(monkeypatch, "--check-config") == 0
    out = capsys.readouterr().out
    assert "built-in demo config" in out
    assert "Running with built-in demo data" not in out, "nothing is being run"


def test_check_config_reports_malformed_json_rather_than_raising(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")

    assert "not valid JSON" in str(run_cli(monkeypatch, "--config", str(path), "--check-config"))


def test_check_config_reports_a_missing_file(monkeypatch, tmp_path):
    missing = str(tmp_path / "nope.json")

    assert "not found" in str(run_cli(monkeypatch, "--config", missing, "--check-config"))


# -- --generate-config --


def test_generate_config_writes_a_file_that_check_config_then_accepts(monkeypatch, tmp_path):
    """The two flags are each other's test: whatever we ship has to pass our own check."""
    path = str(tmp_path / "generated.json")

    assert run_cli(monkeypatch, "--generate-config", path) == 0
    assert run_cli(monkeypatch, "--config", path, "--check-config") == 0


# -- a rate-limited run --


def test_a_rate_limited_run_exits_with_the_message_and_no_traceback(monkeypatch, tmp_path, capsys):
    """The user's next move is to wait, and a stack trace argues for the opposite:
    it reads as a bug in the tool, and the thing you do with a buggy run is re-run
    it. Still offline - `FlatScorer` is a stub that never reaches the network."""
    class RateLimitedScorer:
        def __init__(self, config, verbose=True, progress=None):
            pass

        def run(self):
            raise osm.RateLimitedError("https://overpass-api.de/api/interpreter", 42)

    monkeypatch.setattr(cli, "FlatScorer", RateLimitedScorer)
    monkeypatch.setattr("sys.argv", ["flatscorer", "--config", write_config(tmp_path, valid_config())])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    message = str(exit_info.value.code)
    assert "429" in message
    assert "42 seconds" in message
    assert exit_info.value.code != 0
