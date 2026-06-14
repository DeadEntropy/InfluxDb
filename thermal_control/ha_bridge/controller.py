"""
controller.py
─────────────
Home Assistant REST API bridge.

Reads current room temperatures and AC states from HA, and writes
integer setpoints back. All temperatures in °F throughout.

Expects HA_URL and HA_TOKEN in the environment (loaded from .env by
the caller).
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 5  # seconds per HA API call


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
        "Content-Type": "application/json",
    }


def _get_state(entity_id):
    url = f"{os.environ['HA_URL']}/api/states/{entity_id}"
    r = requests.get(url, headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── Startup checks ────────────────────────────────────────────────────────

def check_sensor_units(house_config):
    """
    Called once at scheduler startup. Verifies every room sensor reports in °F
    so a future HA unit-system change (metric switch, sensor replacement) is
    caught immediately rather than silently producing wrong temperatures.
    """
    for room in house_config["rooms"]:
        entity = room.get("sensor_entity")
        if not entity:
            continue
        try:
            s   = _get_state(entity)
            uom = s.get("attributes", {}).get("unit_of_measurement")
            if uom != "°F":
                logger.warning(
                    f"Unexpected unit_of_measurement for {entity}: "
                    f"'{uom}' (expected '°F') — readings may be wrong"
                )
        except Exception as exc:
            logger.warning(f"Could not check units for {entity}: {exc}")


# ── Reads ─────────────────────────────────────────────────────────────────

def get_room_temps(house_config):
    """
    Returns {room_id: temp_F} for modelled rooms whose sensors are available.
    Rooms with null sensor_entity or unavailable state are omitted.
    """
    result = {}
    for room in house_config["rooms"]:
        entity = room.get("sensor_entity")
        if not entity:
            continue
        room_id = room["id"]
        try:
            s = _get_state(entity)
            val = s["state"]
            if val not in ("unavailable", "unknown"):
                result[room_id] = float(val)
            else:
                logger.debug(f"{entity}: {val}")
        except Exception as exc:
            logger.warning(f"Could not read {entity}: {exc}")
    return result


def get_outdoor_temp(house_config):
    """
    Returns outdoor temperature (°F), or None if the sensor is unavailable.
    """
    entity = house_config["outdoor_sensor"]
    try:
        s = _get_state(entity)
        val = s["state"]
        if val not in ("unavailable", "unknown"):
            return float(val)
        logger.warning(f"Outdoor sensor {entity} is {val}")
        return None
    except Exception as exc:
        logger.warning(f"Could not read outdoor sensor {entity}: {exc}")
        return None


def get_away_mode(control_config):
    """
    Return True if holiday/away mode is active (item 8).

    Reads the input_boolean named in control.yaml at targets.away.entity.
    Fails safe to False (home / normal schedule) when no entity is configured
    or the read errors — a flaky helper must never strand the house warm.
    """
    away   = control_config.get("targets", {}).get("away", {})
    entity = away.get("entity")
    if not entity:
        return False
    try:
        return _get_state(entity)["state"] == "on"
    except Exception as exc:
        logger.warning(f"Could not read away mode {entity}: {exc} — assuming home")
        return False


def get_presence(house_config):
    """
    Return {room_id: occupied_bool} for rooms with a presence_entity (item 9).
    'on' = occupied. Fails safe to occupied (True) on any read error or an
    unavailable/unknown state — better to cool an empty room than to stop
    cooling an occupied one. Rooms with no presence_entity are omitted; callers
    treat an absent room as always occupied.
    """
    result = {}
    for room in house_config["rooms"]:
        entity = room.get("presence_entity")
        if not entity:
            continue
        room_id = room["id"]
        try:
            val = _get_state(entity)["state"]
            if val in ("unavailable", "unknown"):
                logger.warning(f"Presence {entity} is {val} — assuming occupied")
                result[room_id] = True
            else:
                result[room_id] = (val == "on")
        except Exception as exc:
            logger.warning(f"Could not read presence {entity}: {exc} — assuming occupied")
            result[room_id] = True
    return result


def get_room_targets(house_config):
    """
    Return {room_id: target_f_int} read from each room's thermostat_entity
    (items 7/7b). The climate entity's target temperature is the user-facing
    UPPER bound of the comfort band. Rooms whose card is unavailable/unknown,
    has no target, or errors are omitted — the caller treats an absent room as
    "no reading" (follow schedule). Rooms with no thermostat_entity are omitted.
    """
    result = {}
    for room in house_config["rooms"]:
        entity = room.get("thermostat_entity")
        if not entity:
            continue
        room_id = room["id"]
        try:
            s = _get_state(entity)
            if s["state"] in ("unavailable", "unknown"):
                logger.warning(f"Thermostat {entity} is {s['state']} — skipped")
                continue
            temp = s.get("attributes", {}).get("temperature")
            if temp is None:
                logger.warning(f"Thermostat {entity} has no target temperature — skipped")
                continue
            result[room_id] = int(round(float(temp)))
        except Exception as exc:
            logger.warning(f"Could not read thermostat {entity}: {exc}")
    return result


def set_room_target(house_config, room_id, temp_f):
    """
    Write an integer target temperature (°F) to a room's thermostat_entity via
    climate.set_temperature (items 7/7b). Used both to keep the card synced to
    the active schedule band and to revert it to the schedule when a user
    override expires. Best-effort: logs and swallows failures so a write error
    never breaks the control loop.
    """
    entity = next(
        (r.get("thermostat_entity") for r in house_config["rooms"] if r["id"] == room_id),
        None,
    )
    if not entity:
        return
    url = f"{os.environ['HA_URL']}/api/services/climate/set_temperature"
    try:
        r = requests.post(
            url,
            json={"entity_id": entity, "temperature": int(temp_f)},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        logger.info(f"Thermostat {room_id} ({entity}): target → {int(temp_f)}°F")
    except Exception as exc:
        logger.warning(f"Could not set thermostat {entity}: {exc}")


def get_ac_states(house_config):
    """
    Returns {ac_id: {hvac_mode, ac_sensor_temp_f, setpoint_f, hvac_action, ac_on}}
    using the climate entity's state and attributes.
    Raises if any climate entity is unreachable (they are critical).
    """
    result = {}
    for ac in house_config["ac_units"]:
        s     = _get_state(ac["climate_entity"])
        attrs = s["attributes"]
        result[ac["id"]] = {
            "hvac_mode":        s["state"],
            "ac_sensor_temp_f": float(attrs["current_temperature"]),
            "setpoint_f":       int(attrs["temperature"]),
            "hvac_action":      attrs.get("hvac_action", "unknown"),
            "ac_on":            int(attrs.get("hvac_action") == "cooling"),
        }
    return result


# ── Writes ────────────────────────────────────────────────────────────────

def set_hvac_mode(ac_id, mode, house_config):
    """
    Sets the hvac_mode of a climate entity (e.g. "cool", "off").
    Used as a corrective action when a unit's mode has drifted unexpectedly.
    """
    entity = next(ac["climate_entity"] for ac in house_config["ac_units"] if ac["id"] == ac_id)
    url    = f"{os.environ['HA_URL']}/api/services/climate/set_hvac_mode"
    r = requests.post(
        url,
        json={"entity_id": entity, "hvac_mode": mode},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    logger.warning(f"  {ac_id} ({entity}): hvac_mode → {mode}")


def apply_setpoints(optimal_setpoints, house_config):
    """
    Writes integer setpoints (°F) to HA climate entities, then reads back
    to verify each write was applied.
    optimal_setpoints: {ac_id: setpoint_F}
    """
    url    = f"{os.environ['HA_URL']}/api/services/climate/set_temperature"
    ac_map = {ac["id"]: ac["climate_entity"] for ac in house_config["ac_units"]}

    failures = []
    for ac_id, sp in optimal_setpoints.items():
        entity = ac_map[ac_id]
        try:
            r = requests.post(
                url,
                json={"entity_id": entity, "temperature": int(sp)},
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            logger.info(f"  {ac_id} ({entity}): setpoint → {sp}°F")
        except Exception as exc:
            logger.error(f"  {ac_id} ({entity}): write failed — {exc}")
            failures.append(ac_id)
    if failures:
        raise RuntimeError(f"Setpoint write failed for: {failures}")

    # Read back and warn on any mismatch — catches silent HA failures where
    # the HTTP call succeeds but the entity state was not actually updated.
    for ac in house_config["ac_units"]:
        ac_id = ac["id"]
        if ac_id not in optimal_setpoints:
            continue
        try:
            s      = _get_state(ac["climate_entity"])
            actual = int(s["attributes"].get("temperature", -1))
            expected = int(optimal_setpoints[ac_id])
            if actual != expected:
                logger.warning(
                    f"  {ac_id}: readback mismatch — wrote {expected}°F, got {actual}°F"
                )
        except Exception as exc:
            logger.warning(f"  {ac_id}: readback check failed — {exc}")
