"""
Tests for the dashboard's live config-error banner (dashboard/app.py).

The banner must appear only while the config *currently on disk* is invalid and
clear the moment it is fixed — so liveness is re-validated each render rather
than read from a sticky errors.log entry.
"""

import csv
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from thermal_control.dashboard import app as dash

ROOT         = Path(__file__).resolve().parent.parent       # thermal_control/
GOOD_HOUSE   = ROOT / "config" / "house.yaml"
GOOD_CONTROL = ROOT / "config" / "control.yaml"
TZ           = ZoneInfo(dash.TZ_NAME)


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


# ── Plot time-range selector (resolve_plot_range / _filter_range) ─────────────

def test_resolve_plot_range_presets():
    latest = datetime(2026, 7, 13, 12, 0, tzinfo=TZ)
    for code, delta in [("1d", timedelta(days=1)), ("3d", timedelta(days=3)),
                         ("1w", timedelta(weeks=1)), ("1m", timedelta(days=30))]:
        rc, start, end = dash.resolve_plot_range(code, None, None, latest)
        assert rc == code
        assert end == latest
        assert start == latest - delta


def test_resolve_plot_range_all_disables_filtering():
    rc, start, end = dash.resolve_plot_range("all", None, None, datetime(2026, 7, 13, tzinfo=TZ))
    assert (rc, start, end) == ("all", None, None)


def test_resolve_plot_range_unknown_code_falls_back_to_default():
    latest = datetime(2026, 7, 13, tzinfo=TZ)
    rc, start, end = dash.resolve_plot_range("bogus", None, None, latest)
    assert rc == dash.DEFAULT_RANGE
    assert end == latest


def test_resolve_plot_range_custom_valid():
    rc, start, end = dash.resolve_plot_range(
        "custom", "2026-07-01T00:00", "2026-07-05T00:00", None)
    assert rc == "custom"
    assert start == datetime(2026, 7, 1, tzinfo=TZ)
    assert end == datetime(2026, 7, 5, tzinfo=TZ)


def test_resolve_plot_range_custom_swaps_reversed_dates():
    rc, start, end = dash.resolve_plot_range(
        "custom", "2026-07-05T00:00", "2026-07-01T00:00", None)
    assert start < end


def test_resolve_plot_range_custom_missing_input_falls_back_to_default():
    latest = datetime(2026, 7, 13, tzinfo=TZ)
    rc, start, end = dash.resolve_plot_range("custom", "", "", latest)
    assert rc == dash.DEFAULT_RANGE
    assert end == latest


def test_resolve_plot_range_no_data_yet():
    """No decision-log rows at all → nothing to anchor a relative window to."""
    rc, start, end = dash.resolve_plot_range("1d", None, None, None)
    assert rc == "1d"
    assert (start, end) == (None, None)


def test_filter_range():
    import pandas as pd

    ts = pd.date_range("2026-07-01", periods=5, freq="D", tz=dash.TZ_NAME)
    df = pd.DataFrame({"timestamp": ts, "v": range(5)})
    out = dash._filter_range(df, ts[1], ts[3])
    assert list(out["v"]) == [1, 2, 3]


def _write_synthetic_decision_log(path, days=30):
    import pandas as pd

    ts = pd.date_range("2026-06-01", periods=days * 24, freq="h", tz=dash.TZ_NAME)
    df = pd.DataFrame({
        "timestamp": [t.isoformat() for t in ts],
        "on_bedroom_ac": 1, "on_living_ac": 0, "on_extension_ac": 1,
        "T_master_bedroom": 72.0, "T_dining_room": 73.0, "T_tv_room": 74.0,
    })
    df.to_csv(path, index=False)
    return len(df)


def test_onoff_data_range_filters_data(dash_paths, monkeypatch):
    cfg, logs = dash_paths
    log = logs / "mpc_decision_log.csv"
    total_rows = _write_synthetic_decision_log(log, days=30)
    monkeypatch.setattr(dash, "DECISION_LOG", log)
    client = dash.app.test_client()

    resp = client.get("/plots/onoff_data.json?ac=bedroom_ac&range=1d")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ac"] == "bedroom_ac"
    assert 20 <= len(body["timestamps"]) <= 25     # ~1 day of hourly rows
    assert len(body["on"]) == len(body["timestamps"])

    resp = client.get("/plots/onoff_data.json?ac=bedroom_ac&range=all")
    assert resp.get_json()["timestamps"].__len__() == total_rows


