"""
schedule.py
───────────
Resolve the active comfort targets for a given wall-clock time.

Priority (highest wins):
  1. Static per-room override  — targets.<room_id> in control.yaml
  2. Active schedule entry     — targets.schedule[i].rooms.<room_id>
  3. Default band              — targets.default

The active schedule entry is the one whose `time` is the latest value
≤ the current local time.  The list wraps midnight: if the current time
is before the first entry's time, the *last* entry in the list is active.
"""

from datetime import datetime, time as dtime


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def resolve_targets(control_cfg: dict, now: datetime) -> dict:
    """
    Return {room_id: {"min_f": float, "max_f": float}} for all modelled rooms.

    control_cfg : the parsed control.yaml dict
    now         : current local datetime (naive or aware — only .time() is used)
    """
    targets_cfg = control_cfg["targets"]
    default     = targets_cfg["default"]
    schedule    = targets_cfg.get("schedule", [])

    RESERVED    = {"default", "schedule"}
    static      = {k: v for k, v in targets_cfg.items() if k not in RESERVED}

    # ── Find active schedule entry ────────────────────────────────────────────
    active_rooms = {}
    if schedule:
        current_time = now.time().replace(second=0, microsecond=0)
        parsed       = sorted(
            [(_parse_hhmm(e["time"]), e["rooms"]) for e in schedule],
            key=lambda x: x[0],
        )

        # Latest entry whose time ≤ now.  If none qualifies (current_time is
        # before the earliest entry), the last entry wraps from the previous day.
        candidates = [(t, rooms) for t, rooms in parsed if t <= current_time]
        _, active_rooms = max(candidates, key=lambda x: x[0]) if candidates else parsed[-1]
        active_rooms = active_rooms or {}

    # ── Collect all room ids mentioned anywhere in the config ─────────────────
    all_rooms = set(static)
    for _, rooms in ([(None, active_rooms)] if active_rooms else []):
        all_rooms |= set(rooms)

    # ── Build per-room target applying priority ───────────────────────────────
    def resolve_room(room_id):
        if room_id in static:
            return dict(static[room_id])
        if room_id in active_rooms:
            return dict(active_rooms[room_id])
        return dict(default)

    return {room_id: resolve_room(room_id) for room_id in all_rooms}


def resolve_active_entry_name(control_cfg: dict, now: datetime) -> str:
    """Return the name of the currently active schedule entry, or 'default'."""
    schedule = control_cfg["targets"].get("schedule", [])
    if not schedule:
        return "default"

    current_time = now.time().replace(second=0, microsecond=0)
    parsed = sorted(
        [(_parse_hhmm(e["time"]), e["name"]) for e in schedule],
        key=lambda x: x[0],
    )
    candidates = [(t, name) for t, name in parsed if t <= current_time]
    _, name = max(candidates, key=lambda x: x[0]) if candidates else parsed[-1]
    return name


def resolve_targets_for_rooms(control_cfg: dict, rooms: list, now: datetime) -> dict:
    """
    Like resolve_targets() but guaranteed to cover exactly `rooms`.
    Rooms not mentioned anywhere in the config get the default band.
    """
    resolved = resolve_targets(control_cfg, now)
    default  = control_cfg["targets"]["default"]
    return {room: resolved.get(room, dict(default)) for room in rooms}
