"""Tests for the entry-point helpers in scheduler.py and shadow_run.py."""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from thermal_control import scheduler as sc
from thermal_control import shadow_run as sr


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


def test_shadow_fill_missing_matches_scheduler():
    """shadow_run.fill_missing carries the same contract as scheduler's."""
    now = datetime.now(timezone.utc)
    filled, missing = sr.fill_missing({"a": 75.0}, {}, ["a", "b"], 74.0)
    assert filled == {"a": 75.0, "b": 74.0}
    assert missing == {"b"}


# ── load_configs / _config_mtimes ───────────────────────────────────────────
def test_load_configs_returns_house_and_control():
    house, control = sc.load_configs()
    assert "ac_units" in house and "rooms" in house
    assert "mpc" in control and "targets" in control


def test_config_mtimes_returns_two_floats():
    mtimes = sc._config_mtimes()
    assert len(mtimes) == 2
    assert all(isinstance(m, float) for m in mtimes)


# ── _maybe_reload (NEXT_STEPS item 3: config hot-reload) ────────────────────
def test_maybe_reload_noop_when_unchanged():
    assert sc._maybe_reload(sc._config_mtimes()) is None


def test_maybe_reload_rebuilds_on_change():
    result = sc._maybe_reload((0.0, 0.0))                  # stale mtimes → reload
    assert result is not None
    house, control, sim, mpc, mtimes = result
    assert sim.rooms and mpc.ac_units
    assert mtimes == sc._config_mtimes()


def test_maybe_reload_keeps_last_good_on_invalid_yaml(monkeypatch):
    def boom():
        raise ValueError("mid-edit yaml")
    monkeypatch.setattr(sc, "load_configs", boom)
    assert sc._maybe_reload((0.0, 0.0)) is None            # failure swallowed → last-good kept


# ── config integrity check (reject bad edits, log to errors.log) ────────────
def test_validate_config_structure_accepts_real_config():
    house, control = sc.load_configs()
    sc._validate_config_structure(house, control)          # must not raise


@pytest.mark.parametrize("mangle, needle", [
    (lambda h, c: h.pop("rooms"),                  "rooms"),
    (lambda h, c: h.pop("ac_units"),               "ac_units"),
    (lambda h, c: h["location"].pop("timezone"),   "location.timezone"),
    (lambda h, c: h.__setitem__("rooms", []),      "empty"),
    (lambda h, c: c.pop("mpc"),                    "mpc"),
    (lambda h, c: c["mpc"].pop("setpoint_on_f"),   "mpc.setpoint_on_f"),
])
def test_validate_config_structure_rejects_missing_keys(mangle, needle):
    house, control = sc.load_configs()
    mangle(house, control)
    with pytest.raises(ValueError, match=needle):
        sc._validate_config_structure(house, control)


def test_validate_config_structure_rejects_non_mapping():
    with pytest.raises(ValueError, match="house.yaml"):
        sc._validate_config_structure(["not", "a", "dict"], {"mpc": {}})


def test_maybe_reload_logs_invalid_edit_to_errors_log_once(monkeypatch):
    """A rejected edit is recorded in errors.log exactly once per broken save."""
    monkeypatch.setattr(sc, "_last_reload_error_mtimes", None)
    monkeypatch.setattr(sc, "_config_mtimes", lambda: (1.0, 2.0))

    real_load = sc.load_configs

    def bad_config():
        house, control = real_load()
        house.pop("rooms")                                 # syntactically fine, structurally broken
        return house, control
    monkeypatch.setattr(sc, "load_configs", bad_config)

    logged = []
    monkeypatch.setattr(sc.config_error_logger, "error", lambda m: logged.append(m))

    assert sc._maybe_reload((0.0, 0.0)) is None            # rejected → last-good kept
    assert sc._maybe_reload((0.0, 0.0)) is None            # same broken mtimes again
    assert len(logged) == 1                                # de-duped: logged only once
    assert "rooms" in logged[0]


# ── _write_safe_setpoints ───────────────────────────────────────────────────
def test_write_safe_setpoints(monkeypatch, house_config):
    captured = {}
    monkeypatch.setattr(sc.ha, "apply_setpoints",
                        lambda sp, house: captured.update(sp))
    sc._write_safe_setpoints(house_config)
    assert set(captured) == {ac["id"] for ac in house_config["ac_units"]}
    assert set(captured.values()) == {sc.SAFE_SETPOINT_F}


# ── _append_decision_log ────────────────────────────────────────────────────
def test_append_decision_log_writes_header_then_row(monkeypatch, tmp_path):
    log = tmp_path / "mpc_decision_log.csv"
    monkeypatch.setattr(sc, "DECISION_LOG", log)
    sc._append_decision_log({"a": 1, "b": 2})
    sc._append_decision_log({"a": 3, "b": 4})
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


# ── thermostat card sync/detection (NEXT_STEPS item 7b) ─────────────────────
def test_plan_card_detection_seeds_then_detects_edit():
    display = {"kitchen": 77, "family_room": 76}

    # first sight: nothing synced yet → both cards seeded to the schedule, no edit
    detected, writes = sc._plan_card_detection(
        raw_targets={}, display_target=display, card_synced={}, away=False
    )
    assert detected == {} and writes == {"kitchen": 77, "family_room": 76}

    # synced; user dragged kitchen to 80, family_room untouched (still shows 76)
    synced = {"kitchen": 77, "family_room": 76}
    detected, writes = sc._plan_card_detection(
        raw_targets={"kitchen": 80, "family_room": 76},
        display_target=display, card_synced=synced, away=False,
    )
    assert detected == {"kitchen": 80} and writes == {}


def test_plan_card_detection_away_pins_card_ignores_edits():
    # away → card pinned to the away target; a user edit is overwritten, not honored
    detected, writes = sc._plan_card_detection(
        raw_targets={"kitchen": 80}, display_target={"kitchen": 78},
        card_synced={"kitchen": 78}, away=True,
    )
    assert detected == {} and writes == {"kitchen": 78}


def test_plan_card_revert_resyncs_and_reverts():
    display = {"kitchen": 76, "family_room": 77}
    synced  = {"kitchen": 77, "family_room": 77}     # kitchen schedule moved 77→76

    # kitchen under an active override → left alone; family_room resynced to 77;
    # nothing to do for a card already on its schedule value
    writes = sc._plan_card_revert(
        raw_targets={"kitchen": 80, "family_room": 77},
        display_target=display, card_synced=synced, effective={"kitchen": 80},
    )
    # kitchen is in effective → skipped despite the schedule move; family_room
    # already on 77 → no write
    assert writes == {}

    # override expired: card still shows the old user value 80, not in effective
    writes = sc._plan_card_revert(
        raw_targets={"kitchen": 80}, display_target={"kitchen": 77},
        card_synced={"kitchen": 77}, effective={},
    )
    assert writes == {"kitchen": 77}                 # reverted to schedule


# ── shadow_run.append_row ───────────────────────────────────────────────────
def test_shadow_append_row(monkeypatch, tmp_path):
    log = tmp_path / "shadow.csv"
    monkeypatch.setattr(sr, "SHADOW_LOG", log)
    sr.append_row({"x": 10, "y": 20})
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"x": "10", "y": "20"}]
