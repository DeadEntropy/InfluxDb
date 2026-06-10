"""
mpc.py
──────
Bang-bang Model Predictive Controller.

The MPC reads actual room temperatures from independent sensors,
evaluates all 2³ = 8 possible AC on/off combinations over a 3-hour
lookahead horizon using the fitted thermal model, and selects the
combination that minimises total discomfort while using as little
energy as possible.

Control law
───────────
Each AC unit has exactly two states:
  ON  → setpoint set to setpoint_on_f  (e.g. 65°F) — guarantees the
         physical controller triggers since indoor temps always exceed 65°F
  OFF → setpoint set to setpoint_off_f (e.g. 84°F) — guarantees the
         physical controller never triggers

This bypasses the broken physical control loop (bedroom AC sensor in
dining room) by commanding the AC directly rather than relying on the
controller's own sensor to make the switching decision.

Objective
─────────
  cost = Σ_t Σ_rooms  [ max(0, T_room(t) − T_max)²          # too hot
                       + max(0, T_min − T_room(t))² ]        # too cold
       + energy_weight · Σ_j  power_j · AC_on_j              # energy

Enumeration
───────────
With 3 AC units there are 2³ = 8 combinations. Each is evaluated by
running the forward simulator for `horizon_steps` timesteps. The
combination with the lowest total cost wins.

Outdoor temperature
───────────────────
Accepts an outdoor_series list (one °F value per horizon step). When
use_forecast is enabled in control.yaml, the caller passes a real
forecast from control/forecast.py; otherwise a constant repeated value
is passed. The MPC itself is agnostic to the source.

Usage
─────
    from thermal_control.control.mpc import BangBangMPC
    from thermal_control.model.simulate import HouseSimulator

    sim = HouseSimulator(weights_dir, house_config)
    mpc = BangBangMPC(sim, house_config, control_config)

    optimal_setpoints = mpc.solve(current_state, outdoor_series)
    # outdoor_series: list of °F, length = horizon_steps
    # returns e.g. {'bedroom_ac': 65, 'living_ac': 84, 'extension_ac': 65}
"""

import itertools
import numpy as np


