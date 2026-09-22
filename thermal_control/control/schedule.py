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
                                 (caller passes unoccupied=). Skipped for rooms
                                 whose active schedule entry states a conditional
                                 band (item 11) — that band is more specific.
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

Presence-conditional bands (item 11)
────────────────────────────────────
A room's value inside a schedule entry's `rooms` block is either a flat band
(the original form) or a presence dict naming one band per presence state:

    rooms:
      master_bedroom: {min_f: 72, max_f: 74}              # flat, unconditional
      nicolas_office: {occupied: {min_f: 65, max_f: 76}}  # conditional

Only schedule entries accept the conditional form — targets.default and the
static per-room block stay flat-only, because both beat the schedule at all
times and a conditional there would be an always-on presence rule.

A missing `unoccupied` key means WIDE_BAND, which is exactly what priority 0b
would have done anyway, so the common "keep it cool if someone's here" rule
costs one key. A missing `occupied` key falls through to static/default as if
the room were not listed in the entry at all.

Malformed values never reject the config: _pick_band() falls back to the
unconditional reading (the flat band if one was given, else fall-through) and
control/config_check.collect_band_warnings() reports the problem out-of-band,
to the scheduler's config_warnings.log and the dashboard's warning banner.
"""

from datetime import datetime, time as dtime, timedelta

# Wide "don't care" band — zero discomfort penalty at any realistic indoor
# temperature, so the MPC ignores the room. Used for unoccupied rooms (item 9).
WIDE_BAND = {"min_f": 65, "max_f": 85}


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ── Presence-conditional bands (item 11) ────────────────────────────────────
# Shape tags returned by classify_band(). Kept here rather than in config_check
# so the runtime picker and the validation walk classify from one definition and
# cannot drift apart; config_check imports classify_band() for its own messages.
FLAT        = "flat"          # {min_f, max_f} — the original, unconditional form
CONDITIONAL = "conditional"   # {occupied: {...}} and/or {unoccupied: {...}}
MIXED       = "mixed"         # both at once — inconsistent; the flat band wins
INVALID     = "invalid"       # unusable (typo'd key, partial band, not a mapping)

_COND_KEYS = ("occupied", "unoccupied")


def _flat_band(value):
    """Return {min_f, max_f} if `value` is a complete flat band, else None."""
    if not isinstance(value, dict):
        return None
    if "min_f" not in value or "max_f" not in value:
        return None
    return {"min_f": value["min_f"], "max_f": value["max_f"]}


def classify_band(value):
    """
    Classify a room's value inside a schedule entry's `rooms` block.

    Returns (shape, problems) where shape is one of FLAT/CONDITIONAL/MIXED/
    INVALID and problems is a list of human-readable fragments describing what
    is wrong (empty when the value is clean). Callers prefix each fragment with
    their own "<entry> / <room>: " context.

    Shape is decided by which keys are present, so a typo like `occuppied:`
    reads as INVALID rather than being silently ignored.
    """
    problems = []
    if not isinstance(value, dict):
        return INVALID, [f"expected a band mapping, got {type(value).__name__}"]

    has_flat_key = "min_f" in value or "max_f" in value
    has_cond_key = any(k in value for k in _COND_KEYS)
    flat         = _flat_band(value)

    if has_flat_key and flat is None:
        missing = "max_f" if "min_f" in value else "min_f"
        problems.append(f"incomplete band, missing '{missing}'")

    if has_cond_key:
        for key in _COND_KEYS:
            if key in value and _flat_band(value[key]) is None:
                problems.append(f"'{key}' is not a complete {{min_f, max_f}} band")

    if has_flat_key and has_cond_key:
        problems.insert(0, "both a flat band and occupied/unoccupied given")
        # A usable flat band is the unconditional fallback; without one there is
        # nothing to fall back to, so the whole value is unusable.
        return (MIXED, problems) if flat else (INVALID, problems)
    if has_cond_key:
        # Every stated branch malformed → nothing usable left.
        usable = [k for k in _COND_KEYS if _flat_band(value.get(k)) is not None]
        if not usable and "unoccupied" not in value:
            return INVALID, problems
        return CONDITIONAL, problems
    if has_flat_key:
        return (FLAT, problems) if flat else (INVALID, problems)

    known = ", ".join(_COND_KEYS)
    return INVALID, problems + [
        f"no 'min_f'/'max_f' and no {known} key (got: {', '.join(map(str, value)) or 'empty'})"
    ]


def _pick_band(value, occupied: bool):
    """
    Resolve one room's schedule value to a band, or None to fall through to the
    normal priority chain (static → default, then the global unoccupied rule).

    Never raises and never logs: this runs on every tick, and a malformed entry
    degrades to the unconditional reading rather than taking the control loop
    down. collect_band_warnings() reports the same problems once per config edit.
    """
    shape, _ = classify_band(value)

    if shape in (FLAT, MIXED):
        # MIXED falls back to the unconditional band, dropping the conditional
        # keys — the documented behaviour for an inconsistent entry.
        return _flat_band(value)
    if shape == CONDITIONAL:
        chosen = value.get("occupied") if occupied else value.get("unoccupied")
        band   = _flat_band(chosen)
        if band is not None:
            return band
        # An absent/malformed 'unoccupied' means the wide "don't care" band —
        # identical to what priority 0b would have applied anyway.
        if not occupied:
            return dict(WIDE_BAND)
        return None
    return None                                   # INVALID → fall through


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


def resolve_targets(control_cfg: dict, now: datetime, occupied_rooms) -> dict:
    """
    Return {room_id: {"min_f": float, "max_f": float}} for all modelled rooms.

    control_cfg    : the parsed control.yaml dict
    now            : current local datetime (naive or aware — only .time() is used)
    occupied_rooms : set of room_ids currently occupied, used to pick a branch of
                     a presence-conditional band (item 11). Required, not
                     defaulted: the scheduler wants fail-safe-to-occupied while
                     the dashboard grid wants the unoccupied view, and a silent
                     default would quietly hand one of them the wrong answer.
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
    # Static and default stay flat-only by design (both beat the schedule at all
    # times, so a conditional there would be an always-on presence rule).
    def resolve_room(room_id):
        if room_id in static:
            return dict(static[room_id])
        if room_id in active_rooms:
            band = _pick_band(active_rooms[room_id], room_id in occupied_rooms)
            if band is not None:
                return band
        return dict(default)

    return {room_id: resolve_room(room_id) for room_id in all_rooms}


