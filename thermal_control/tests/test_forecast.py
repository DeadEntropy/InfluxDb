"""Tests for control/forecast.py — Open-Meteo fetch + outdoor series builder."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from thermal_control.control import forecast as fc


# ── get_forecast_f ──────────────────────────────────────────────────────────
def test_get_forecast_f_interpolates_and_converts(monkeypatch, fake_response):
    # Build hourly times around the real current hour so the script's
    # "align to now" logic picks the right slice without patching datetime.
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now_hour + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")
             for h in (-1, 0, 1, 2)]
    # start_idx aligns to index 1 (now) → slice [20, 26, 30] °C, a rising trend.
    payload = {"hourly": {"time": times, "temperature_2m": [10.0, 20.0, 26.0, 30.0]}}

    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: fake_response(payload))

    out = fc.get_forecast_f(26.0, -80.0, n_steps=6, step_minutes=10)
    assert len(out) == 6
    assert out[0] == pytest.approx(20.0 * 9 / 5 + 32)     # 68°F at t0 (now)
    assert all(o > out[0] for o in out[1:])               # rising trend


def test_get_forecast_f_returns_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(fc.requests, "get", boom)
    assert fc.get_forecast_f(26.0, -80.0, 6) is None


# ── build_outdoor_series ────────────────────────────────────────────────────
def test_build_series_flat_when_forecast_disabled(house_config, control_config):
    cfg = copy.deepcopy(control_config)
    cfg["mpc"]["use_forecast"] = False
    series, desc = fc.build_outdoor_series(88.0, house_config, cfg)
    assert series == [88.0] * cfg["mpc"]["horizon_steps"]
    assert "disabled" in desc


def test_build_series_applies_forecast_trend(monkeypatch, house_config, control_config):
    """NEXT_STEPS item 5: only the forecast *trend* is applied; the level always
    comes from the local sensor (series[0] == sensor reading)."""
    cfg = copy.deepcopy(control_config)
    cfg["mpc"]["use_forecast"] = True
    horizon = cfg["mpc"]["horizon_steps"]
    fake = [70.0 + i for i in range(horizon)]             # +1°F per step trend
    monkeypatch.setattr(fc, "get_forecast_f", lambda *a, **k: fake)

    series, desc = fc.build_outdoor_series(88.0, house_config, cfg)
    assert series[0] == pytest.approx(88.0)               # level from sensor
    assert series[-1] == pytest.approx(88.0 + (horizon - 1))   # trend preserved
    assert "forecast delta" in desc


def test_build_series_fallback_on_fetch_failure(monkeypatch, house_config, control_config):
    cfg = copy.deepcopy(control_config)
    cfg["mpc"]["use_forecast"] = True
    monkeypatch.setattr(fc, "get_forecast_f", lambda *a, **k: None)
    series, desc = fc.build_outdoor_series(88.0, house_config, cfg)
    assert series == [88.0] * cfg["mpc"]["horizon_steps"]
    assert "failed" in desc