class BangBangMPC:

    def __init__(self, simulator, house_config, control_config):
        self.sim          = simulator
        self.ac_units     = [ac["id"] for ac in house_config["ac_units"]]
        self.rooms        = simulator.rooms

        mpc_cfg           = control_config["mpc"]
        self.horizon      = mpc_cfg["horizon_steps"]
        self.sp_on        = mpc_cfg["setpoint_on_f"]
        self.sp_off       = mpc_cfg["setpoint_off_f"]
        self.energy_w     = mpc_cfg["energy_weight"]

        # Comfort targets per room (with per-room overrides)
        default           = control_config["targets"]["default"]
        overrides         = {k: v for k, v in control_config["targets"].items()
                             if k != "default"}
        self.targets      = {
            room: overrides.get(room, default)
            for room in self.rooms
        }

        # AC power for energy penalty (normalised to max)
        raw_power         = control_config.get("ac_power", {})
        max_p             = max(raw_power.values()) if raw_power else 1
        self.ac_power     = {ac: raw_power.get(ac, 1000) / max_p
                             for ac in self.ac_units}

    # ── Cost function ─────────────────────────────────────────────────────
    def _cost(self, states, ac_combo):
        """
        states    : list of state dicts from simulator.rollout (includes t=0)
        ac_combo  : tuple of 0/1 per AC unit (same order as self.ac_units)
        """
        discomfort = 0.0
        for state in states[1:]:                      # skip initial state
            for room in self.rooms:
                T   = state[room]
                tgt = self.targets[room]
                discomfort += max(0.0, T - tgt["max_f"]) ** 2   # too hot
                discomfort += max(0.0, tgt["min_f"] - T) ** 2   # too cold

        energy = sum(
            self.energy_w * self.ac_power[ac] * on
            for ac, on in zip(self.ac_units, ac_combo)
        )
        return discomfort + energy

    # ── Main solve ────────────────────────────────────────────────────────
    def solve(self, current_state, outdoor_series):
        """
        current_state  : {room_id: temp_F}       — from independent sensors
        outdoor_series : list of float (°F)       — one value per horizon step
        Returns        : {ac_id: setpoint_F}      — 65°F (ON) or 84°F (OFF)
        """
        best_cost      = float("inf")
        best_setpoints = None
        best_combo     = None
        results        = []

        for combo in itertools.product([0, 1], repeat=len(self.ac_units)):
            setpoints = {
                ac: (self.sp_on if on else self.sp_off)
                for ac, on in zip(self.ac_units, combo)
            }
            states = self.sim.rollout(
                current_state,
                [setpoints] * self.horizon,
                outdoor_series,
            )
            cost = self._cost(states, combo)
            results.append((combo, setpoints, cost, states))

            if cost < best_cost:
                best_cost      = cost
                best_setpoints = setpoints
                best_combo     = combo

        self._last_results    = results        # for inspection / logging
        self._best_combo      = best_combo
        self._best_cost       = best_cost
        self._current_state   = current_state
        return best_setpoints

    # ── Diagnostics ───────────────────────────────────────────────────────
    def explain(self):
        """Print diagnosis, ranked combinations, and projected outcome."""
        if not hasattr(self, "_last_results"):
            print("Call solve() first.")
            return

        # ── 1. Comfort diagnosis ──────────────────────────────────────────
        print(f"\n── Comfort diagnosis {'─' * 48}")
        print(f"  {'Room':<26}  {'Temp':>7}  {'Target':>12}  Status")
        print("  " + "─" * 62)
        hot_rooms  = []
        cold_rooms = []
        for room in sorted(self.rooms):
            T   = self._current_state.get(room, float("nan"))
            tgt = self.targets[room]
            if T > tgt["max_f"]:
                status = f"TOO HOT  +{T - tgt['max_f']:.1f}°F"
                hot_rooms.append(room)
            elif T < tgt["min_f"]:
                status = f"too cold -{tgt['min_f'] - T:.1f}°F"
                cold_rooms.append(room)
            else:
                status = "ok"
            tgt_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
            print(f"  {room:<26}  {T:>6.1f}°F  {tgt_str:>12}  {status}")

        if hot_rooms:
            print(f"\n  Needs cooling : {', '.join(hot_rooms)}")
        if cold_rooms:
            print(f"  Already cool  : {', '.join(cold_rooms)}")
        if not hot_rooms and not cold_rooms:
            print("\n  All rooms within comfort band.")

        # ── 2. Ranked combinations ────────────────────────────────────────
        print(f"\n── All {len(self._last_results)} combinations (ranked) {'─' * 38}")
        print(f"  {'Combo (bed/liv/ext)':<22}  {'Setpoints':>30}  {'Cost':>10}")
        print("  " + "─" * 68)
        for combo, sp, cost, _ in sorted(self._last_results, key=lambda x: x[2]):
            combo_str = "/".join("ON " if c else "off" for c in combo)
            sp_str    = " ".join(f"{ac.split('_')[0]}={v}" for ac, v in sp.items())
            marker    = " ◀ chosen" if combo == self._best_combo else ""
            print(f"  {combo_str:<20}  {sp_str:>30}  {cost:>10.4f}{marker}")

        # ── 3. Projected outcome for chosen combo ─────────────────────────
        chosen_states = next(
            states for combo, _, _, states in self._last_results
            if combo == self._best_combo
        )
        final = chosen_states[-1]
        horizon_min = len(chosen_states) * 10  # each step = 10 min; includes t=0

        print(f"\n── Projected outcome in {horizon_min - 10} min (chosen combo) {'─' * 28}")
        print(f"  {'Room':<26}  {'Now':>7}  {'In {h}min'.format(h=horizon_min-10):>9}  {'Target':>12}  Trend")
        print("  " + "─" * 68)
        for room in sorted(self.rooms):
            T_now  = self._current_state.get(room, float("nan"))
            T_end  = final.get(room, float("nan"))
            tgt    = self.targets[room]
            tgt_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
            delta  = T_end - T_now
            trend  = f"{'▼' if delta < -0.2 else '▲' if delta > 0.2 else '─'} {abs(delta):.1f}°F"
            in_band = tgt["min_f"] <= T_end <= tgt["max_f"]
            ok_str = "✓" if in_band else ("↑hot" if T_end > tgt["max_f"] else "↓cold")
            print(f"  {room:<26}  {T_now:>6.1f}°F  {T_end:>8.1f}°F  {tgt_str:>12}  {trend}  {ok_str}")