def test_onoff_data_includes_room_series_with_bands(dash_paths, monkeypatch):
    cfg, logs = dash_paths
    log = logs / "mpc_decision_log.csv"
    _write_synthetic_decision_log(log, days=2)
    monkeypatch.setattr(dash, "DECISION_LOG", log)
    client = dash.app.test_client()

    body = client.get("/plots/onoff_data.json?ac=bedroom_ac&range=all").get_json()
    rooms = {r["id"]: r for r in body["rooms"]}
    assert "master_bedroom" in rooms                 # has a T_ column in the synthetic log
    room = rooms["master_bedroom"]
    n = len(body["timestamps"])
    assert len(room["temp"]) == len(room["band_lo"]) == len(room["band_hi"]) == n
    assert room["color"].startswith("#")
    assert all(lo <= hi for lo, hi in zip(room["band_lo"], room["band_hi"]))
    # A room with no T_ column in the log (kids_bedroom/anna_office) is omitted.
    assert "kids_bedroom" not in rooms


def test_onoff_data_room_series_reflects_active_override(dash_paths, monkeypatch):
    """A logged manual override on a room must shift band_hi to the user's
    target for rows inside the override window, not just the plain schedule
    band (regression: the historical plot used to ignore overrides entirely)."""
    cfg, logs = dash_paths
    log = logs / "mpc_decision_log.csv"
    _write_synthetic_decision_log(log, days=2)
    monkeypatch.setattr(dash, "DECISION_LOG", log)

    user_inputs = logs / "user_inputs.log"
    with open(user_inputs, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "event", "room", "value"])
        w.writerow(["2026-06-01T05:00:00-04:00", "override_activated", "master_bedroom", "81"])
        w.writerow(["2026-06-01T08:00:00-04:00", "override_cancelled", "master_bedroom", "81"])
    monkeypatch.setattr(dash, "USER_INPUTS", user_inputs)

    client = dash.app.test_client()
    body = client.get("/plots/onoff_data.json?ac=bedroom_ac&range=all").get_json()
    room = {r["id"]: r for r in body["rooms"]}["master_bedroom"]
    timestamps = body["timestamps"]

    in_window  = timestamps.index("2026-06-01T06:00:00-04:00")
    out_window = timestamps.index("2026-06-01T02:00:00-04:00")
    assert room["band_hi"][in_window] == 81
    assert room["band_hi"][out_window] != 81


def test_onoff_data_unknown_ac_404(dash_paths):
    client = dash.app.test_client()
    resp = client.get("/plots/onoff_data.json?ac=not_a_real_ac")
    assert resp.status_code == 404


def test_onoff_data_custom_range_outside_data_is_empty(dash_paths, monkeypatch):
    cfg, logs = dash_paths
    log = logs / "mpc_decision_log.csv"
    _write_synthetic_decision_log(log, days=5)
    monkeypatch.setattr(dash, "DECISION_LOG", log)
    client = dash.app.test_client()

    resp = client.get(
        "/plots/onoff_data.json?ac=bedroom_ac&range=custom"
        "&start=2020-01-01T00:00&end=2020-01-02T00:00")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["timestamps"] == [] and body["on"] == [] and body["rooms"] == []


def test_onoff_data_no_log_file_is_empty(dash_paths, monkeypatch):
    cfg, logs = dash_paths
    monkeypatch.setattr(dash, "DECISION_LOG", logs / "mpc_decision_log.csv")  # doesn't exist
    client = dash.app.test_client()
    resp = client.get("/plots/onoff_data.json?ac=bedroom_ac")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["timestamps"] == []


def test_plotly_js_served_locally(dash_paths):
    resp = dash.app.test_client().get("/vendor/plotly.min.js")
    assert resp.status_code == 200
    assert resp.mimetype == "application/javascript"
    assert len(resp.data) > 100_000                  # the real bundle, not a stub
