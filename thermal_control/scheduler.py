"""
scheduler.py
────────────
Main 10-minute MPC control loop.

Every tick:
  0. Hot-reload config/*.yaml if edited on disk (volume-mounted in Docker);
     the edit is validated first — an invalid one (broken yaml or a missing
     required key) is rejected, the last-good config is kept, the failure is
     recorded in logs/errors.log, and the loop never stops
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
from thermal_control.control.config_check import validate_config_structure as _validate_config_structure
from thermal_control.ha_bridge           import controller as ha
from thermal_control                     import log_writer

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

DECISION_LOG    = ROOT / "logs" / "mpc_decision_log.csv"
USER_EVENT_LOG  = ROOT / "logs" / "user_inputs.log"
VERSION_FILE    = ROOT / "logs" / "mpc_version.txt"   # read by the dashboard footer
ERROR_LOG       = ROOT / "logs" / "errors.log"        # rejected config reloads land here


def _make_config_error_logger():
    """
    Dedicated logger for rejected config hot-reloads, writing to errors.log.

    Kept separate from the main INFO stream so an operator who edits a yaml on
    the server has one file to tail for "did my edit take?". propagate=False so
    these lines don't also flood stdout; the main logger still records a copy.
    """
    el = logging.getLogger("thermal_control.config_errors")
    el.setLevel(logging.ERROR)
    el.propagate = False
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(ERROR_LOG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        el.addHandler(handler)
    except OSError as exc:
        logger.warning(f"Could not open {ERROR_LOG}: {exc}")
    return el


config_error_logger = _make_config_error_logger()
_last_reload_error_mtimes = None   # de-dupes repeated errors for one broken edit


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


def _append_user_event(ts, event, room="", value=""):
    """
    Append one row to the user-event log (user_inputs.log).

    Columns:
      timestamp  ISO-8601 local time of the event
      event      one of: away_activated, away_deactivated,
                         presence_occupied, presence_unoccupied,
                         override_activated, override_changed,
                         override_expired, override_cancelled
      room       room_id (empty for away-mode events)
      value      target_f for override events; empty otherwise
    """
    log_writer.append_csv_row(USER_EVENT_LOG, {
        "timestamp": ts, "event": event, "room": room, "value": value,
    })


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

    The edited config is validated (yaml syntax → required structure →
    simulator/MPC build) BEFORE it replaces the live config. Returns
    (house, control, sim, mpc, mtimes) on a successful, validated reload and
    None otherwise. On any failure (mid-edit/broken yaml, missing key, bad
    weights) the running controller keeps its last-good config; the failure is
    recorded in errors.log and retried on the next edit.
    """
    global _last_reload_error_mtimes
    try:
        mtimes = _config_mtimes()
    except OSError as exc:
        logger.warning(f"Could not stat config files — keeping previous config: {exc}")
        return None
    if mtimes == known_mtimes:
        return None

    try:
        house, control = load_configs()
        _validate_config_structure(house, control)
        sim = HouseSimulator(WEIGHTS_DIR, house)
        mpc = BangBangMPC(sim, house, control)
    except Exception as exc:
        msg = (f"Invalid config edit rejected — keeping previous live config "
               f"({type(exc).__name__}: {exc})")
        logger.error(msg)
        # De-dupe: log each distinct broken edit to errors.log once, not on
        # every tick it stays broken (mtimes are unchanged until the next save).
        if mtimes != _last_reload_error_mtimes:
            config_error_logger.error(msg)
            _last_reload_error_mtimes = mtimes
        return None

    _last_reload_error_mtimes = None
    logger.info("Config change detected — validated and reloaded "
                "house.yaml/control.yaml, rebuilt simulator and MPC")
    return house, control, sim, mpc, mtimes


def _input_signature(house, control):
    """
    Cheap snapshot of the user-controllable inputs the HA app changes — the
    per-room thermostat targets (items 7/7b) and the away toggle (item 8).
    Compared during the inter-tick sleep so the loop can wake early when the user
    drags a card instead of waiting up to a full tick. The baseline is captured
    at the end of a tick (after the scheduler's own write-backs), so a steady
    state reads stable and only a real user edit trips it. Fails safe so a
    transient HA glitch at most causes one harmless extra tick.
    """
    try:
        targets = ha.get_room_targets(house)
    except Exception:
        targets = {}
    return (tuple(sorted(targets.items())), ha.get_away_mode(control))


