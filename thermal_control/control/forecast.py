"""
forecast.py
───────────
Fetches outdoor temperature forecast from Open-Meteo (free, no API key).
Returns °F values interpolated to the MPC's 10-minute step resolution.

Falls back gracefully — callers receive None on any network or parse
error and should substitute the current observed outdoor temperature.
"""

import logging
from datetime import datetime, timezone

import numpy as np
import requests

logger = logging.getLogger(__name__)

_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 10  # seconds


def get_forecast_f(lat, lon, n_steps, step_minutes=10):
    """
    Fetch outdoor temperature forecast from Open-Meteo.

    lat, lon     : location coordinates
    n_steps      : number of steps needed (e.g. horizon_steps = 18)
    step_minutes : resolution of the MPC timestep (default 10 min)

    Returns a list of floats (°F) of length n_steps, starting from
    approximately now, at step_minutes resolution.
    Returns None on any network or parse failure.
    """
    hours_needed = (n_steps * step_minutes) // 60 + 2

    try:
        r = requests.get(
            _URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data        = r.json()
        times_str   = data["hourly"]["time"]
        temps_c_all = data["hourly"]["temperature_2m"]
    except Exception as exc:
        logger.warning(f"Forecast fetch failed: {exc}")
        return None

    # Find the index of the current hour so we start the forecast from now,
    # not from midnight. Open-Meteo's hourly array always begins at 00:00 of
    # the requested day, so a naive [:hours_needed] slice would give stale
    # early-morning temperatures when called in the afternoon.
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_idx = 0
    for idx, t in enumerate(times_str):
        # times_str entries are ISO-8601 local strings, e.g. "2026-06-10T14:00"
        t_naive = datetime.fromisoformat(t)
        # Compare naive local time to UTC-expressed current hour using the
        # wall-clock hour only (Open-Meteo returns local-timezone times when
        # timezone=auto, so direct hour comparison is safe).
        if t_naive.replace(tzinfo=None) >= now_hour.replace(tzinfo=None):
            start_idx = idx
            break

    temps_c = temps_c_all[start_idx : start_idx + hours_needed]
    if len(temps_c) < 2:
        logger.warning("Forecast slice too short after aligning to current hour")
        return None

    # Linear interpolation from hourly → step_minutes resolution, then C→F
    x_hourly = np.arange(len(temps_c)) * 60
    x_steps  = np.arange(n_steps) * step_minutes
    interp_c = np.interp(x_steps, x_hourly, temps_c)
    return [c * 9 / 5 + 32 for c in interp_c]


def build_outdoor_series(outdoor_f, house_config, control_config):
    """
    Build the outdoor temperature series for the MPC horizon.

    Uses the Open-Meteo forecast when use_forecast is true; falls back to
    repeating the current observed value. Returns (series, description) so
    callers can log the source without duplicating the fallback logic.

    outdoor_f      : current outdoor temperature in °F (fallback value)
    house_config   : loaded house.yaml dict
    control_config : loaded control.yaml dict
    """
    horizon      = control_config["mpc"]["horizon_steps"]
    step_minutes = control_config["mpc"]["tick_minutes"]

    if control_config["mpc"].get("use_forecast"):
        forecast = get_forecast_f(
            house_config["location"]["lat"],
            house_config["location"]["lon"],
            horizon,
            step_minutes,
        )
        if forecast is not None:
            desc = f"forecast {forecast[0]:.1f}–{forecast[-1]:.1f}°F over horizon"
            return forecast, desc
        desc = f"constant {outdoor_f:.1f}°F (forecast fetch failed)"
        return [outdoor_f] * horizon, desc

    desc = f"constant {outdoor_f:.1f}°F (forecast disabled)"
    return [outdoor_f] * horizon, desc
