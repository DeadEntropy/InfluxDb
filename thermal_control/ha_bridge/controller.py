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


def get_ac_states(house_config):
    """
    Returns {ac_id: {ac_sensor_temp_f, setpoint_f, hvac_action, ac_on}}
    using the climate entity's current_temperature attribute.
    Raises if any climate entity is unreachable (they are critical).
    """
    result = {}
    for ac in house_config["ac_units"]:
        s     = _get_state(ac["climate_entity"])
        attrs = s["attributes"]
        result[ac["id"]] = {
            "ac_sensor_temp_f": float(attrs["current_temperature"]),
            "setpoint_f":       int(attrs["temperature"]),
            "hvac_action":      attrs.get("hvac_action", "unknown"),
            "ac_on":            int(attrs.get("hvac_action") == "cooling"),
        }
    return result


# ── Writes ────────────────────────────────────────────────────────────────

def apply_setpoints(optimal_setpoints, house_config):
    """
    Writes integer setpoints (°F) to HA climate entities.
    optimal_setpoints: {ac_id: setpoint_F}
    """
    url = f"{os.environ['HA_URL']}/api/services/climate/set_temperature"
    ac_map = {ac["id"]: ac["climate_entity"] for ac in house_config["ac_units"]}

    for ac_id, sp in optimal_setpoints.items():
        entity = ac_map[ac_id]
        r = requests.post(
            url,
            json={"entity_id": entity, "temperature": int(sp)},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        logger.info(f"  {ac_id} ({entity}): setpoint → {sp}°F")
