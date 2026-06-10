"""
simulate.py
───────────
Two-layer forward simulator for the house thermal model.

  Layer 2 — Switching logic (deterministic physics):
    AC_on_j = 1  if  T_sensor_room_j > Setpoint_j
              0  otherwise
    No hysteresis (confirmed from data: 96%+ of transitions at delta=0).
    T_sensor_room_j is the simulated temperature of the room where
    AC unit j's controller sensor is physically located.

  Layer 3 — Thermal model (fitted Ridge regressions):
    T_room(t+1) = model_room.predict(features_at_t)

Pass 2 validation compares two rollout strategies against ground truth
on the held-out validation set:

  One-step-ahead : at each step, reset room temps to observed values
                   but derive AC_on from simulated switching.
                   Isolates switching model error only.

  Full rollout   : accumulate state across all steps; errors compound.
                   Reflects real-world simulator drift.
                   Run over 2-hour windows (12 steps), average MAE.
                   Target: < 1.5°C drift after 2 hours.

Outputs:
  thermal_control/model/simulate_validation.png
"""

import json
import yaml
import joblib
import numpy as np
import pandas as pd
from pathlib import Path


# ── HouseSimulator ────────────────────────────────────────────────────────
class HouseSimulator:

    def __init__(self, weights_dir, house_config):
        with open(weights_dir / "feature_lists.json") as f:
            self.feature_lists = json.load(f)
        self.models      = {
            room: joblib.load(weights_dir / f"{room}.joblib")
            for room in self.feature_lists
        }
        self.rooms       = list(self.feature_lists.keys())
        self.ac_units    = house_config["ac_units"]
        self.sensor_rooms = {ac["id"]: ac["sensor_room"] for ac in self.ac_units}

    def _derive_ac_on(self, state, setpoints):
        """
        Layer 2: switching logic.
        state     : {room_id: temp_F}  — simulated room temperatures
        setpoints : {ac_id: setpoint_F}
        Returns   : {ac_id: 0 or 1}
        """
        ac_on = {}
        for ac in self.ac_units:
            ac_id       = ac["id"]
            sensor_room = self.sensor_rooms[ac_id]
            T_sensor    = state.get(sensor_room, np.nan)
            sp          = setpoints[ac_id]
            ac_on[ac_id] = 1 if (not np.isnan(T_sensor) and T_sensor > sp) else 0
        return ac_on

    def step(self, state, setpoints, T_outdoor):
        """
        Advance all rooms by one 10-minute timestep.
        state     : {room_id: temp_F}
        setpoints : {ac_id: setpoint_F}
        T_outdoor : float (°F)
        Returns   : next_state {room_id: temp_F}, ac_on {ac_id: 0/1}
        """
        ac_on      = self._derive_ac_on(state, setpoints)
        next_state = {}
        for room_id, features in self.feature_lists.items():
            row = {}
            for feat in features:
                if feat.endswith("_lag1") and feat.startswith("T_") and feat != "T_outdoor_lag1":
                    room_key = feat[len("T_"):-len("_lag1")]
                    row[feat] = state.get(room_key, np.nan)
                elif feat == "T_outdoor_lag1":
                    row[feat] = T_outdoor
                elif feat.startswith("ac_on_"):
                    ac_id     = feat[len("ac_on_"):]
                    row[feat] = ac_on.get(ac_id, 0)
            X              = pd.DataFrame([row])[features]
            next_state[room_id] = self.models[room_id].predict(X)[0]
        return next_state, ac_on

    def rollout(self, initial_state, setpoint_schedule, outdoor_series):
        """
        Multi-step forward simulation.
        initial_state     : {room_id: temp_F}
        setpoint_schedule : list of {ac_id: setpoint_F}, one per step
        outdoor_series    : list of float (°F), one per step
        Returns list of state dicts (length = n_steps + 1, includes initial).
        """
        states = [initial_state]
        state  = initial_state.copy()
        for setpoints, T_out in zip(setpoint_schedule, outdoor_series):
            state, _ = self.step(state, setpoints, T_out)
            states.append(state.copy())
        return states


