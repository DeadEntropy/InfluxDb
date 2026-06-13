"""
scheduler.py
────────────
Main 10-minute MPC control loop.

Every tick:
  0. Hot-reload config/*.yaml if edited on disk (volume-mounted in Docker);
     a failed reload keeps the last-good config and never stops the loop
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

import csv
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent          # thermal_control/
REPO_ROOT    = ROOT.parent                    # /workspaces/InfluxDb/
WEIGHTS_DIR  = ROOT / "model" / "weights"
HOUSE_YAML   = ROOT / "config" / "house.yaml"
CONTROL_YAML = ROOT / "config" / "control.yaml"

sys.path.insert(0, str(REPO_ROOT))

from thermal_control.model.simulate      import HouseSimulator
from thermal_control.control.mpc         import BangBangMPC
from thermal_control.control.forecast    import build_outdoor_series
from thermal_control.control.schedule    import resolve_targets_for_rooms, update_override_tracker
from thermal_control.ha_bridge           import controller as ha

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

DECISION_LOG = ROOT / "logs" / "decisions.csv"


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


def _append_decision_log(record):
    """Append one row to the CSV decision log, creating the file if needed."""
    try:
        DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
        write_header = not DECISION_LOG.exists()
        with open(DECISION_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(record.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(record)
    except Exception as exc:
        logger.warning(f"Decision log write failed: {exc}")


def load_configs():
    with open(HOUSE_YAML) as f:
        house = yaml.safe_load(f)
    with open(CONTROL_YAML) as f:
        control = yaml.safe_load(f)
    return house, control


def _config_mtimes():
    return (HOUSE_YAML.stat().st_mtime, CONTROL_YAML.stat().st_mtime)


def _maybe_reload(known_mtimes):
    """
    Detect on-disk changes to house.yaml/control.yaml (volume-mounted in
    Docker, editable on the server) and rebuild the simulator + MPC from
    the new config — also picking up any retrained weights in WEIGHTS_DIR.

    Returns (house, control, sim, mpc, mtimes) on a successful reload,
    None when nothing changed. Every failure (mid-edit YAML, transient
    stat error, bad weights) is logged and swallowed so the running
    controller keeps its last-good config; it retries next tick.
    """
    try:
        mtimes = _config_mtimes()
        if mtimes == known_mtimes:
            return None
        house, control = load_configs()
        sim = HouseSimulator(WEIGHTS_DIR, house)
        mpc = BangBangMPC(sim, house, control)
        logger.info("Config change detected — reloaded house.yaml/control.yaml, "
                    "rebuilt simulator and MPC")
        return house, control, sim, mpc, mtimes
    except Exception as exc:
        logger.error(f"Config reload failed — keeping previous config: {exc}")
        return None


def fill_missing(current_temps, cache, rooms, fallback_f):
    """
    For rooms missing from current_temps, use cached value (if fresh)
    or fallback_f. Updates cache with fresh readings.

    Returns (filled, missing_rooms):
      filled       : {room_id: temp_F} — complete dict for all modelled rooms;
                     stale/never-received rooms get a simulation value but must
                     NOT steer the MPC cost function (see missing_rooms).
      missing_rooms: set of room_ids whose value is fabricated — the MPC
                     excludes these from the discomfort cost for this tick.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=STALE_LIMIT_MINUTES)

    for room_id, temp in current_temps.items():
        cache[room_id] = (temp, now)

    filled = {}
    missing_rooms = set()
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
                # Stale but a real past reading — better propagation estimate
                # than a fabricated 74°F, but excluded from MPC cost this tick.
                filled[room_id] = cached_temp
                missing_rooms.add(room_id)
                logger.warning(
                    f"{room_id}: sensor stale >{STALE_LIMIT_MINUTES} min "
                    f"(last {cached_temp:.1f}°F) — excluded from MPC cost"
                )
        else:
            filled[room_id] = fallback_f
            missing_rooms.add(room_id)
            logger.warning(
                f"{room_id}: no reading ever received — excluded from MPC cost"
            )

    return filled, missing_rooms


