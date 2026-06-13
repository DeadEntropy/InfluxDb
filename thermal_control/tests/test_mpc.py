"""Tests for control/mpc.py — BangBangMPC."""

import numpy as np
import pytest


# ── __init__ / config parsing ───────────────────────────────────────────────
def test_init_parses_config_and_normalizes_power(mpc, control_config):
    assert mpc.horizon == control_config["mpc"]["horizon_steps"]
    assert mpc.sp_on == control_config["mpc"]["setpoint_on_f"]
    assert mpc.sp_off == control_config["mpc"]["setpoint_off_f"]

    # ac_power normalised to the max unit (living_ac = 2400 W → 1.0).
    assert max(mpc.ac_power.values()) == pytest.approx(1.0)
    assert mpc.ac_power["living_ac"] == pytest.approx(1.0)
    assert mpc.ac_power["bedroom_ac"] == pytest.approx(1600 / 2400)


def test_step_weights_discount(mpc):
    """NEXT_STEPS item 4: per-step discount, linear start→end, length = horizon."""
    assert len(mpc.step_weights) == mpc.horizon
    assert mpc.step_weights[0] == pytest.approx(1.0)      # discount_start
    assert mpc.step_weights[-1] == pytest.approx(0.25)    # discount_end
    assert np.all(np.diff(mpc.step_weights) <= 0)         # monotone non-increasing


# ── _cost ───────────────────────────────────────────────────────────────────
def test_cost_quadratic_penalty_and_missing_exclusion(mpc):
    """Quadratic too-hot/too-cold penalty; rooms in missing_rooms contribute
    nothing (KEY FEATURE: fabricated/stale data must not steer the MPC)."""
    mpc.step_weights = np.array([1.0])                    # single-step, weight 1
    mpc._current_targets = {r: {"min_f": 75, "max_f": 77} for r in mpc.rooms}

    hot_room = mpc.rooms[0]
    states = [
        {r: 76.0 for r in mpc.rooms},                     # t0 (ignored by cost)
        {**{r: 76.0 for r in mpc.rooms}, hot_room: 80.0},  # t1: one room +3°F over max
    ]
    all_off = tuple(0 for _ in mpc.ac_units)

    cost = mpc._cost(states, all_off, missing_rooms=set())
    assert cost == pytest.approx((80 - 77) ** 2)          # 9.0, energy term zero

    excluded = mpc._cost(states, all_off, missing_rooms={hot_room})
    assert excluded == pytest.approx(0.0)


def test_cost_energy_term_scales_with_power(mpc):
    """Energy penalty = mean(weight) · energy_weight · normalised power, summed
    over ON units; in-band rooms contribute zero discomfort."""
    mpc._current_targets = {r: {"min_f": 60, "max_f": 90} for r in mpc.rooms}  # always in band
    states = [{r: 75.0 for r in mpc.rooms}, {r: 75.0 for r in mpc.rooms}]

    all_off = tuple(0 for _ in mpc.ac_units)
    assert mpc._cost(states, all_off, set()) == pytest.approx(0.0)

    living_idx = mpc.ac_units.index("living_ac")
    combo = tuple(1 if i == living_idx else 0 for i in range(len(mpc.ac_units)))
    expected = mpc.step_weights.mean() * mpc.energy_w * mpc.ac_power["living_ac"]
    assert mpc._cost(states, combo, set()) == pytest.approx(expected)


# ── solve ───────────────────────────────────────────────────────────────────
def test_solve_returns_valid_setpoints_and_min_cost(mpc):
    state   = {r: 78.0 for r in mpc.rooms}
    outdoor = [90.0] * mpc.horizon
    sp = mpc.solve(state, outdoor)

    assert set(sp) == set(mpc.ac_units)
    assert all(v in (mpc.sp_on, mpc.sp_off) for v in sp.values())
    assert len(mpc._last_results) == 8                    # 2³ combinations enumerated
    # chosen cost is the minimum across all evaluated combos
    assert mpc._best_cost == pytest.approx(min(c for _, _, c, _ in mpc._last_results))


def test_solve_all_hot_runs_cooling(mpc):
    """Every room well above its max band → the all-OFF combo is never chosen;
    the MPC commits to cooling (at least one AC forced ON)."""
    state   = {r: 85.0 for r in mpc.rooms}
    outdoor = [95.0] * mpc.horizon
    sp = mpc.solve(state, outdoor)
    assert sp != {ac: mpc.sp_off for ac in mpc.ac_units}   # not all-off
    assert any(v == mpc.sp_on for v in sp.values())


def test_solve_excludes_missing_room_from_decision(mpc):
    """A single very-hot room flips the decision; excluding it via missing_rooms
    must not (fabricated data is inert)."""
    outdoor = [90.0] * mpc.horizon
    hot_room = "nicolas_office"
    state = {r: 74.0 for r in mpc.rooms}
    state[hot_room] = 90.0

    steered  = mpc.solve(dict(state), outdoor, missing_rooms=set())
    ignored  = mpc.solve(dict(state), outdoor, missing_rooms={hot_room})
    # With the hot room counted, extension_ac (covers nicolas_office) is pushed ON;
    # excluding it should relax that pressure → different (cheaper-energy) decision.
    assert steered != ignored


# ── explain ─────────────────────────────────────────────────────────────────
def test_explain_before_and_after_solve(mpc):
    assert mpc.explain() == "Call solve() first."
    mpc.solve({r: 78.0 for r in mpc.rooms}, [90.0] * mpc.horizon)
    text = mpc.explain()
    assert "chosen" in text
    assert "combinations (ranked)" in text


# ── decision_record ─────────────────────────────────────────────────────────
def test_decision_record_columns(mpc):
    assert mpc.decision_record() == {}                    # before solve
    mpc.solve({r: 78.0 for r in mpc.rooms}, [90.0] * mpc.horizon)
    rec = mpc.decision_record()
    for room in mpc.rooms:
        assert f"T_{room}" in rec
        assert f"proj_{room}" in rec
    for ac in mpc.ac_units:
        assert f"on_{ac}" in rec
        assert f"sp_{ac}" in rec
    assert "cost_chosen" in rec
    # one cost_<3bit> column per enumerated combo
    assert sum(1 for k in rec if k.startswith("cost_") and k != "cost_chosen") == 8
