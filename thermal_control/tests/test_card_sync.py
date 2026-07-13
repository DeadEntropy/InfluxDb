"""Tests for card_sync.py — HA thermostat card sync and responsive polling."""

from thermal_control import card_sync as cs


# ── input_signature ──────────────────────────────────────────────────────────
def test_input_signature_reads_targets_and_away(monkeypatch):
    monkeypatch.setattr(cs.ha, "get_room_targets", lambda house: {"kitchen": 77, "family_room": 76})
    monkeypatch.setattr(cs.ha, "get_away_mode", lambda control: False)
    sig = cs.input_signature(house={}, control={})
    assert sig == ((("family_room", 76), ("kitchen", 77)), False)


def test_input_signature_fails_safe_on_ha_error(monkeypatch):
    def boom(house):
        raise RuntimeError("HA unreachable")
    monkeypatch.setattr(cs.ha, "get_room_targets", boom)
    monkeypatch.setattr(cs.ha, "get_away_mode", lambda control: True)
    sig = cs.input_signature(house={}, control={})
    assert sig == ((), True)                                # empty targets, not a raised exception


# ── responsive_sleep ─────────────────────────────────────────────────────────
def test_responsive_sleep_wakes_early_on_change(monkeypatch):
    calls = {"n": 0}

    def changing_signature(house, control):
        calls["n"] += 1
        return calls["n"]                                   # a new value every call → always "changed"

    monkeypatch.setattr(cs, "input_signature", changing_signature)
    woke_early = cs.responsive_sleep(sleep_s=1.0, poll_s=0.02,
                                     house={}, control={}, baseline=0)
    assert woke_early is True


def test_responsive_sleep_times_out(monkeypatch):
    monkeypatch.setattr(cs, "input_signature", lambda house, control: "steady")
    woke_early = cs.responsive_sleep(sleep_s=0.05, poll_s=0.02,
                                     house={}, control={}, baseline="steady")
    assert woke_early is False


# ── plan_card_detection ──────────────────────────────────────────────────────
def test_plan_card_detection_seeds_then_detects_edit():
    display = {"kitchen": 77, "family_room": 76}

    # first sight: nothing synced yet → both cards seeded to the schedule, no edit
    detected, writes = cs.plan_card_detection(
        raw_targets={}, display_target=display, card_synced={}, away=False
    )
    assert detected == {} and writes == {"kitchen": 77, "family_room": 76}

    # synced; user dragged kitchen to 80, family_room untouched (still shows 76)
    synced = {"kitchen": 77, "family_room": 76}
    detected, writes = cs.plan_card_detection(
        raw_targets={"kitchen": 80, "family_room": 76},
        display_target=display, card_synced=synced, away=False,
    )
    assert detected == {"kitchen": 80} and writes == {}


def test_plan_card_detection_away_pins_card_ignores_edits():
    # away → card pinned to the away target; a user edit is overwritten, not honored
    detected, writes = cs.plan_card_detection(
        raw_targets={"kitchen": 80}, display_target={"kitchen": 78},
        card_synced={"kitchen": 78}, away=True,
    )
    assert detected == {} and writes == {"kitchen": 78}


def test_plan_card_revert_resyncs_and_reverts():
    display = {"kitchen": 76, "family_room": 77}
    synced  = {"kitchen": 77, "family_room": 77}     # kitchen schedule moved 77→76

    # kitchen under an active override → left alone; family_room resynced to 77;
    # nothing to do for a card already on its schedule value
    writes = cs.plan_card_revert(
        raw_targets={"kitchen": 80, "family_room": 77},
        display_target=display, card_synced=synced, effective={"kitchen": 80},
    )
    # kitchen is in effective → skipped despite the schedule move; family_room
    # already on 77 → no write
    assert writes == {}

    # override expired: card still shows the old user value 80, not in effective
    writes = cs.plan_card_revert(
        raw_targets={"kitchen": 80}, display_target={"kitchen": 77},
        card_synced={"kitchen": 77}, effective={},
    )
    assert writes == {"kitchen": 77}                 # reverted to schedule
