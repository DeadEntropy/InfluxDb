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
Currently uses the current observed outdoor temperature as a constant
over the horizon. TODO: replace with Open-Meteo forecast (forecast.py).

Usage
─────
    from thermal_control.control.mpc import BangBangMPC
    from thermal_control.model.simulate import HouseSimulator

    sim = HouseSimulator(weights_dir, house_config)
    mpc = BangBangMPC(sim, house_config, control_config)

    optimal_setpoints = mpc.solve(current_state, current_outdoor)
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
    def solve(self, current_state, current_outdoor):
        """
        current_state   : {room_id: temp_F}  — from independent sensors
        current_outdoor : float (°F)          — current outdoor temperature
        Returns         : {ac_id: setpoint_F} — min or max per AC
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
            # Use constant outdoor temp over horizon (TODO: use forecast)
            states = self.sim.rollout(
                current_state,
                [setpoints] * self.horizon,
                [current_outdoor] * self.horizon,
            )
            cost = self._cost(states, combo)
            results.append((combo, setpoints, cost, states))

            if cost < best_cost:
                best_cost      = cost
                best_setpoints = setpoints
                best_combo     = combo

        self._last_results = results        # for inspection / logging
        self._best_combo   = best_combo
        self._best_cost    = best_cost
        return best_setpoints

    # ── Diagnostics ───────────────────────────────────────────────────────
    def explain(self):
        """Print a ranked table of all 8 combinations after solve()."""
        if not hasattr(self, "_last_results"):
            print("Call solve() first.")
            return
        print(f"\n{'Combo (bed/liv/ext)':<22}  {'Setpoints':>30}  {'Cost':>10}")
        print("─" * 68)
        for combo, sp, cost, _ in sorted(self._last_results, key=lambda x: x[2]):
            combo_str = "/".join("ON " if c else "off" for c in combo)
            sp_str    = " ".join(f"{ac.split('_')[0]}={v}" for ac, v in sp.items())
            marker    = " ◀ chosen" if combo == self._best_combo else ""
            print(f"  {combo_str:<20}  {sp_str:>30}  {cost:>10.4f}{marker}")