def run():
    house, control = load_configs()
    config_mtimes = _config_mtimes()

    local_tz = ZoneInfo(house["location"]["timezone"])
    _local_time = lambda *_: datetime.now(tz=local_tz).timetuple()
    for handler in logging.root.handlers:
        if handler.formatter:
            handler.formatter.converter = _local_time

    sim = HouseSimulator(WEIGHTS_DIR, house)
    mpc = BangBangMPC(sim, house, control)

    tick_seconds = control["mpc"]["tick_minutes"] * 60
    sensor_cache = {}   # {room_id: (temp_F, datetime)}
    outdoor_cache = (FALLBACK_TEMP_F, datetime.min.replace(tzinfo=timezone.utc))
    away_active  = False   # holiday mode (item 8); transitions are logged
    presence_state = {}    # {room_id: occupied_bool} (item 9); transitions logged
    override_tracker = {}  # {room_id: (shift_f, started_at)} (item 7)

    # SIGTERM (systemd/Docker stop) raises SystemExit, which escapes the inner
    # except-Exception block and hits the outer finally → safe setpoints written.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ha.check_sensor_units(house)
    logger.info("Thermal MPC scheduler started")
    logger.info(f"Code version  : {os.environ.get('MPC_VERSION', 'dev (not containerized)')}")
    logger.info(f"Tick interval : {control['mpc']['tick_minutes']} min")
    logger.info(f"Horizon       : {control['mpc']['horizon_steps']} steps "
                f"({control['mpc']['horizon_steps'] * control['mpc']['tick_minutes']} min)")
    logger.info(f"Rooms         : {sim.rooms}")

    try:
        while True:
            tick_start = time.monotonic()
            now = datetime.now(tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"── Tick {now} ─────────────────────────")

            # 0. Hot-reload config edited on the server (never raises)
            reloaded = _maybe_reload(config_mtimes)
            if reloaded:
                house, control, sim, mpc, config_mtimes = reloaded
                tick_seconds = control["mpc"]["tick_minutes"] * 60

            try:
                # 1. Read room temperatures
                current_temps = ha.get_room_temps(house)
                logger.info(f"Live sensors ({len(current_temps)}/{len(sim.rooms)}): "
                            + ", ".join(f"{r}={t:.1f}°F" for r, t in sorted(current_temps.items())))

                state, missing_rooms = fill_missing(
                    current_temps, sensor_cache, sim.rooms, FALLBACK_TEMP_F
                )
                if missing_rooms:
                    logger.warning(
                        f"Missing sensors this tick: {', '.join(sorted(missing_rooms))}"
                    )

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
                    outdoor_cache = (outdoor, datetime.now(timezone.utc))
                    logger.info(f"Outdoor: {outdoor:.1f}°F")
                else:
                    outdoor, _ = outdoor_cache
                    logger.warning(f"Outdoor sensor unavailable, using cached {outdoor:.1f}°F")

                # 5. Build outdoor series for MPC horizon
                outdoor_series, outdoor_desc = build_outdoor_series(
                    outdoor, house, control
                )
                logger.info(f"Outdoor series: {outdoor_desc}")

                # 6. Resolve schedule and solve MPC. Away/holiday mode (item 8)
                #    overrides the schedule with an energy-saving band for every
                #    room; presence (item 9) drops empty rooms to a wide band.
                #    Both override the schedule; away wins over presence. State
                #    transitions are logged so they show up in the history.
                away = ha.get_away_mode(control)
                if away != away_active:
                    logger.warning(f"Away mode {'ACTIVATED' if away else 'DEACTIVATED'}")
                    away_active = away

                now_dt     = datetime.now(tz=local_tz)
                unoccupied = set()
                overrides  = {}
                if away:
                    logger.info("Away mode active — using holiday bands")
                else:
                    presence = ha.get_presence(house)
                    for room, occ in sorted(presence.items()):
                        if presence_state.get(room) != occ:
                            logger.warning(f"Presence {room}: "
                                           f"{'occupied' if occ else 'UNOCCUPIED'}")
                            presence_state[room] = occ
                    unoccupied = {r for r, occ in presence.items() if not occ}
                    if unoccupied:
                        logger.info(f"Unoccupied (MPC ignoring): {', '.join(sorted(unoccupied))}")

                    # Manual short-term overrides (item 7): apply within their
                    # duration window, reset the HA slider to 0 once expired.
                    duration_min = control["mpc"].get("override_duration_minutes", 60)
                    overrides, events = update_override_tracker(
                        ha.get_overrides(house), override_tracker, now_dt, duration_min
                    )
                    for room, kind, shift in events:
                        logger.warning(f"Override {room}: {kind} ({shift:+d}°F)")
                        if kind == "expired":
                            ha.clear_override(house, room)
                    if overrides:
                        logger.info("Active overrides: "
                                    + ", ".join(f"{r}{s:+d}°F" for r, s in sorted(overrides.items())))

                targets = resolve_targets_for_rooms(
                    control, sim.rooms, now_dt,
                    away=away, unoccupied=unoccupied, overrides=overrides,
                )
                setpoints = mpc.solve(state, outdoor_series, missing_rooms, targets)
                logger.info(mpc.explain())

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

                # 8. Apply setpoints (includes read-back verification).
                #    Caught here so a write failure still gets a log record.
                logger.info("Applying setpoints:")
                write_ok = True
                try:
                    ha.apply_setpoints(setpoints, house)
                except Exception as exc:
                    write_ok = False
                    logger.error(f"Setpoint apply failed: {exc}", exc_info=True)

                # 9. Append one row to the decision log CSV
                _append_decision_log({
                    "timestamp": datetime.now(tz=local_tz).isoformat(timespec="seconds"),
                    "T_outdoor": round(outdoor, 1),
                    "write_ok":  write_ok,
                    **mpc.decision_record(),
                })

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
