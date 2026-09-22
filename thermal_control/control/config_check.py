"""
config_check.py
───────────────
Shared structural validation for the hot-reloaded config (house.yaml /
control.yaml).

Two consumers:
  • scheduler.py rejects a bad edit before it replaces the live config.
  • dashboard/app.py re-validates the on-disk config to decide whether to
    show a live "config broken" banner.

Two severities:
  • validate_config_structure() RAISES — the edit is rejected outright and the
    MPC keeps running its last-good config (red banner).
  • collect_band_warnings() RETURNS a list — the edit is accepted and live, but
    a presence-conditional band (item 11) was malformed and silently degraded to
    its unconditional reading (amber banner). Never rejects: a typo in one room's
    presence rule must not strand the whole house on a stale config.

Kept dependency-free (plain dict checks, plus schedule.classify_band so the
warning walk and the runtime picker classify from one definition) so importing
it from either process is cheap and has no side effects.
"""

from thermal_control.control.schedule import (
    CONDITIONAL, INVALID, MIXED, classify_band,
)

# Keys the runtime indexes unconditionally — a syntactically valid file that
# drops one of these would otherwise blow up mid-tick, so we reject it first.
# Pure-syntax errors (missing commas/brackets/indent) are caught earlier, when
# the yaml is parsed.
REQUIRED_HOUSE_KEYS   = ("rooms", "ac_units", "location")
REQUIRED_CONTROL_KEYS = ("mpc", "targets")
REQUIRED_MPC_KEYS     = ("tick_minutes", "horizon_steps",
                         "setpoint_on_f", "setpoint_off_f")


def validate_config_structure(house, control):
    """
    Raise ValueError if the parsed config is missing structure the control loop
    depends on. Deliberately shallow — it guards the keys read without a guard;
    the HouseSimulator/BangBangMPC constructors are the deeper check for
    anything room/AC-specific.
    """
    if not isinstance(house, dict):
        raise ValueError("house.yaml: top-level YAML is not a mapping")
    if not isinstance(control, dict):
        raise ValueError("control.yaml: top-level YAML is not a mapping")
    for key in REQUIRED_HOUSE_KEYS:
        if key not in house:
            raise ValueError(f"house.yaml: missing required key '{key}'")
    if "timezone" not in house["location"]:
        raise ValueError("house.yaml: missing required key 'location.timezone'")
    if not house["rooms"]:
        raise ValueError("house.yaml: 'rooms' is empty")
    for key in REQUIRED_CONTROL_KEYS:
        if key not in control:
            raise ValueError(f"control.yaml: missing required key '{key}'")
    for key in REQUIRED_MPC_KEYS:
        if key not in control["mpc"]:
            raise ValueError(f"control.yaml: missing required key 'mpc.{key}'")


def _presence_rooms(house):
    """Room ids that actually have a presence sensor declared in house.yaml."""
    if not isinstance(house, dict):
        return set()
    return {r["id"] for r in house.get("rooms") or []
            if isinstance(r, dict) and r.get("id") and r.get("presence_entity")}


def collect_band_warnings(house, control):
    """
    Return a list of human-readable problems with presence-conditional bands
    (item 11). Empty list = clean. Never raises — a malformed band degrades at
    runtime (see schedule._pick_band) rather than rejecting the config, and this
    is how that silent degradation gets surfaced.

    Walks *every* schedule entry, not just the one active now, so a broken 22:00
    rule shows up at 10:00 instead of only once it goes live.
    """
    warnings = []
    targets = control.get("targets") if isinstance(control, dict) else None
    if not isinstance(targets, dict):
        return warnings                      # structure errors are the raiser's job
    schedule = targets.get("schedule")
    if not isinstance(schedule, list):
        schedule = []

    has_presence = _presence_rooms(house)

    for i, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            continue
        label = entry.get("name") or f"schedule[{i}]"
        rooms = entry.get("rooms")
        if not isinstance(rooms, dict):
            continue
        for room, value in rooms.items():
            shape, problems = classify_band(value)
            for problem in problems:
                warnings.append(f"{label} / {room}: {problem}")
            if shape == MIXED:
                band = value.get("min_f"), value.get("max_f")
                warnings.append(
                    f"{label} / {room}: using the flat band "
                    f"{band[0]}–{band[1]}°F and ignoring occupied/unoccupied"
                )
            elif shape == INVALID:
                warnings.append(
                    f"{label} / {room}: band unusable, falling back to the "
                    f"static/default band for this room"
                )
            elif shape == CONDITIONAL and room not in has_presence:
                # get_presence() omits rooms with no presence_entity and callers
                # fail safe to occupied, so such a rule silently pins the room to
                # its 'occupied' branch forever.
                warnings.append(
                    f"{label} / {room}: presence-conditional band but no "
                    f"'presence_entity' for '{room}' in house.yaml — the "
                    f"'occupied' branch will always be used"
                )

    # Conditional bands are a schedule-only grammar: default and the static
    # per-room block beat the schedule at all times, so one there would be an
    # always-on presence rule. _pick_band is never consulted for them, meaning a
    # conditional would silently resolve to garbage — flag it.
    RESERVED = {"schedule", "away"}
    for key, value in targets.items():
        if key in RESERVED or not isinstance(value, dict):
            continue
        if any(k in value for k in ("occupied", "unoccupied")):
            where = "targets.default" if key == "default" else f"targets.{key}"
            warnings.append(
                f"{where}: occupied/unoccupied is only supported inside a "
                f"schedule entry; this band will be used as-is and the "
                f"presence keys ignored"
            )

    return warnings
