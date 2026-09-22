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
    targets = sch.resolve_targets(cfg, MONDAY, occupied_rooms=set())  # Monday = weekday
    assert targets["nicolas_office"] == {"min_f": 74, "max_f": 76}


def test_resolve_targets_static_override_beats_schedule():
    cfg = {"targets": {
        "default":  {"min_f": 75, "max_f": 77},
        "kitchen":  {"min_f": 70, "max_f": 72},          # static per-room override
        "schedule": [{"name": "day", "time": "06:00",
                      "rooms": {"kitchen": {"min_f": 80, "max_f": 82}}}],
    }}
    targets = sch.resolve_targets(cfg, MONDAY, occupied_rooms=set())
    assert targets["kitchen"] == {"min_f": 70, "max_f": 72}   # static wins over schedule


# ── resolve_active_entry_name ───────────────────────────────────────────────
def test_resolve_active_entry_name(control_config):
    assert sch.resolve_active_entry_name(control_config, MONDAY) == "daytime"
    assert sch.resolve_active_entry_name(control_config, datetime(2026, 6, 15, 23, 0)) == "sleeping"


# ── away_targets (NEXT_STEPS item 8) ────────────────────────────────────────
def test_away_targets_uses_away_band(control_config):
    rooms = ["kitchen", "nicolas_office"]
    away = sch.away_targets(control_config, rooms)
    assert away == {r: {"min_f": 65, "max_f": 80} for r in rooms}


def test_away_targets_fallback_when_block_absent():
    away = sch.away_targets({"targets": {}}, ["kitchen"])
    assert away == {"kitchen": {"min_f": 76, "max_f": 80}}


# ── resolve_targets_for_rooms (priority chain) ──────────────────────────────
def test_resolve_for_rooms_priority_chain(control_config):
    rooms = ["nicolas_office"]

    # away beats everything
    assert sch.resolve_targets_for_rooms(control_config, rooms, MONDAY,
                                         away=True)["nicolas_office"] == {"min_f": 65, "max_f": 80}

    # manual override sets the upper bound, lower bound follows at the scheduled
    # width, and it beats presence (unoccupied). Derived from the live config so
    # it tracks retuning: at MONDAY 10:00 nicolas_office is unlisted in the
    # active entry and falls through to targets.default.
    width = control_config["targets"]["default"]["max_f"] - \
            control_config["targets"]["default"]["min_f"]
    shifted = sch.resolve_targets_for_rooms(
        control_config, rooms, MONDAY,
        override_targets={"nicolas_office": 78}, unoccupied={"nicolas_office"},
    )["nicolas_office"]
    assert shifted == {"min_f": 78 - width, "max_f": 78}

    # unoccupied with no override → wide "don't care" band (NEXT_STEPS item 9).
    # Regression guard on the *global* presence rule: it must survive the
    # per-entry conditional bands of item 11 as the fallback for every
    # room/hour that doesn't state one (here: nicolas_office at 10:00 Monday).
    empty = sch.resolve_targets_for_rooms(
        control_config, rooms, MONDAY, unoccupied={"nicolas_office"},
    )["nicolas_office"]
    assert empty == sch.WIDE_BAND


# ── Presence-conditional bands (NEXT_STEPS item 11) ─────────────────────────
OCCUPIED   = {"min_f": 65, "max_f": 76}
UNOCCUPIED = {"min_f": 65, "max_f": 78}


def _cfg(room_value):
    """Minimal config whose only schedule entry carries `room_value`."""
    return {"targets": {
        "default":  {"min_f": 75, "max_f": 78},
        "schedule": [{"name": "evening", "time": "06:00",
                      "rooms": {"nicolas_office": room_value}}],
    }}


@pytest.mark.parametrize("value, shape", [
    ({"min_f": 65, "max_f": 78},                      sch.FLAT),
    ({"occupied": OCCUPIED},                          sch.CONDITIONAL),
    ({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED}, sch.CONDITIONAL),
    ({"min_f": 65, "max_f": 78, "occupied": OCCUPIED}, sch.MIXED),
    ({"occuppied": OCCUPIED},                         sch.INVALID),   # typo
    ({"min_f": 65},                                   sch.INVALID),   # partial
    ({},                                              sch.INVALID),
    ("nonsense",                                      sch.INVALID),
    ({"occupied": {"max_f": 76}},                     sch.INVALID),   # partial branch
])
def test_classify_band_shapes(value, shape):
    assert sch.classify_band(value)[0] == shape


@pytest.mark.parametrize("value", [
    {"occuppied": OCCUPIED}, {"min_f": 65}, {}, "nonsense",
])
def test_classify_band_reports_a_problem_for_every_invalid_shape(value):
    # Silence is the failure mode that matters: an unusable band must always
    # come with something the operator can read.
    assert sch.classify_band(value)[1]


def test_pick_band_selects_branch_by_presence():
    value = {"occupied": OCCUPIED, "unoccupied": UNOCCUPIED}
    assert sch._pick_band(value, occupied=True) == OCCUPIED
    assert sch._pick_band(value, occupied=False) == UNOCCUPIED