if __name__ == "__main__":
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # ── Config ────────────────────────────────────────────────────────────
    HOUSE_YAML   = "thermal_control/config/house.yaml"
    VAL_CSV      = "thermal_control/preprocess/val.csv"
    WEIGHTS_DIR  = Path("thermal_control/model/weights")
    OUTPUT_PNG   = "thermal_control/model/simulate_validation.png"

    with open(HOUSE_YAML) as f:
        house = yaml.safe_load(f)

    val = pd.read_csv(VAL_CSV, index_col="time", parse_dates=True)
    sim = HouseSimulator(WEIGHTS_DIR, house)

    AC_IDS    = [ac["id"] for ac in house["ac_units"]]
    ROOMS     = sim.rooms
    WINDOW    = 12   # 2-hour rollout window in 10-min steps

    # ── Pass 2a: one-step-ahead with simulated switching ──────────────────
    print("── Pass 2a: one-step-ahead (simulated switching) ───────────────────")
    records = []
    for i in range(len(val) - 1):
        row      = val.iloc[i]
        row_next = val.iloc[i + 1]
        state    = {r: row[f"T_{r}"] for r in ROOMS}
        setpoints = {a: row[f"setpoint_{a}"] for a in AC_IDS}
        T_out    = row["T_outdoor"]
        next_state, ac_on = sim.step(state, setpoints, T_out)
        for room_id in ROOMS:
            records.append({
                "room": room_id,
                "actual": row_next[f"T_{room_id}"],
                "pred":   next_state[room_id],
            })

    df_1step = pd.DataFrame(records)
    print(f"{'Room':<18}  {'MAE (°F)':>10}  {'MAE (°C)':>10}")
    print("─" * 44)
    for room_id in sorted(ROOMS):
        sub = df_1step[df_1step["room"] == room_id]
        mae_f = (sub["actual"] - sub["pred"]).abs().mean()
        print(f"  {room_id:<18}  {mae_f:>10.4f}  {mae_f/1.8:>10.4f}")

    # ── Pass 2b: full 2-hour rollout windows ──────────────────────────────
    print("\n── Pass 2b: 2-hour rollout windows (simulated switching) ───────────")
    mae_per_room = {r: [] for r in ROOMS}

    for start in range(0, len(val) - WINDOW, WINDOW):
        chunk     = val.iloc[start: start + WINDOW + 1]
        init      = {r: chunk.iloc[0][f"T_{r}"] for r in ROOMS}
        setpoints = [{a: chunk.iloc[t][f"setpoint_{a}"] for a in AC_IDS}
                     for t in range(WINDOW)]
        outdoors  = [chunk.iloc[t]["T_outdoor"] for t in range(WINDOW)]

        states = sim.rollout(init, setpoints, outdoors)

        for t in range(1, WINDOW + 1):
            if start + t >= len(val):
                break
            actual_row = val.iloc[start + t]
            for room_id in ROOMS:
                err = abs(states[t][room_id] - actual_row[f"T_{room_id}"])
                mae_per_room[room_id].append(err)

    print(f"{'Room':<18}  {'MAE (°F)':>10}  {'MAE (°C)':>10}  {'< 1.5°C?':>10}")
    print("─" * 54)
    for room_id in sorted(ROOMS):
        mae_f = np.mean(mae_per_room[room_id])
        mae_c = mae_f / 1.8
        ok    = "✓" if mae_c < 1.5 else "✗"
        print(f"  {room_id:<18}  {mae_f:>10.4f}  {mae_c:>10.4f}  {ok:>10}")

    # ── Plot: full rollout on entire validation set ───────────────────────
    init      = {r: val.iloc[0][f"T_{r}"] for r in ROOMS}
    setpoints = [{a: val.iloc[t][f"setpoint_{a}"] for a in AC_IDS} for t in range(len(val))]
    outdoors  = [val.iloc[t]["T_outdoor"] for t in range(len(val))]
    all_states = sim.rollout(init, setpoints, outdoors)

    fig, axes = plt.subplots(len(ROOMS), 1, figsize=(14, 2.5 * len(ROOMS)), sharex=True)
    for ax, room_id in zip(axes, sorted(ROOMS)):
        actual = val[f"T_{room_id}"].values
        pred   = [s[room_id] for s in all_states[1:]]
        mae_c  = np.mean(np.abs(np.array(actual) - np.array(pred))) / 1.8
        ax.plot(val.index, actual, color="black",   linewidth=0.8, label="Actual",        alpha=0.9)
        ax.plot(val.index, pred,   color="#DD8452", linewidth=0.8, label=f"Simulated  MAE={mae_c:.3f}°C", linestyle="--")
        ax.set_title(room_id.replace("_", " "), fontsize=9, loc="left")
        ax.set_ylabel("°F", fontsize=8)
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, alpha=0.2)

    fig.suptitle("Pass 2 — full rollout on validation set (simulated AC switching)", y=1.005)
    plt.tight_layout()
    plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
    print(f"\nPlot saved → {OUTPUT_PNG}")
