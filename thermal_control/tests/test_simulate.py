"""Tests for model/simulate.py — HouseSimulator (the two-layer forward model)."""

import numpy as np


# ── _derive_ac_on ───────────────────────────────────────────────────────────
def test_derive_ac_on_switching_logic(simulator, ac_ids):
    """Layer 2: AC is ON iff its *sensor room* temp exceeds the setpoint.
    No hysteresis. NaN sensor → OFF (fail-safe)."""
    warm = {r: 80.0 for r in simulator.rooms}      # every sensor room is warm
    on   = simulator._derive_ac_on(warm, {ac: 65 for ac in ac_ids})
    assert on == {ac: 1 for ac in ac_ids}

    off = simulator._derive_ac_on(warm, {ac: 84 for ac in ac_ids})
    assert off == {ac: 0 for ac in ac_ids}

    # Missing sensor room → NaN → OFF, never crashes.
    nan_on = simulator._derive_ac_on({}, {ac: 65 for ac in ac_ids})
    assert nan_on == {ac: 0 for ac in ac_ids}


def test_derive_ac_on_uses_sensor_room_not_zone(simulator):
    """KEY FEATURE (the project's raison d'être): bedroom_ac switches on
    dining_room's temperature, not on the bedrooms it actually cools."""
    sensor_room = simulator.sensor_rooms["bedroom_ac"]
    assert sensor_room == "dining_room"

    state = {r: 70.0 for r in simulator.rooms}   # everything cool…
    state["dining_room"] = 80.0                  # …except the sensor room
    on = simulator._derive_ac_on(state, {"bedroom_ac": 65, "living_ac": 84,
                                         "extension_ac": 84})
    assert on["bedroom_ac"] == 1                 # driven by dining_room, not the bedrooms


# ── step ────────────────────────────────────────────────────────────────────
def test_step_returns_state_for_all_rooms(simulator, ac_ids):
    state = {r: 75.0 for r in simulator.rooms}
    next_state, ac_on = simulator.step(state, {ac: 65 for ac in ac_ids}, 90.0)
    assert set(next_state) == set(simulator.rooms)
    assert all(np.isfinite(v) for v in next_state.values())
    assert set(ac_on) == set(ac_ids)


# ── rollout ─────────────────────────────────────────────────────────────────
def test_rollout_length_and_initial_state(simulator, ac_ids):
    state = {r: 75.0 for r in simulator.rooms}
    n = 6
    sched = [{ac: 65 for ac in ac_ids}] * n
    outdoor = [90.0] * n
    states = simulator.rollout(state, sched, outdoor)
    assert len(states) == n + 1            # includes the initial state
    assert states[0] == state              # initial preserved unmutated