def conditional_band_rooms(control_cfg: dict, now: datetime) -> set:
    """
    Rooms whose *active* schedule entry states a presence-conditional band that
    _pick_band() can actually use (item 11).

    resolve_targets_for_rooms() uses this to skip the blanket
    "unoccupied → WIDE_BAND" rule for those rooms: an entry that spells out what
    an empty room should get is more specific than the global default, and
    without this guard the global rule would overwrite it.

    A room whose static override wins is excluded — its band came from static,
    not from the conditional, so the global rule still applies to it as before.
    """
    targets_cfg = control_cfg["targets"]
    schedule    = targets_cfg.get("schedule", [])
    entry       = _active_entry(schedule, now) if schedule else None
    rooms       = (entry.get("rooms") if entry else None) or {}

    RESERVED = {"default", "schedule", "away"}
    static   = {k for k in targets_cfg if k not in RESERVED}

    return {room for room, value in rooms.items()
            if room not in static and classify_band(value)[0] == CONDITIONAL}


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


def scheduled_bands(control_cfg: dict, rooms: list, now: datetime,
                    away: bool = False) -> dict:
    """
    The schedule *as written* for `rooms` — no presence widening, no manual
    overrides. This is the display view: what the thermostat cards show and what
    the dashboard's 24h grid draws.

    Differs from resolve_targets_for_rooms(away=away) in that a
    presence-conditional band resolves to its `unoccupied` branch, and from
    resolve_targets_for_rooms(unoccupied=all) in that the blanket
    "unoccupied → WIDE_BAND" rule is *not* applied — that rule describes a live
    sensor reading, not the schedule.
    """
    if away:
        return away_targets(control_cfg, rooms)
    resolved = resolve_targets(control_cfg, now, occupied_rooms=set())
    default  = control_cfg["targets"]["default"]
    return {room: resolved.get(room, dict(default)) for room in rooms}


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
                       Yields to a manual override and to away mode. Also selects
                       which branch of a presence-conditional band applies
                       (item 11) — every room in `rooms` not listed here counts as
                       occupied, which matches get_presence()'s fail-safe (a room
                       with no presence sensor, or an unreadable one, is never
                       reported unoccupied).
    override_targets : {room_id: target_f} active manual overrides (items 7/7b).
                       `target_f` is the desired UPPER bound; the band becomes
                       {max_f: target_f, min_f: target_f − W} where W is the
                       room's scheduled band width, so the width is preserved.
                       Beats presence; ignored when away is True. Duration/expiry
                       is handled by the caller.
    """
    if away:
        return away_targets(control_cfg, rooms)
    unoccupied = unoccupied or set()
    resolved   = resolve_targets(control_cfg, now, set(rooms) - unoccupied)
    default    = control_cfg["targets"]["default"]
    targets    = {room: resolved.get(room, dict(default)) for room in rooms}
    conditional      = conditional_band_rooms(control_cfg, now)
    override_targets = override_targets or {}
    for room in rooms:
        target = override_targets.get(room)
        if target is not None:
            b = targets[room]
            width = b["max_f"] - b["min_f"]
            targets[room] = {"min_f": target - width, "max_f": target}
        elif room in unoccupied and room not in conditional:
            # The blanket rule only applies where the schedule has not already
            # said what this room gets while empty.
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
