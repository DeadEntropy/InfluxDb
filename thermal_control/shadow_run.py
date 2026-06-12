"""
shadow_run.py
─────────────
Shadow MPC: reads live sensor data from Home Assistant every minute,
runs the full MPC solve, and logs what the MPC *would* have commanded
alongside what the real thermostats are actually doing — without writing
any setpoints to HA.

Runs for SHADOW_HOURS (default 24) then exits cleanly.
Extract thermal_control/logs/shadow.csv from the container afterward.

Run from the repo root:
    python thermal_control/shadow_run.py
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT      = Path(__file__).parent          # thermal_control/
REPO_ROOT = ROOT.parent                    # repo root
sys.path.insert(0, str(REPO_ROOT))

from thermal_control.model.simulate   import HouseSimulator
from thermal_control.control.mpc      import BangBangMPC
from thermal_control.control.forecast import build_outdoor_series
from thermal_control.control.schedule import resolve_targets_for_rooms, resolve_active_entry_name
from thermal_control.ha_bridge        import controller as ha

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SHADOW_HOURS        = 24
TICK_MINUTES        = 1
STALE_LIMIT_MINUTES = 30
FALLBACK_TEMP_F     = 74.0
SHADOW_LOG          = ROOT / "logs" / "shadow.csv"
WEIGHTS_DIR         = ROOT / "model" / "weights"
HOUSE_YAML          = ROOT / "config" / "house.yaml"
CONTROL_YAML        = ROOT / "config" / "control.yaml"


def fill_missing(current_temps, cache, rooms, fallback_f):
    now          = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=STALE_LIMIT_MINUTES)

    for room_id, temp in current_temps.items():
        cache[room_id] = (temp, now)

    filled, missing_rooms = {}, set()
    for room_id in rooms:
        if room_id in current_temps:
            filled[room_id] = current_temps[room_id]
        elif room_id in cache:
            cached_temp, cached_at = cache[room_id]
            filled[room_id] = cached_temp
            if cached_at < stale_cutoff:
                missing_rooms.add(room_id)
                logger.warning(f"{room_id}: stale >{STALE_LIMIT_MINUTES}min — excluded from cost")
        else:
            filled[room_id] = fallback_f
            missing_rooms.add(room_id)
            logger.warning(f"{room_id}: no reading — excluded from cost")

    return filled, missing_rooms


def append_row(record):
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        write_header = not SHADOW_LOG.exists()
        with open(SHADOW_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(record.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(record)
    except Exception as exc:
        logger.warning(f"Log write failed: {exc}")


def run():
    with open(HOUSE_YAML)   as f: house   = yaml.safe_load(f)
    with open(CONTROL_YAML) as f: control = yaml.safe_load(f)

    local_tz = ZoneInfo(house["location"]["timezone"])
    _local_time = lambda *_: datetime.now(tz=local_tz).timetuple()
    for handler in logging.root.handlers:
        if handler.formatter:
            handler.formatter.converter = _local_time

    sim = HouseSimulator(WEIGHTS_DIR, house)
    mpc = BangBangMPC(sim, house, control)

    tick_seconds  = TICK_MINUTES * 60
    deadline      = datetime.now(tz=local_tz) + timedelta(hours=SHADOW_HOURS)
    sensor_cache  = {}
    outdoor_cache = (FALLBACK_TEMP_F, datetime.min.replace(tzinfo=timezone.utc))
    tick_count    = 0

    logger.info(f"Shadow MPC started — {SHADOW_HOURS}h run, {TICK_MINUTES}-min ticks")
    logger.info(f"Code version: {os.environ.get('MPC_VERSION', 'dev (not containerized)')}")
    logger.info(f"Stopping at {deadline:%Y-%m-%d %H:%M:%S}")
    logger.info(f"Logging to {SHADOW_LOG}")

    while datetime.now(tz=local_tz) < deadline:
        tick_start = time.monotonic()
        tick_count += 1
        logger.info(f"── Tick {tick_count}  {datetime.now(tz=local_tz):%Y-%m-%d %H:%M:%S}")

        try:
            # Room temperatures
            current_temps = ha.get_room_temps(house)
            state, missing_rooms = fill_missing(
                current_temps, sensor_cache, sim.rooms, FALLBACK_TEMP_F
            )

            # Actual AC states — what the thermostats are doing right now
            ac_states = ha.get_ac_states(house)

            # Outdoor temperature
            outdoor = ha.get_outdoor_temp(house)
            if outdoor is not None:
                outdoor_cache = (outdoor, datetime.now(timezone.utc))
            else:
                outdoor, _ = outdoor_cache
                logger.warning(f"Outdoor unavailable, using cached {outdoor:.1f}°F")

            outdoor_series, _ = build_outdoor_series(outdoor, house, control)

            # Resolve schedule and solve (no writes to HA)
            now_dt        = datetime.now(tz=local_tz)
            targets       = resolve_targets_for_rooms(control, sim.rooms, now_dt)
            schedule_name = resolve_active_entry_name(control, now_dt)
            mpc.solve(state, outdoor_series, missing_rooms, targets)

            logger.info(f"Schedule: {schedule_name}")
            logger.info(mpc.explain())

            # Build CSV row
            record = {
                "timestamp":       now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "active_schedule": schedule_name,
                "T_outdoor":       round(outdoor, 1),
                "missing_rooms":   ",".join(sorted(missing_rooms)),
            }
            for room in sim.rooms:
                record[f"T_{room}"] = round(state.get(room, float("nan")), 2)
            for ac_id in mpc.ac_units:
                sp = mpc._best_combo[mpc.ac_units.index(ac_id)]
                record[f"mpc_on_{ac_id}"]        = sp
                record[f"actual_action_{ac_id}"]  = ac_states[ac_id]["hvac_action"]
            record["cost_chosen"] = round(mpc._best_cost, 4)
            for combo, _, cost, _ in mpc._last_results:
                record[f"cost_{''.join(str(c) for c in combo)}"] = round(cost, 4)
            chosen_final = next(
                s[-1] for c, _, _, s in mpc._last_results if c == mpc._best_combo
            )
            for room in sim.rooms:
                record[f"proj_{room}"] = round(chosen_final.get(room, float("nan")), 2)

            append_row(record)

        except Exception as exc:
            logger.error(f"Tick failed: {exc}", exc_info=True)

        elapsed = time.monotonic() - tick_start
        time.sleep(max(0, tick_seconds - elapsed))

    logger.info(f"Shadow run complete — {tick_count} ticks, log at {SHADOW_LOG}")


if __name__ == "__main__":
    run()