def test_pick_band_missing_unoccupied_means_wide_band():
    # The shorthand that keeps control.yaml small: omitting `unoccupied` gives
    # exactly what the global item-9 rule would have applied anyway.
    value = {"occupied": OCCUPIED}
    assert sch._pick_band(value, occupied=True) == OCCUPIED
    assert sch._pick_band(value, occupied=False) == sch.WIDE_BAND


def test_pick_band_missing_occupied_falls_through():
    assert sch._pick_band({"unoccupied": UNOCCUPIED}, occupied=True) is None


def test_pick_band_mixed_falls_back_to_the_flat_band():
    # The documented degradation: an inconsistent entry keeps the unconditional
    # reading and drops the conditional keys entirely.
    value = {"min_f": 65, "max_f": 78, "occupied": OCCUPIED}
    assert sch._pick_band(value, occupied=True)  == {"min_f": 65, "max_f": 78}
    assert sch._pick_band(value, occupied=False) == {"min_f": 65, "max_f": 78}


@pytest.mark.parametrize("value", [{"occuppied": OCCUPIED}, {"min_f": 65}, {}])
def test_pick_band_invalid_falls_through(value):
    assert sch._pick_band(value, occupied=True) is None


def test_conditional_band_beats_the_global_unoccupied_rule():
    # Without the conditional_band_rooms() guard the blanket
    # "unoccupied → WIDE_BAND" rule would overwrite the entry's own answer.
    cfg = _cfg({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED})
    got = sch.resolve_targets_for_rooms(
        cfg, ["nicolas_office"], MONDAY, unoccupied={"nicolas_office"},
    )["nicolas_office"]
    assert got == UNOCCUPIED


def test_conditional_band_applies_occupied_branch_when_present():
    cfg = _cfg({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED})
    got = sch.resolve_targets_for_rooms(
        cfg, ["nicolas_office"], MONDAY, unoccupied=set(),
    )["nicolas_office"]
    assert got == OCCUPIED


def test_away_and_override_still_beat_a_conditional_band():
    cfg = _cfg({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED})
    cfg["targets"]["away"] = {"default": {"min_f": 65, "max_f": 80}}
    rooms = ["nicolas_office"]

    assert sch.resolve_targets_for_rooms(
        cfg, rooms, MONDAY, away=True, unoccupied=set(),
    )["nicolas_office"] == {"min_f": 65, "max_f": 80}

    # Override width follows the *occupied* band it resolved against (76−65=11).
    assert sch.resolve_targets_for_rooms(
        cfg, rooms, MONDAY, override_targets={"nicolas_office": 74},
    )["nicolas_office"] == {"min_f": 63, "max_f": 74}


def test_static_override_beats_a_conditional_band():
    cfg = _cfg({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED})
    cfg["targets"]["nicolas_office"] = {"min_f": 70, "max_f": 72}
    got = sch.resolve_targets_for_rooms(cfg, ["nicolas_office"], MONDAY,
                                        unoccupied=set())["nicolas_office"]
    assert got == {"min_f": 70, "max_f": 72}
    # …and because static won, the room is not "conditional", so the global
    # unoccupied rule still applies to it as it always did.
    assert sch.conditional_band_rooms(cfg, MONDAY) == set()


def test_scheduled_bands_shows_the_unoccupied_branch():
    # The display view (thermostat cards, dashboard grid): no live presence, and
    # crucially no blanket wide-band rule.
    cfg = _cfg({"occupied": OCCUPIED, "unoccupied": UNOCCUPIED})
    assert sch.scheduled_bands(cfg, ["nicolas_office"], MONDAY) == {
        "nicolas_office": UNOCCUPIED
    }


def test_scheduled_bands_leaves_other_rooms_on_their_schedule():
    cfg = _cfg({"occupied": OCCUPIED})
    assert sch.scheduled_bands(cfg, ["kitchen"], MONDAY) == {
        "kitchen": {"min_f": 75, "max_f": 78}        # targets.default, untouched
    }


def test_live_config_nicolas_office_evening_and_night(control_config):
    """The real control.yaml: the rule this feature was built for."""
    rooms = ["nicolas_office"]

    def band(hour, occupied):
        when = datetime(2026, 6, 15, hour, 30)       # Monday
        return sch.resolve_targets_for_rooms(
            control_config, rooms, when,
            unoccupied=set() if occupied else set(rooms),
        )["nicolas_office"]

    # 20:45 early evening — empty keeps today's 78 cap, occupied tightens to 76
    assert band(21, occupied=False)["max_f"] == 78
    assert band(21, occupied=True)["max_f"]  == 76
    # 22:00 sleeping — empty is still the wide don't-care band, as before
    assert band(23, occupied=False) == sch.WIDE_BAND
    assert band(23, occupied=True)["max_f"] == 76
    # 00:30 night — same
    assert band(1, occupied=False) == sch.WIDE_BAND
    assert band(1, occupied=True)["max_f"] == 76


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
