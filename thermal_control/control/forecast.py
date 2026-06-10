"""
forecast.py
───────────
Fetches outdoor temperature forecast from Open-Meteo (free, no API key).
Returns °F values interpolated to the MPC's 10-minute step resolution.

Falls back gracefully — callers receive None on any network or parse
error and should substitute the current observed outdoor temperature.
"""

import logging

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
        temps_c = r.json()["hourly"]["temperature_2m"][:hours_needed]
    except Exception as exc:
        logger.warning(f"Forecast fetch failed: {exc}")
        return None

    # Linear interpolation from hourly → step_minutes resolution, then C→F
    x_hourly = np.arange(len(temps_c)) * 60
    x_steps  = np.arange(n_steps) * step_minutes
    interp_c = np.interp(x_steps, x_hourly, temps_c)
    return [c * 9 / 5 + 32 for c in interp_c]
