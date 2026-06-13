"""Tests for the entry-point helpers in scheduler.py and shadow_run.py."""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from thermal_control import scheduler as sc
from thermal_control import shadow_run as sr


# ── fill_missing (KEY FEATURE: stale-sensor caching + cost exclusion) ───────
def test_fill_missing_fresh_cached_stale_and_never_seen():
    now = datetime.now(timezone.utc)
    rooms = ["a", "b", "c", "d"]
    cache = {
        "b": (70.0, now),                                  # fresh cache
        "c": (71.0, now - timedelta(minutes=60)),          # stale (> 30 min)
    }
    current = {"a": 75.0}                                  # only 'a' read this tick

    filled, missing = sc.fill_missing(current, cache, rooms, fallback_f=74.0)

    assert filled == {"a": 75.0, "b": 70.0, "c": 71.0, "d": 74.0}
    assert missing == {"c", "d"}                           # stale + never-seen excluded
    assert "a" not in missing and "b" not in missing       # fresh data steers cost
    assert cache["a"][0] == 75.0                           # fresh reading cached


def test_shadow_fill_missing_matches_scheduler():
    """shadow_run.fill_missing carries the same contract as scheduler's."""
    now = datetime.now(timezone.utc)
    filled, missing = sr.fill_missing({"a": 75.0}, {}, ["a", "b"], 74.0)
    assert filled == {"a": 75.0, "b": 74.0}
    assert missing == {"b"}


# ── load_configs / _config_mtimes ───────────────────────────────────────────
def test_load_configs_returns_house_and_control():
    house, control = sc.load_configs()
    assert "ac_units" in house and "rooms" in house
    assert "mpc" in control and "targets" in control


def test_config_mtimes_returns_two_floats():
    mtimes = sc._config_mtimes()
    assert len(mtimes) == 2
    assert all(isinstance(m, float) for m in mtimes)


# ── _maybe_reload (NEXT_STEPS item 3: config hot-reload) ────────────────────
def test_maybe_reload_noop_when_unchanged():
    assert sc._maybe_reload(sc._config_mtimes()) is None


def test_maybe_reload_rebuilds_on_change():
    result = sc._maybe_reload((0.0, 0.0))                  # stale mtimes → reload
    assert result is not None
    house, control, sim, mpc, mtimes = result
    assert sim.rooms and mpc.ac_units
    assert mtimes == sc._config_mtimes()


def test_maybe_reload_keeps_last_good_on_invalid_yaml(monkeypatch):
    def boom():
        raise ValueError("mid-edit yaml")
    monkeypatch.setattr(sc, "load_configs", boom)
    assert sc._maybe_reload((0.0, 0.0)) is None            # failure swallowed → last-good kept


# ── _write_safe_setpoints ───────────────────────────────────────────────────
def test_write_safe_setpoints(monkeypatch, house_config):
    captured = {}
    monkeypatch.setattr(sc.ha, "apply_setpoints",
                        lambda sp, house: captured.update(sp))
    sc._write_safe_setpoints(house_config)
    assert set(captured) == {ac["id"] for ac in house_config["ac_units"]}
    assert set(captured.values()) == {sc.SAFE_SETPOINT_F}


# ── _append_decision_log ────────────────────────────────────────────────────
def test_append_decision_log_writes_header_then_row(monkeypatch, tmp_path):
    log = tmp_path / "decisions.csv"
    monkeypatch.setattr(sc, "DECISION_LOG", log)
    sc._append_decision_log({"a": 1, "b": 2})
    sc._append_decision_log({"a": 3, "b": 4})
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


# ── shadow_run.append_row ───────────────────────────────────────────────────
def test_shadow_append_row(monkeypatch, tmp_path):
    log = tmp_path / "shadow.csv"
    monkeypatch.setattr(sr, "SHADOW_LOG", log)
    sr.append_row({"x": 10, "y": 20})
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"x": "10", "y": "20"}]
