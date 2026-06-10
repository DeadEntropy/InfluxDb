"""
scheduler.py
────────────
Main 10-minute MPC control loop.

Every tick:
  1. Read all room temperatures from independent Zigbee sensors via HA REST API
  2. Read outdoor temperature from pool soffit sensor
  3. Solve bang-bang MPC over all 2³=8 AC combinations
  4. Write optimal setpoints to HA climate entities
  5. Log the decision and MPC explanation

Unavailable sensors: the last known reading is cached and reused for
up to STALE_LIMIT_MINUTES. If no reading has ever been received for a
room, a neutral fallback temperature is used so the MPC can still run.

Run from the repo root:
    cd /workspaces/InfluxDb
    python thermal_control/scheduler.py
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent          # thermal_control/
REPO_ROOT    = ROOT.parent                    # /workspaces/InfluxDb/
WEIGHTS_DIR  = ROOT / "model" / "weights"
HOUSE_YAML   = ROOT / "config" / "house.yaml"
CONTROL_YAML = ROOT / "config" / "control.yaml"

sys.path.insert(0, str(REPO_ROOT))

from thermal_control.model.simulate  import HouseSimulator
from thermal_control.control.mpc     import BangBangMPC
from thermal_control.control.forecast import get_forecast_f
from thermal_control.ha_bridge       import controller as ha

# ── Config ────────────────────────────────────────────────────────────────
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STALE_LIMIT_MINUTES = 30   # drop cached reading after this long
FALLBACK_TEMP_F     = 74.0 # used when a room has no reading at all
SAFE_SETPOINT_F     = 76   # written to all ACs on any exit path


def _write_safe_setpoints(house):
    """
    Write a neutral setpoint to every AC unit on any exit path.
    76°F hands control back to each thermostat's own sensor — neither
    forcing the AC on nor forcing it off. Called from the finally block
    so it runs on KeyboardInterrupt, SIGTERM, and unhandled exceptions.
    """
    safe = {ac["id"]: SAFE_SETPOINT_F for ac in house["ac_units"]}
    try:
        ha.apply_setpoints(safe, house)
        logger.info(f"Safe setpoints written ({SAFE_SETPOINT_F}°F) — exiting")
    except Exception as exc:
        logger.error(f"Failed to write safe setpoints on exit: {exc}")


def load_configs():
    with open(HOUSE_YAML) as f:
        house = yaml.safe_load(f)
    with open(CONTROL_YAML) as f:
        control = yaml.safe_load(f)
    return house, control


def fill_missing(current_temps, cache, rooms, fallback_f):
    """
    For rooms missing from current_temps, use cached value (if fresh)
    or fallback_f. Updates cache with fresh readings.
    Returns complete {room_id: temp_F} for all modelled rooms.
    """
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=STALE_LIMIT_MINUTES)

    for room_id, temp in current_temps.items():
        cache[room_id] = (temp, now)

    filled = {}
    for room_id in rooms:
        if room_id in current_temps:
            filled[room_id] = current_temps[room_id]
        elif room_id in cache:
            cached_temp, cached_at = cache[room_id]
            if cached_at >= stale_cutoff:
                filled[room_id] = cached_temp
                logger.debug(f"{room_id}: using cached {cached_temp:.1f}°F "
                             f"(age {(now - cached_at).seconds // 60} min)")
            else:
                filled[room_id] = fallback_f
                logger.warning(f"{room_id}: cache stale, using fallback {fallback_f}°F")
        else:
            filled[room_id] = fallback_f
            logger.warning(f"{room_id}: no reading ever received, using fallback {fallback_f}°F")

    return filled


def run():
    house, control = load_configs()
    sim = HouseSimulator(WEIGHTS_DIR, house)
    mpc = BangBangMPC(sim, house, control)

    tick_seconds = control["mpc"]["tick_minutes"] * 60
    sensor_cache = {}   # {room_id: (temp_F, datetime)}
    outdoor_cache = (FALLBACK_TEMP_F, datetime.min)

    # SIGTERM (systemd/Docker stop) raises SystemExit, which escapes the inner
    # except-Exception block and hits the outer finally → safe setpoints written.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    logger.info("Thermal MPC scheduler started")
    logger.info(f"Tick interval : {control['mpc']['tick_minutes']} min")
    logger.info(f"Horizon       : {control['mpc']['horizon_steps']} steps "
                f"({control['mpc']['horizon_steps'] * control['mpc']['tick_minutes']} min)")
    logger.info(f"Rooms         : {sim.rooms}")

    try:
        while True:
            tick_start = time.monotonic()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"── Tick {now} ─────────────────────────")

            try:
                # 1. Read room temperatures
                current_temps = ha.get_room_temps(house)
                logger.info(f"Live sensors ({len(current_temps)}/{len(sim.rooms)}): "
                            + ", ".join(f"{r}={t:.1f}°F" for r, t in sorted(current_temps.items())))

                state = fill_missing(current_temps, sensor_cache, sim.rooms, FALLBACK_TEMP_F)

                # 2. Read AC states — mode + action logged each tick so anomalies
                #    (unexpected mode drift) are visible in the log history.
                ac_states = ha.get_ac_states(house)
                for ac_id, s in ac_states.items():
                    logger.info(f"  {ac_id}: mode={s['hvac_mode']}  "
                                f"action={s['hvac_action']}  "
                                f"sensor={s['ac_sensor_temp_f']:.1f}°F  "
                                f"setpoint={s['setpoint_f']}°F")

                # 3. Outdoor temperature
                outdoor = ha.get_outdoor_temp(house)
                if outdoor is not None:
                    outdoor_cache = (outdoor, datetime.utcnow())
                    logger.info(f"Outdoor: {outdoor:.1f}°F")
                else:
                    outdoor, _ = outdoor_cache
                    logger.warning(f"Outdoor sensor unavailable, using cached {outdoor:.1f}°F")

                # 5. Build outdoor series for MPC horizon
                horizon = control["mpc"]["horizon_steps"]
                if control["mpc"].get("use_forecast"):
                    forecast = get_forecast_f(
                        house["location"]["lat"],
                        house["location"]["lon"],
                        horizon,
                    )
                    if forecast is not None:
                        outdoor_series = forecast
                        logger.info(f"Forecast: {forecast[0]:.1f}–{forecast[-1]:.1f}°F over horizon")
                    else:
                        outdoor_series = [outdoor] * horizon
                        logger.warning("Forecast unavailable, using constant outdoor temp")
                else:
                    outdoor_series = [outdoor] * horizon

                # 6. Solve MPC
                setpoints = mpc.solve(state, outdoor_series)
                mpc.explain()

                # 7. Correct hvac_mode for any ON-commanded unit that has drifted
                #    out of 'cool' mode (HA restart, power blip). Units are normally
                #    always left in 'cool' so this fires only on anomalous ticks.
                setpoint_on = control["mpc"]["setpoint_on_f"]
                for ac_id, sp in setpoints.items():
                    if sp == setpoint_on and ac_states[ac_id]["hvac_mode"] != "cool":
                        logger.warning(
                            f"{ac_id}: hvac_mode='{ac_states[ac_id]['hvac_mode']}' "
                            f"but MPC wants ON — correcting to 'cool'"
                        )
                        ha.set_hvac_mode(ac_id, "cool", house)

                # 8. Apply setpoints (includes read-back verification)
                logger.info("Applying setpoints:")
                ha.apply_setpoints(setpoints, house)

            except Exception as exc:
                logger.error(f"Tick failed: {exc}", exc_info=True)

            # Sleep for the remainder of the tick interval
            elapsed = time.monotonic() - tick_start
            sleep_s = max(0, tick_seconds - elapsed)
            logger.info(f"Tick done in {elapsed:.1f}s, sleeping {sleep_s:.0f}s")
            time.sleep(sleep_s)

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received")
    finally:
        _write_safe_setpoints(house)


if __name__ == "__main__":
    run()