def _responsive_sleep(sleep_s, poll_s, house, control, baseline):
    """
    Sleep up to sleep_s, waking early (within poll_s) if a user input changes
    vs `baseline`. Returns True when woken early by a change, False on timeout.
    """
    deadline = time.monotonic() + sleep_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_s, remaining))
        if time.monotonic() >= deadline:
            return False
        if _input_signature(house, control) != baseline:
            return True


def _plan_card_detection(raw_targets, display_target, card_synced, away):
    """
    Reconcile per-room thermostat cards (item 7b) against what the scheduler last
    wrote (``card_synced``) and detect user edits. Pure: returns the writes/edits
    to act on; the caller performs the HA writes and updates ``card_synced``.

      raw_targets    : {room: target_f} read from the cards this tick (absent =
                       unavailable).
      display_target : {room: upper_bound_f} the card should show — the away band
                       max when away, else the scheduled band max.
      card_synced    : {room: last_value_the_scheduler_wrote}; room absent = card
                       not yet seeded.

    Returns (detected, writes):
      detected : {room: target_f} home-mode user edits (candidate overrides).
      writes   : {room: target_f} cards to (re)write now — first-tick seeding and
                 away-mode pinning (away beats override, so edits are ignored and
                 the card is held on the away target).
    """
    detected, writes = {}, {}
    for room, S in display_target.items():
        T    = raw_targets.get(room)
        last = card_synced.get(room)
        if last is None:                       # first sight → seed to schedule/away
            writes[room] = S
        elif away:                             # away pins the card; ignore edits
            if T != S:
                writes[room] = S
        elif T is not None and T != last:      # user dragged the card → override
            detected[room] = T
    return detected, writes


