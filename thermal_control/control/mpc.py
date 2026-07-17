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
  cost = Σ_t w_t · Σ_rooms  [ max(0, T_room(t) − T_max)²     # too hot
                             + max(0, T_min − T_room(t))² ]   # too cold
       + mean(w) · energy_weight · Σ_j  power_j · AC_on_j     # energy

where w_t decreases linearly from discount_start (step 1) to discount_end
(last step) — model error accumulates over the rollout, so far-future
predicted breaches count less than near-certain present ones. The energy
term is scaled by mean(w) (equivalent to spreading it per-step and
discounting), which preserves the discomfort/energy balance.

The combo actually *selected* additionally adds switch_penalty × (number of
ACs that would turn ON vs. the previous tick's decision) — a small bias
against flipping an AC on for a marginal gain, to cut down on short-cycling.
Turning an AC OFF is never penalised: energy_weight already pulls the
optimum toward fewer ACs running, and since the per-tick energy cost of one
AC is bounded well below any reasonable switch_penalty, a symmetric penalty
would make that pull unbeatable — an AC that turned on for a real reason
(e.g. a room was occupied and warm) would then stay on indefinitely once
comfort was restored, never releasing back off. This term is selection-only:
the cost/cost_<binary> values reported by decision_record() stay the raw
discomfort+energy above, so logged costs remain comparable across ticks
regardless of switch_penalty.

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
import logging

import numpy as np

logger = logging.getLogger(__name__)


class BangBangMPC:

    def __init__(self, simulator, house_config, control_config):
        self.sim          = simulator
        self.ac_units     = [ac["id"] for ac in house_config["ac_units"]]
        self.rooms        = simulator.rooms

        mpc_cfg           = control_config["mpc"]
        self.horizon      = mpc_cfg["horizon_steps"]
        self.tick_minutes = mpc_cfg["tick_minutes"]
        self.sp_on        = mpc_cfg["setpoint_on_f"]
        self.sp_off       = mpc_cfg["setpoint_off_f"]
        self.energy_w     = mpc_cfg["energy_weight"]

        # Switching-cost penalty (selection-only, see module docstring). Absent
        # key = 0.0 = no bias, matching today's behavior.
        self.switch_penalty = mpc_cfg.get("switch_penalty", 0.0)
        self._best_combo    = None   # previous tick's decision; None until first solve()

        # Per-step discount weights (model error grows over the horizon).
        # Defaults make this a no-op when the config keys are absent.
        self.step_weights = np.linspace(
            mpc_cfg.get("discount_start", 1.0),
            mpc_cfg.get("discount_end", 1.0),
            self.horizon,
        )

        # Static fallback targets (used when solve() is called without targets,
        # e.g. from dry_run.py). The scheduler passes resolved per-tick targets
        # via solve(); "default" and "schedule" are not room ids.
        RESERVED          = {"default", "schedule"}
        default           = control_config["targets"]["default"]
        overrides         = {k: v for k, v in control_config["targets"].items()
                             if k not in RESERVED}
        self.targets      = {
            room: dict(overrides.get(room, default))
            for room in self.rooms
        }

        # AC power for energy penalty (normalised to max)
        raw_power         = control_config.get("ac_power", {})
        max_p             = max(raw_power.values()) if raw_power else 1
        self.ac_power     = {ac: raw_power.get(ac, 1000) / max_p
                             for ac in self.ac_units}

    # ── Cost function ─────────────────────────────────────────────────────
    def _cost(self, states, ac_combo, missing_rooms):
        """
        states        : list of state dicts from simulator.rollout (includes t=0)
        ac_combo      : tuple of 0/1 per AC unit (same order as self.ac_units)
        missing_rooms : set of room_ids to exclude from discomfort cost this tick
        """
        discomfort = 0.0
        for w, state in zip(self.step_weights, states[1:]):   # skip initial state
            for room in self.rooms:
                if room in missing_rooms:
                    continue                           # no data — don't steer cost
                T   = state[room]
                tgt = self._current_targets[room]
                discomfort += w * max(0.0, T - tgt["max_f"]) ** 2   # too hot
                discomfort += w * max(0.0, tgt["min_f"] - T) ** 2   # too cold

        # mean(w) ≡ spreading the energy term per-step and discounting it —
        # keeps the discomfort/energy balance independent of the discount.
        energy = self.step_weights.mean() * sum(
            self.energy_w * self.ac_power[ac] * on
            for ac, on in zip(self.ac_units, ac_combo)
        )
        return discomfort + energy

    # ── Main solve ────────────────────────────────────────────────────────
    def solve(self, current_state, outdoor_series, missing_rooms=None, targets=None):
        """
        current_state  : {room_id: temp_F}       — from independent sensors
        outdoor_series : list of float (°F)       — one value per horizon step
        missing_rooms  : set of room_ids whose sensor is stale/absent this tick;
                         excluded from discomfort cost (fabricated data must not
                         steer the MPC).
        targets        : {room_id: {min_f, max_f}} resolved for this tick by
                         resolve_targets_for_rooms(); falls back to self.targets
                         (init-time snapshot) when not provided.
        Returns        : {ac_id: setpoint_F}      — 65°F (ON) or 84°F (OFF)
        """
        if missing_rooms is None:
            missing_rooms = set()
        self._missing_rooms   = missing_rooms
        self._current_targets = targets if targets is not None else self.targets

        prev_combo     = self._best_combo   # None on the very first solve() call
        self._prev_combo = prev_combo       # kept for explain()

        best_adjusted  = float("inf")
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
            cost = self._cost(states, combo, missing_rooms)
            results.append((combo, setpoints, cost, states))

            turned_on = 0 if prev_combo is None else sum(
                a == 1 and b == 0 for a, b in zip(combo, prev_combo)
            )
            adjusted = cost + self.switch_penalty * turned_on

            if adjusted < best_adjusted:
                best_adjusted  = adjusted
                best_cost      = cost
                best_setpoints = setpoints
                best_combo     = combo

        self._last_results    = results        # for inspection / logging (raw costs)
        self._best_combo      = best_combo
        self._best_cost       = best_cost
        self._current_state   = current_state
        return best_setpoints

    # ── Diagnostics ───────────────────────────────────────────────────────
    def explain(self):
        """
        Return a multi-line string summarising the last solve: comfort
        diagnosis, all 8 combinations ranked by cost, and the projected
        outcome for the chosen combo. Log via logger.info(mpc.explain()).
        """
        if not hasattr(self, "_last_results"):
            return "Call solve() first."
        if not hasattr(self, "_missing_rooms"):
            self._missing_rooms = set()
        if not hasattr(self, "_current_targets"):
            self._current_targets = self.targets

        lines = []
        horizon_min = self.horizon * self.tick_minutes

        # ── 1. Comfort diagnosis ──────────────────────────────────────────
        lines.append(f"\n── Comfort diagnosis {'─' * 48}")
        lines.append(f"  {'Room':<26}  {'Temp':>7}  {'Target':>12}  Status")
        lines.append("  " + "─" * 62)
        hot_rooms, cold_rooms = [], []
        for room in sorted(self.rooms):
            T   = self._current_state.get(room, float("nan"))
            tgt = self._current_targets[room]
            if room in self._missing_rooms:
                status = "NO DATA  (excluded from cost)"
                tgt_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
                lines.append(f"  {room:<26}  {'---':>7}   {tgt_str:>12}  {status}")
                continue
            if T > tgt["max_f"]:
                status = f"TOO HOT  +{T - tgt['max_f']:.1f}°F"
                hot_rooms.append(room)
            elif T < tgt["min_f"]:
                status = f"too cold -{tgt['min_f'] - T:.1f}°F"
                cold_rooms.append(room)
            else:
                status = "ok"
            tgt_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
            lines.append(f"  {room:<26}  {T:>6.1f}°F  {tgt_str:>12}  {status}")

        if hot_rooms:
            lines.append(f"\n  Needs cooling : {', '.join(hot_rooms)}")
        if cold_rooms:
            lines.append(f"  Already cool  : {', '.join(cold_rooms)}")
        if not hot_rooms and not cold_rooms:
            lines.append("\n  All rooms within comfort band.")

        # ── 2. Ranked combinations ────────────────────────────────────────
        prev_combo = getattr(self, "_prev_combo", None)

        def adjusted_cost(combo, cost):
            if prev_combo is None:
                return cost
            turned_on = sum(a == 1 and b == 0 for a, b in zip(combo, prev_combo))
            return cost + self.switch_penalty * turned_on

        lines.append(f"\n── All {len(self._last_results)} combinations (ranked) {'─' * 38}")
        header = f"  {'Combo (bed/liv/ext)':<22}  {'Setpoints':>30}  {'Cost':>10}"
        if self.switch_penalty:
            header += f"  {'+switch':>10}"
        lines.append(header)
        lines.append("  " + "─" * 68)
        for combo, sp, cost, _ in sorted(self._last_results, key=lambda x: adjusted_cost(x[0], x[2])):
            combo_str = "/".join("ON " if c else "off" for c in combo)
            sp_str    = " ".join(f"{ac.split('_')[0]}={v}" for ac, v in sp.items())
            marker    = " ◀ chosen" if combo == self._best_combo else ""
            if prev_combo is not None and combo == prev_combo and combo != self._best_combo:
                marker += " (prev)"
            row = f"  {combo_str:<20}  {sp_str:>30}  {cost:>10.4f}"
            if self.switch_penalty:
                row += f"  {adjusted_cost(combo, cost):>10.4f}"
            lines.append(row + marker)

        # ── 3. Projected outcome for chosen combo ─────────────────────────
        chosen_states = next(
            states for combo, _, _, states in self._last_results
            if combo == self._best_combo
        )
        final = chosen_states[-1]

        lines.append(f"\n── Projected outcome in {horizon_min} min (chosen combo) {'─' * 28}")
        lines.append(f"  {'Room':<26}  {'Now':>7}  {f'In {horizon_min}min':>9}  {'Target':>12}  Trend")
        lines.append("  " + "─" * 68)
        for room in sorted(self.rooms):
            T_now   = self._current_state.get(room, float("nan"))
            T_end   = final.get(room, float("nan"))
            tgt     = self._current_targets[room]
            tgt_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
            delta   = T_end - T_now
            trend   = f"{'▼' if delta < -0.2 else '▲' if delta > 0.2 else '─'} {abs(delta):.1f}°F"
            in_band = tgt["min_f"] <= T_end <= tgt["max_f"]
            ok_str  = "✓" if in_band else ("↑hot" if T_end > tgt["max_f"] else "↓cold")
            lines.append(f"  {room:<26}  {T_now:>6.1f}°F  {T_end:>8.1f}°F  {tgt_str:>12}  {trend}  {ok_str}")

        return "\n".join(lines)

    def decision_record(self):
        """
        Return a flat dict representing this tick's MPC decision. Intended
        for appending to the CSV decision log — one row per tick.

        Columns:
          T_<room>          current temperature per room (°F)
          on_<ac>           1/0 — whether the MPC chose ON for each AC
          sp_<ac>           applied setpoint (65 or 84°F) per AC
          cost_chosen       cost of the chosen combination
          cost_<binary>     cost for each of the 8 combinations, keyed by
                            a 3-bit string e.g. "010" (bed=0,liv=1,ext=0)
          switch_penalty    the switch_penalty value in effect this tick (raw
                            costs above are unaffected by it — this column is
                            what lets a later analysis reconstruct which combo
                            the penalty would have changed, if any)
          proj_<room>       predicted temperature at horizon end (°F)
        """
        if not hasattr(self, "_last_results"):
            return {}

        record = {}

        for room in self.rooms:
            record[f"T_{room}"] = round(self._current_state.get(room, float("nan")), 2)

        for ac, on in zip(self.ac_units, self._best_combo):
            record[f"on_{ac}"]  = on
            record[f"sp_{ac}"]  = self.sp_on if on else self.sp_off

        record["cost_chosen"] = round(self._best_cost, 4)
        record["switch_penalty"] = self.switch_penalty

        for combo, _, cost, _ in self._last_results:
            key = "".join(str(c) for c in combo)
            record[f"cost_{key}"] = round(cost, 4)

        chosen_states = next(
            states for combo, _, _, states in self._last_results
            if combo == self._best_combo
        )
        final = chosen_states[-1]
        for room in self.rooms:
            record[f"proj_{room}"] = round(final.get(room, float("nan")), 2)

        return record
