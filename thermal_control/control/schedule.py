"""
schedule.py
───────────
Resolve the active comfort targets for a given wall-clock time.

Priority (highest wins):
  0. Away / holiday mode       — targets.away (item 8), when its HA toggle is
                                 ON: substitutes the away band for every room,
                                 overriding everything below (handled by the
                                 caller passing away=True to resolve_*_for_rooms)
  0a. Manual override (items 7/7b) — a per-room thermostat target sets the band's
                                 UPPER bound to the user's value, with the lower
                                 bound following at the scheduled width (caller
                                 passes override_targets=). Beats presence (an
                                 explicit request means condition the room) but
                                 yields to away mode; expiry handled by the
                                 scheduler.
  0b. Presence (unoccupied)    — item 9: a room reported empty by its presence
                                 sensor drops to the wide 65–85°F "don't care"
                                 band, overriding the schedule/static below but
                                 yielding to away mode and a manual override
                                 (caller passes unoccupied=)
  1. Static per-room override  — targets.<room_id> in control.yaml
  2. Active schedule entry     — targets.schedule[i].rooms.<room_id>
  3. Default band              — targets.default

The active schedule entry is the one whose `time` is the latest value
≤ the current local time.  The list wraps midnight: if the current time
is before the first entry's time, the *last* entry in the list is active.

Each entry may carry an optional `days` field selecting the days it applies
to: "weekday" (Mon–Fri) or "weekend" (Sat+Sun). Entries with no `days` field
apply every day (backwards-compatible). Entries not matching the current day
are excluded before the latest-time-wins selection runs, so e.g. a weekday-only
06:00 entry leaves the preceding (every-day) block active on a weekend until the
weekend entry's own time is reached.
"""

from datetime import datetime, time as dtime, timedelta

# Wide "don't care" band — zero discomfort penalty at any realistic indoor
# temperature, so the MPC ignores the room. Used for unoccupied rooms (item 9).
WIDE_BAND = {"min_f": 65, "max_f": 85}


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _entry_applies(entry: dict, now: datetime) -> bool:
    """True if `entry`'s optional `days` field matches now's weekday."""
    days = entry.get("days")
    if days is None:
        return True
    is_weekend = now.weekday() >= 5          # Mon=0 … Sat=5, Sun=6
    if days == "weekend":
        return is_weekend
    if days == "weekday":
        return not is_weekend
    raise ValueError(f"Unknown schedule 'days' value: {days!r} "
                     f"(expected 'weekday', 'weekend', or absent)")


def _active_entry(schedule: list, now: datetime) -> dict | None:
    """Return the schedule entry active at `now`, or None if none apply.

    Filters by each entry's `days` field, then picks the latest entry whose
    `time` ≤ now, wrapping midnight to the day's last entry when now precedes
    the earliest.
    """
    applicable = [e for e in schedule if _entry_applies(e, now)]
    if not applicable:
        return None
    current_time = now.time().replace(second=0, microsecond=0)
    by_time   = sorted(applicable, key=lambda e: _parse_hhmm(e["time"]))
    candidates = [e for e in by_time if _parse_hhmm(e["time"]) <= current_time]
    return candidates[-1] if candidates else by_time[-1]


def resolve_targets(control_cfg: dict, now: datetime) -> dict:
    """
    Return {room_id: {"min_f": float, "max_f": float}} for all modelled rooms.

    control_cfg : the parsed control.yaml dict
    now         : current local datetime (naive or aware — only .time() is used)
    """
    targets_cfg = control_cfg["targets"]
    default     = targets_cfg["default"]
    schedule    = targets_cfg.get("schedule", [])

    RESERVED    = {"default", "schedule", "away"}
    static      = {k: v for k, v in targets_cfg.items() if k not in RESERVED}

    # ── Find active schedule entry ────────────────────────────────────────────
    entry        = _active_entry(schedule, now) if schedule else None
    active_rooms = (entry.get("rooms") if entry else None) or {}

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
    entry    = _active_entry(schedule, now) if schedule else None
    return entry["name"] if entry else "default"