def _plan_card_revert(raw_targets, display_target, card_synced, effective):
    """
    Home-mode follow-up to _plan_card_detection: keep cards for rooms *not* under
    an active override pinned to the schedule — resyncing on a schedule
    transition and reverting on override expiry/cancel. Returns {room: target_f}
    writes to apply (caller updates ``card_synced``).
    """
    writes = {}
    for room, S in display_target.items():
        if room in effective:                  # leave the user's value showing
            continue
        T = raw_targets.get(room)
        if card_synced.get(room) != S or (T is not None and T != S):
            writes[room] = S
    return writes


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
    for handler in logging.root.handlers + config_error_logger.handlers:
        if handler.formatter:
            handler.formatter.converter = _local_time

    sim = HouseSimulator(WEIGHTS_DIR, house)
    mpc = BangBangMPC(sim, house, control)

    tick_seconds = control["mpc"]["tick_minutes"] * 60
    sensor_cache = {}   # {room_id: (temp_F, datetime)}
    outdoor_cache = (FALLBACK_TEMP_F, datetime.min.replace(tzinfo=timezone.utc))
    away_active  = False   # holiday mode (item 8); transitions are logged
    presence_state = {}    # {room_id: occupied_bool} (item 9); transitions logged
    override_tracker = {}  # {room_id: (target_f, started_at)} (items 7/7b)
    card_synced  = {}      # {room_id: last target the scheduler wrote} (item 7b)

    # SIGTERM (systemd/Docker stop) raises SystemExit, which escapes the inner
    # except-Exception block and hits the outer finally → safe setpoints written.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ha.check_sensor_units(house)
    logger.info("Thermal MPC scheduler started")
    mpc_version = os.environ.get("MPC_VERSION", "dev (not containerized)")
    logger.info(f"Code version  : {mpc_version}")
    # Stamp the version into the shared logs dir so the (separate) dashboard
    # container can surface which MPC build is live. Best-effort: never fatal.
    log_writer.write_version_file(VERSION_FILE, mpc_version)
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
                now_dt = datetime.now(tz=local_tz)
                if away != away_active:
                    event_name = "away_activated" if away else "away_deactivated"
                    logger.warning(f"Away mode {'ACTIVATED' if away else 'DEACTIVATED'}")
                    _append_user_event(now_dt.isoformat(timespec="seconds"), event_name)
                    away_active = away

                unoccupied = set()
                overrides  = {}

                # 6a. Thermostat cards (item 7b). Each card shows an upper-bound
                #     target: the away band max when away, else the scheduled max.
                #     The scheduler keeps the card synced to that and reads it back
                #     to detect user edits (a value ≠ what it last wrote).
                display_bands  = resolve_targets_for_rooms(control, sim.rooms, now_dt, away=away)
                display_target = {r["id"]: display_bands[r["id"]]["max_f"]
                                  for r in house["rooms"] if r.get("thermostat_entity")}
                raw_targets    = ha.get_room_targets(house)
                detected, writes = _plan_card_detection(
                    raw_targets, display_target, card_synced, away
                )
                for room, val in writes.items():
                    ha.set_room_target(house, room, val)
                    card_synced[room] = val

                if away:
                    logger.info("Away mode active — using holiday bands")
                    override_tracker.clear()   # away beats override; drop any tracked
                else:
                    presence = ha.get_presence(house)
                    for room, occ in sorted(presence.items()):
                        if presence_state.get(room) != occ:
                            logger.warning(f"Presence {room}: "
                                           f"{'occupied' if occ else 'UNOCCUPIED'}")
                            _append_user_event(
                                now_dt.isoformat(timespec="seconds"),
                                "presence_occupied" if occ else "presence_unoccupied",
                                room=room,
                            )
                            presence_state[room] = occ
                    unoccupied = {r for r, occ in presence.items() if not occ}
                    if unoccupied:
                        logger.info(f"Unoccupied (MPC ignoring): {', '.join(sorted(unoccupied))}")

                    # Manual overrides (items 7/7b): a card edited away from the
                    # schedule holds for override_duration_minutes, then reverts.
                    duration_min = control["mpc"].get("override_duration_minutes", 60)
                    overrides, events = update_override_tracker(
                        detected, override_tracker, now_dt, duration_min
                    )
                    for room, kind, target in events:
                        logger.warning(f"Override {room}: {kind} (→ {target}°F)")
                        _append_user_event(
                            now_dt.isoformat(timespec="seconds"),
                            f"override_{kind}",
                            room=room,
                            value=str(target),
                        )
                    if overrides:
                        logger.info("Active overrides: "
                                    + ", ".join(f"{r}→{t}°F" for r, t in sorted(overrides.items())))

                    # Resync/revert cards not under an active override (schedule
                    # transition, or override just expired/cancelled).
                    revert = _plan_card_revert(
                        raw_targets, display_target, card_synced, overrides
                    )
                    for room, val in revert.items():
                        ha.set_room_target(house, room, val)
                        card_synced[room] = val

                targets = resolve_targets_for_rooms(
                    control, sim.rooms, now_dt,
                    away=away, unoccupied=unoccupied, override_targets=overrides,
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
                log_writer.append_csv_row(DECISION_LOG, {
                    "timestamp": datetime.now(tz=local_tz).isoformat(timespec="seconds"),
                    "T_outdoor": round(outdoor, 1),
                    "write_ok":  write_ok,
                    **mpc.decision_record(),
                })

            except Exception as exc:
                logger.error(f"Tick failed: {exc}", exc_info=True)

            # Sleep for the remainder of the tick interval, but poll the app's
            # override sliders / away toggle while sleeping and wake early when
            # one changes so a target edit takes effect in seconds, not minutes.
            elapsed = time.monotonic() - tick_start
            sleep_s = max(0, tick_seconds - elapsed)
            poll_s  = max(5, control["mpc"].get("poll_seconds", 20))
            logger.info(f"Tick done in {elapsed:.1f}s, sleeping {sleep_s:.0f}s "
                        f"(polling app targets every {poll_s}s)")
            baseline = _input_signature(house, control)
            if _responsive_sleep(sleep_s, poll_s, house, control, baseline):
                logger.info("App target changed — waking early to re-solve")

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received")
    finally:
        _write_safe_setpoints(house)


if __name__ == "__main__":
    run()
