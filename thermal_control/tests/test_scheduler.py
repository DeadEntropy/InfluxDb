"""Tests for the entry-point helpers in scheduler.py."""

import csv
from datetime import datetime, timedelta, timezone

from thermal_control import scheduler as sc


# ── fill_missing (KEY FEATURE: stale-sensor caching + cost exclusion) ───────
def test_fill_missing_fresh_cached_stale_and_never_seen():
    now = datetime.now(timezone.utc)
    rooms = ["a", "b", "c", "d"]
    cache = {
        "b": (70.0, now),                                  # fresh cache
        "c": (71.0, now - timedelta(minutes=60)),          # stale (> 30 min)
    }
    current = {"a": 75.0}                                  # only 'a' read this tick

    filled, missing = sc.fill_missing(current, cache, rooms, fallback_f=74.0)

    assert filled == {"a": 75.0, "b": 70.0, "c": 71.0, "d": 74.0}
    assert missing == {"c", "d"}                           # stale + never-seen excluded
    assert "a" not in missing and "b" not in missing       # fresh data steers cost
    assert cache["a"][0] == 75.0                           # fresh reading cached


# ── resolve_kill_switch (item 10) ────────────────────────────────────────────
def test_resolve_kill_switch_filters_setpoints_and_detects_transitions():
    ac_ids    = ["bedroom_ac", "living_ac", "extension_ac"]
    setpoints = {"bedroom_ac": 65, "living_ac": 84, "extension_ac": 65}
    killed_state = {}

    # Tick 1: extension_ac freshly killed → filtered out + one transition logged.
    write_sp, transitions = sc.resolve_kill_switch(
        {"extension_ac"}, killed_state, ac_ids, setpoints
    )
    assert write_sp == {"bedroom_ac": 65, "living_ac": 84}   # extension_ac dropped
    assert transitions == [("extension_ac", True)]
    assert killed_state == {"extension_ac": True}   # untouched ac_ids default, unrecorded

    # Tick 2: still killed, nothing changed → no new transition, still filtered.
    write_sp, transitions = sc.resolve_kill_switch(
        {"extension_ac"}, killed_state, ac_ids, setpoints
    )
    assert write_sp == {"bedroom_ac": 65, "living_ac": 84}
    assert transitions == []

    # Tick 3: released → back in write_setpoints, transition logged as False.
    write_sp, transitions = sc.resolve_kill_switch(set(), killed_state, ac_ids, setpoints)
    assert write_sp == setpoints
    assert transitions == [("extension_ac", False)]


# ── _write_safe_setpoints ───────────────────────────────────────────────────
def test_write_safe_setpoints(monkeypatch, house_config):
    captured = {}
    monkeypatch.setattr(sc.ha, "apply_setpoints",
                        lambda sp, house: captured.update(sp))
    sc._write_safe_setpoints(house_config)
    assert set(captured) == {ac["id"] for ac in house_config["ac_units"]}
    assert set(captured.values()) == {sc.SAFE_SETPOINT_F}


# ── _append_user_event ───────────────────────────────────────────────────────
def test_append_user_event_writes_row(monkeypatch, tmp_path):
    log = tmp_path / "user_inputs.log"
    monkeypatch.setattr(sc, "USER_EVENT_LOG", log)
    sc._append_user_event("2026-07-13T10:00:00", "away_activated")
    sc._append_user_event("2026-07-13T10:05:00", "override_activated",
                          room="kitchen", value="77")
    rows = list(csv.DictReader(log.open()))
    assert rows == [
        {"timestamp": "2026-07-13T10:00:00", "event": "away_activated",
         "room": "", "value": ""},
        {"timestamp": "2026-07-13T10:05:00", "event": "override_activated",
         "room": "kitchen", "value": "77"},
    ]