def away_targets(control_cfg: dict, rooms: list) -> dict:
    """
    Return the away/holiday band (item 8) for every room in `rooms`.

    Each room uses its per-room override under targets.away.rooms if present,
    otherwise targets.away.default. A missing away block falls back to a 76–80°F
    energy-saving band so the feature is safe even if control.yaml omits it.
    """
    away     = control_cfg["targets"].get("away", {})
    default  = away.get("default") or {"min_f": 76, "max_f": 80}
    per_room = away.get("rooms") or {}
    return {room: dict(per_room.get(room, default)) for room in rooms}


def resolve_targets_for_rooms(control_cfg: dict, rooms: list, now: datetime,
                              away: bool = False, unoccupied=None,
                              override_targets=None) -> dict:
    """
    Like resolve_targets() but guaranteed to cover exactly `rooms`.
    Rooms not mentioned anywhere in the config get the default band.

    away             : when True, holiday mode (item 8) overrides everything and
                       every room gets its away band instead (presence/overrides
                       ignored).
    unoccupied       : set of room_ids reported empty by their presence sensor
                       (item 9); each drops to the wide 65–85°F "don't care" band.
                       Yields to a manual override and to away mode.
    override_targets : {room_id: target_f} active manual overrides (items 7/7b).
                       `target_f` is the desired UPPER bound; the band becomes
                       {max_f: target_f, min_f: target_f − W} where W is the
                       room's scheduled band width, so the width is preserved.
                       Beats presence; ignored when away is True. Duration/expiry
                       is handled by the caller.
    """
    if away:
        return away_targets(control_cfg, rooms)
    resolved   = resolve_targets(control_cfg, now)
    default    = control_cfg["targets"]["default"]
    targets    = {room: resolved.get(room, dict(default)) for room in rooms}
    unoccupied = unoccupied or set()
    override_targets = override_targets or {}
    for room in rooms:
        target = override_targets.get(room)
        if target is not None:
            b = targets[room]
            width = b["max_f"] - b["min_f"]
            targets[room] = {"min_f": target - width, "max_f": target}
        elif room in unoccupied:
            targets[room] = dict(WIDE_BAND)
    return targets


def update_override_tracker(raw_targets: dict, tracker: dict,
                            now: datetime, duration_min: float):
    """
    Drive the manual-override lifecycle (items 7/7b).

    raw_targets  : {room_id: target_f} for rooms with a *detected* user edit this
                   tick (the absolute upper-bound target the user set). Rooms with
                   no active edit are simply absent from the dict.
    tracker      : mutable {room_id: (target_f, started_at)} carried across ticks.
    now          : current datetime (tz-aware, matching started_at).
    duration_min : minutes a target stays active before it expires.

    Returns (effective, events):
      effective : {room_id: target_f} overrides still within their window — pass
                  to resolve_targets_for_rooms(override_targets=...).
      events    : list of (room_id, kind, target_f) for logging, kind ∈
                  {"activated","changed","expired","cancelled"}. The caller
                  reverts the card to the schedule for "expired"/"cancelled"
                  rooms (live mode only).

    The tracker keys on the absolute target (not a derived shift) so a schedule
    transition mid-window does not restart the timer. A changed target restarts
    it. "cancelled" fires when a tracked room is no longer being edited (its card
    was returned to the scheduled value) before expiry.
    """
    duration  = timedelta(minutes=duration_min)
    effective = {}
    events    = []
    seen      = set()

    for room, target in raw_targets.items():
        if target is None:
            continue
        seen.add(room)
        prev = tracker.get(room)
        if prev is None:
            tracker[room] = (target, now)
            events.append((room, "activated", target))
        elif prev[0] != target:
            tracker[room] = (target, now)
            events.append((room, "changed", target))

        if now - tracker[room][1] >= duration:
            events.append((room, "expired", target))
            del tracker[room]
        else:
            effective[room] = target

    # A tracked override no longer being edited (card returned to schedule) was
    # cancelled.
    for room in list(tracker):
        if room not in seen:
            events.append((room, "cancelled", tracker[room][0]))
            del tracker[room]

    return effective, events
