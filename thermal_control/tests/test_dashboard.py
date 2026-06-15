"""
Tests for the dashboard's live config-error banner (dashboard/app.py).

The banner must appear only while the config *currently on disk* is invalid and
clear the moment it is fixed — so liveness is re-validated each render rather
than read from a sticky errors.log entry.
"""

import shutil
from pathlib import Path

import pytest

from thermal_control.dashboard import app as dash

ROOT         = Path(__file__).resolve().parent.parent       # thermal_control/
GOOD_HOUSE   = ROOT / "config" / "house.yaml"
GOOD_CONTROL = ROOT / "config" / "control.yaml"


@pytest.fixture
def dash_paths(tmp_path, monkeypatch):
    """Point the dashboard at a throwaway config/logs dir seeded with the real
    (valid) config, and reset the last-good cache so tests don't bleed."""
    cfg, logs = tmp_path / "cfg", tmp_path / "logs"
    cfg.mkdir(); logs.mkdir()
    shutil.copy(GOOD_HOUSE, cfg / "house.yaml")
    shutil.copy(GOOD_CONTROL, cfg / "control.yaml")

    monkeypatch.setattr(dash, "HOUSE_YAML", cfg / "house.yaml")
    monkeypatch.setattr(dash, "CONTROL_YAML", cfg / "control.yaml")
    monkeypatch.setattr(dash, "ERROR_LOG", logs / "errors.log")
    monkeypatch.setattr(dash, "_last_good_cfg", {"control": None, "house": None})
    return cfg, logs


def test_valid_config_no_error_and_caches(dash_paths):
    control, house, err = dash.resolve_page_config()
    assert err is None
    assert control is not None and house is not None
    assert dash._last_good_cfg["control"] is not None     # cached as last-good


def test_broken_yaml_falls_back_to_last_good(dash_paths):
    cfg, _ = dash_paths
    dash.resolve_page_config()                            # prime last-good
    (cfg / "control.yaml").write_text("mpc:\n  tick_minutes: 10\n   bad: x\n")

    control, house, err = dash.resolve_page_config()
    assert err is not None
    assert "Invalid YAML syntax" in err["message"]
    assert control is not None                            # served last-good, page still works


def test_structurally_invalid_config_flagged(dash_paths):
    cfg, _ = dash_paths
    dash.resolve_page_config()
    (cfg / "control.yaml").write_text("mpc:\n  tick_minutes: 10\n")   # valid yaml, no 'targets'

    _, _, err = dash.resolve_page_config()
    assert err is not None
    assert "targets" in err["message"]


def test_error_clears_once_fixed(dash_paths):
    cfg, _ = dash_paths
    dash.resolve_page_config()
    (cfg / "control.yaml").write_text("mpc:\n  tick_minutes: 10\n")
    assert dash.resolve_page_config()[2] is not None      # broken → error

    shutil.copy(GOOD_CONTROL, cfg / "control.yaml")        # operator fixes it
    assert dash.resolve_page_config()[2] is None           # live again → banner clears


def test_since_read_from_errors_log(dash_paths):
    cfg, logs = dash_paths
    (logs / "errors.log").write_text(
        "2026-06-15 14:23:01  ERROR     Invalid config edit rejected (ValueError: ...)\n"
    )
    dash.resolve_page_config()
    (cfg / "control.yaml").write_text("mpc:\n  tick_minutes: 10\n")

    _, _, err = dash.resolve_page_config()
    assert err["since"] == "2026-06-15 14:23"


def test_last_error_log_time_missing_or_garbled(dash_paths):
    _, logs = dash_paths
    assert dash._last_error_log_time() is None             # no file
    (logs / "errors.log").write_text("not a timestamped line\n")
    assert dash._last_error_log_time() is None             # unparseable → None


def test_index_renders_banner_when_broken(dash_paths):
    cfg, _ = dash_paths
    dash.resolve_page_config()                            # prime last-good
    (cfg / "control.yaml").write_text("mpc:\n  tick_minutes: 10\n")

    html = dash.app.test_client().get("/").get_data(as_text=True)
    assert "config-error" in html                         # banner div present
    assert "Schedule the MPC sees" in html                # rest of page still renders
