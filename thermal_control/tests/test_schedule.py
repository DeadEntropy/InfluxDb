"""Tests for control/schedule.py — comfort-band resolution and override lifecycle."""

from datetime import datetime, time as dtime, timedelta

import pytest

from thermal_control.control import schedule as sch

# 2026-06-15 is a Monday; 2026-06-13 (today) is a Saturday.
MONDAY   = datetime(2026, 6, 15, 10, 0)
SATURDAY = datetime(2026, 6, 13, 10, 0)


# ── _parse_hhmm ─────────────────────────────────────────────────────────────
def test_parse_hhmm():
    assert sch._parse_hhmm("06:30") == dtime(6, 30)
    assert sch._parse_hhmm("22:00") == dtime(22, 0)


# ── _entry_applies (NEXT_STEPS item 6: weekend scheduling) ──────────────────
def test_entry_applies_days_filter():
    assert sch._entry_applies({"time": "06:00"}, MONDAY) is True          # no days = every day
    assert sch._entry_applies({"days": "weekday"}, MONDAY) is True
    assert sch._entry_applies({"days": "weekday"}, SATURDAY) is False
    assert sch._entry_applies({"days": "weekend"}, SATURDAY) is True
    assert sch._entry_applies({"days": "weekend"}, MONDAY) is False


def test_entry_applies_unknown_days_raises():
    with pytest.raises(ValueError):
        sch._entry_applies({"days": "holidays"}, MONDAY)


# ── _active_entry (latest-time-wins + midnight wrap) ────────────────────────
def test_active_entry_latest_time_wins_and_midnight_wrap():
    schedule = [
        {"name": "night", "time": "22:00"},
        {"name": "morning", "time": "06:00"},
        {"name": "afternoon", "time": "15:00"},
    ]
    assert sch._active_entry(schedule, datetime(2026, 6, 15, 10, 0))["name"] == "morning"
    assert sch._active_entry(schedule, datetime(2026, 6, 15, 16, 0))["name"] == "afternoon"
    # 02:00 precedes the earliest entry → wraps to the day's last entry.
    assert sch._active_entry(schedule, datetime(2026, 6, 15, 2, 0))["name"] == "night"


# ── resolve_targets (NEXT_STEPS item 1: per-room scheduled band) ────────────
def test_resolve_targets_honors_scheduled_room_band():
    # A weekday-scoped entry tightens one room; on a weekday resolve_targets must
    # surface that per-room band. Kept self-contained (not the live control_config
    # fixture) so retuning a real band can't break this logic test —
    # cf. test_resolve_targets_static_override_beats_schedule.
    cfg = {"targets": {
        "default":  {"min_f": 75, "max_f": 77},
        "schedule": [{"name": "daytime", "time": "06:00", "days": "weekday",
                      "rooms": {"nicolas_office": {"min_f": 74, "max_f": 76}}}],
    }}
    targets = sch.resolve_targets(cfg, MONDAY)                 # Monday = weekday
    assert targets["nicolas_office"] == {"min_f": 74, "max_f": 76}


def test_resolve_targets_static_override_beats_schedule():
    cfg = {"targets": {
        "default":  {"min_f": 75, "max_f": 77},
        "kitchen":  {"min_f": 70, "max_f": 72},          # static per-room override
        "schedule": [{"name": "day", "time": "06:00",
                      "rooms": {"kitchen": {"min_f": 80, "max_f": 82}}}],
    }}
    targets = sch.resolve_targets(cfg, MONDAY)
    assert targets["kitchen"] == {"min_f": 70, "max_f": 72}   # static wins over schedule


# ── resolve_active_entry_name ───────────────────────────────────────────────
def test_resolve_active_entry_name(control_config):
    assert sch.resolve_active_entry_name(control_config, MONDAY) == "daytime"
    assert sch.resolve_active_entry_name(control_config, datetime(2026, 6, 15, 23, 0)) == "sleeping"


# ── away_targets (NEXT_STEPS item 8) ────────────────────────────────────────
def test_away_targets_uses_away_band(control_config):
    rooms = ["kitchen", "nicolas_office"]
    away = sch.away_targets(control_config, rooms)
    assert away == {r: {"min_f": 76, "max_f": 78} for r in rooms}


def test_away_targets_fallback_when_block_absent():
    away = sch.away_targets({"targets": {}}, ["kitchen"])
    assert away == {"kitchen": {"min_f": 76, "max_f": 80}}


# ── resolve_targets_for_rooms (priority chain) ──────────────────────────────
def test_resolve_for_rooms_priority_chain(control_config):
    rooms = ["nicolas_office"]

    # away beats everything
    assert sch.resolve_targets_for_rooms(control_config, rooms, MONDAY,
                                         away=True)["nicolas_office"] == {"min_f": 76, "max_f": 78}

    # manual override sets the upper bound, lower bound follows at the scheduled
    # width, and it beats presence (unoccupied)
    shifted = sch.resolve_targets_for_rooms(
        control_config, rooms, MONDAY,
        override_targets={"nicolas_office": 78}, unoccupied={"nicolas_office"},
    )["nicolas_office"]
    assert shifted == {"min_f": 76, "max_f": 78}      # 74–76 (width 2) → max 78, min 76

    # unoccupied with no override → wide "don't care" band (NEXT_STEPS item 9)
    empty = sch.resolve_targets_for_rooms(
        control_config, rooms, MONDAY, unoccupied={"nicolas_office"},
    )["nicolas_office"]
    assert empty == sch.WIDE_BAND


# ── update_override_tracker (NEXT_STEPS items 7/7b) ─────────────────────────
def test_override_tracker_activate_change_expire_cancel():
    now = datetime(2026, 6, 15, 12, 0)
    tracker = {}

    # activate (target = the absolute upper bound the user set)
    eff, events = sch.update_override_tracker({"kitchen": 78}, tracker, now, duration_min=60)
    assert eff == {"kitchen": 78}
    assert ("kitchen", "activated", 78) in events

    # changed target restarts the timer
    later = now + timedelta(minutes=30)
    eff, events = sch.update_override_tracker({"kitchen": 79}, tracker, later, 60)
    assert eff == {"kitchen": 79}
    assert ("kitchen", "changed", 79) in events

    # re-passing the SAME target (e.g. unchanged across a schedule transition)
    # does NOT restart the timer
    eff, events = sch.update_override_tracker({"kitchen": 79}, tracker, later + timedelta(minutes=10), 60)
    assert eff == {"kitchen": 79}
    assert events == []

    # expiry: now is >= duration past the (restarted) start
    expired_at = later + timedelta(minutes=60)
    eff, events = sch.update_override_tracker({"kitchen": 79}, tracker, expired_at, 60)
    assert eff == {}
    assert ("kitchen", "expired", 79) in events
    assert "kitchen" not in tracker

    # cancel: a tracked card no longer being edited (returned to schedule) → absent
    tracker = {"kitchen": (78, now)}
    eff, events = sch.update_override_tracker({}, tracker, now, 60)
    assert eff == {}
    assert ("kitchen", "cancelled", 78) in events
