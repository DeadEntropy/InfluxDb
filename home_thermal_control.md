# Home Thermal DAG & MPC Controller — Implementation Spec

## Project goal

Build a calibrated causal thermal model of the house (a DAG), then use it as the forward model inside a Model Predictive Controller (MPC) that automatically drives three AC units to maintain target temperatures in every room while minimizing energy consumption. Once running, the system should require no manual AC interaction.

---

## Architecture overview

The control chain is indirect. The MPC does not command AC units on/off directly. Instead:

```
MPC sets Setpoint_j on AC controller j
       ↓
AC_j compares Setpoint_j to T_sensor_j  (sensor in controller's room, not target room)
       ↓
AC_j switches on if T_sensor_j > Setpoint_j
       ↓
AC_j cools its coverage zone (may include rooms other than the controller's room)
```

This creates cross-room entanglement: to cool room A (served by AC_1 whose controller lives in room B), the MPC must manipulate `Setpoint_1` such that the temperature in room B — which is itself influenced by AC_2 — triggers AC_1 to run. The MPC must reason about all setpoints jointly.

The full DAG has three layers:

```
Layer 1 (exogenous inputs):   Setpoint_j,  T_outdoor
Layer 2 (derived switching):  AC_on_j  =  f(AC_sensor_j, Setpoint_j)   ← discrete step function
Layer 3 (endogenous):         T_room_X(t+1) = g(T_rooms(t), AC_on(t), T_outdoor(t))
```

Layer 2 is a deterministic physical rule, not regressed. Layer 3 is fit from data.

---

## ⚠️ Pre-implementation decision: sensor resolution and hysteresis

**Before writing any MPC or simulator code**, the agent must verify two things from the raw HA database. The answers determine the solver formulation.

### 1. Sensor resolution

Pull a sample of raw AC controller sensor readings and inspect the distribution of non-zero differences:

```python
diff = df["ac_sensor_temp_raw"].diff().dropna()
print(diff[diff != 0].value_counts().head(10))
```

- If all non-zero differences are multiples of **~0.556°C** (= 1°F in Celsius), the sensor reports in **rounded Fahrenheit**. This is the expected case.
- If finer granularity is present, rounding may only occur at display level.

**If rounded to whole °F**: the switching condition `AC_on = (T_sensor_F > Setpoint_F)` is a discrete integer comparison. Do not use sigmoid relaxation — it is inappropriate when both sides are coarsely quantized. Use MILP (see Phase 6).

### 2. AC hysteresis

Check whether the controller has a deadband: turns on above setpoint, but only turns off some number of degrees below it.

```python
# For each AC, find timesteps where hvac_action transitions from "cooling" to "idle"
# Inspect T_sensor_F - Setpoint_F at those moments
# If consistently negative (e.g. -1 to -2°F), hysteresis h = that value
```

If hysteresis is confirmed, the switching model is asymmetric and the AC's previous on/off state becomes explicit simulator state.

**Record both findings in `config/house.yaml` before proceeding to Phase 1.**

---

## Repository layout

```
thermal_control/
├── config/
│   ├── house.yaml          # room layout, adjacency, AC zone mapping, sensor resolution
│   └── control.yaml        # MPC parameters, target temperatures, schedule
├── data/
│   ├── extract.py          # pulls data from Home Assistant SQLite DB
│   └── preprocess.py       # resampling, alignment, feature engineering
├── model/
│   ├── dag.py              # DAG structure + structural equations
│   ├── fit.py              # regression-based calibration
│   └── simulate.py         # forward simulator (steps the DAG through time)
├── control/
│   ├── mpc.py              # MPC optimization loop
│   └── forecast.py         # outdoor temperature forecast (Open-Meteo)
├── ha_bridge/
│   └── controller.py       # reads sensors / sends commands to Home Assistant
├── scheduler.py            # main loop, runs every N minutes
├── recalibrate.py          # monthly refit script
├── requirements.txt
└── README.md
```

---

## House configuration (`config/house.yaml`)

The agent must ask the user to fill this in before any code runs. Structure:

```yaml
rooms:
  - id: living_room
    sensor_entity: sensor.living_room_temperature
    exterior_walls: true
  - id: kitchen
    sensor_entity: sensor.kitchen_temperature
    exterior_walls: true
  - id: master_bedroom
    sensor_entity: sensor.master_bedroom_temperature
    exterior_walls: true
  - id: bedroom_2
    sensor_entity: sensor.bedroom_2_temperature
    exterior_walls: false
  # ... add all rooms

adjacency:
  # Pairs of rooms that share a wall or open air path
  - [living_room, kitchen]
  - [living_room, bedroom_2]
  - [master_bedroom, bedroom_2]
  # ...

ac_units:
  - id: ac_1
    climate_entity: climate.ac_living_room
    sensor_room: bedroom_2          # room where THIS AC's controller sensor lives
    covers: [living_room, kitchen]  # rooms this unit thermally influences
  - id: ac_2
    climate_entity: climate.ac_master
    sensor_room: master_bedroom
    covers: [master_bedroom]
  - id: ac_3
    climate_entity: climate.ac_bedrooms
    sensor_room: bedroom_2          # same sensor_room as ac_1 = entangled control
    covers: [bedroom_2]
    # rooms can appear in multiple covers lists if coverage overlaps

outdoor_sensor: sensor.outdoor_temperature

# Filled in after pre-implementation sensor checks:
sensor_resolution:
  unit: fahrenheit        # or celsius
  rounded: true           # true if sensor reports in whole °F
  setpoint_step_f: 1      # minimum setpoint increment in °F
  hysteresis_f: 0         # deadband in °F; 0 if not detected
```

---

## Control configuration (`config/control.yaml`)

```yaml
mpc:
  horizon_hours: 3          # look-ahead window
  timestep_minutes: 10      # must match data resampling interval
  comfort_weight: 1.0       # weight on temperature deviation penalty
  energy_weight: 0.3        # weight on AC power consumption (tune this)
  solver: MILP              # confirmed after sensor resolution check
  setpoint_range_f:
    min: 68                 # minimum allowable setpoint in °F
    max: 82                 # maximum allowable setpoint in °F

targets:
  default:
    min_f: 68
    max_f: 76
  schedule:
    - time: "23:00"
      min_f: 66
      max_f: 78             # relaxed overnight
    - time: "07:00"
      min_f: 69
      max_f: 75

ac_power:
  # Approximate watts when actively cooling — refine from specs or clamp meter
  ac_1: 1200
  ac_2: 900
  ac_3: 900
```

---

## Phase 1 — Data extraction (`data/extract.py`)

Home Assistant stores data in an SQLite database, typically at:
`/home/homeassistant/.homeassistant/home-assistant_v2.db`

### What to extract

- **Room temperature sensors**: all independent `sensor.*_temperature` entities
- **AC controller sensor temperature**: the `current_temperature` attribute of each `climate.*` entity — this is the temperature the AC compares against its setpoint, and may differ from the room's independent sensor
- **AC setpoints**: the `temperature` attribute of each `climate.*` entity
- **AC on/off state**: `hvac_action` attribute — `"cooling"` = on, `"idle"`/`"off"` = off
- **Outdoor temperature**: dedicated sensor or `weather.*` entity

### Key HA database tables

```
states          — every state change ever recorded
  columns: entity_id, state, last_changed, last_updated, attributes (JSON)

statistics      — long-term aggregated stats (more efficient for >6 months)
  columns: metadata_id, start, mean, min, max

statistics_meta — maps metadata_id to entity_id
```

### Extraction logic

```python
import sqlite3
import pandas as pd
import json

DB_PATH = "/path/to/home-assistant_v2.db"

def extract_entity_history(entity_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT last_changed, state
        FROM states
        WHERE entity_id = ?
          AND last_changed BETWEEN ? AND ?
          AND state NOT IN ('unavailable', 'unknown')
        ORDER BY last_changed ASC
    """
    df = pd.read_sql_query(query, conn, params=(entity_id, start_date, end_date))
    conn.close()
    df["last_changed"] = pd.to_datetime(df["last_changed"])
    df["state"] = pd.to_numeric(df["state"], errors="coerce")
    return df.dropna(subset=["state"]).set_index("last_changed")

def extract_ac_attributes(entity_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT last_changed, state, attributes
        FROM states
        WHERE entity_id = ?
          AND last_changed BETWEEN ? AND ?
          AND state NOT IN ('unavailable', 'unknown')
        ORDER BY last_changed ASC
    """
    df = pd.read_sql_query(query, conn, params=(entity_id, start_date, end_date))
    conn.close()
    df["last_changed"] = pd.to_datetime(df["last_changed"])
    attrs = df["attributes"].apply(json.loads)
    df["setpoint"]       = attrs.apply(lambda a: a.get("temperature"))
    df["ac_sensor_temp"] = attrs.apply(lambda a: a.get("current_temperature"))
    df["hvac_action"]    = attrs.apply(lambda a: a.get("hvac_action"))
    df["ac_on"]          = (df["hvac_action"] == "cooling").astype(int)
    return df.set_index("last_changed")[["setpoint", "ac_sensor_temp", "hvac_action", "ac_on"]]
```

**Important**: `ac_sensor_temp` is what the AC controller uses for switching decisions. It may differ from the room's independent sensor due to sensor placement (e.g. higher on wall, near a vent). Always use `ac_sensor_temp` in the switching model, not the room sensor.

---

## Phase 2 — Preprocessing (`data/preprocess.py`)

### Steps

1. **Resample** all time series to a fixed 10-minute interval. Forward-fill AC states and setpoints; use mean for temperature sensors.
2. **Align** all series to a common DatetimeIndex.
3. **Derive `AC_on_j`** from `hvac_action` history (`"cooling"` → 1, else 0). This binary signal is the ground-truth AC feature used in room model fitting — not the setpoint.
4. **Create lagged features**: for each variable X, create `X_lag1` (value at previous timestep).
5. **Add time features**: `hour_sin` and `hour_cos` for cyclical encoding of time of day.
6. **Train/validation split**: last 2 weeks as held-out validation; train on everything before.

### Output schema

```
timestamp (index)
T_<room_id>              # room temperature from independent sensor
T_<room_id>_lag1
AC_on_<ac_id>            # binary: 1 if actively cooling, from hvac_action
AC_sensor_<ac_id>        # temperature seen by the AC controller's sensor (in °F if rounded)
AC_sensor_<ac_id>_lag1
Setpoint_<ac_id>         # setpoint in °F (integer if sensor is rounded)
T_outdoor
T_outdoor_lag1
hour_sin, hour_cos
```

---

## Phase 3 — DAG definition and calibration (`model/dag.py`, `model/fit.py`)

### Structural equation per room

For each room X, the model predicts `T_X(t+1)`:

```
T_X(t+1) = α_X · T_X(t)                            # thermal inertia
          + Σ_{Y adjacent to X} β_XY · T_Y(t)       # conduction from neighbors
          + Σ_{j: AC_j covers X} γ_Xj · AC_on_j(t)  # AC influence (binary on/off)
          + δ_X · T_outdoor(t)                       # exterior coupling
          + ε_X
```

The regressor is `AC_on_j(t)` — the observed binary on/off state — not the setpoint. The setpoint affects room temperatures only indirectly through the switching mechanism, which is modeled separately in the simulator.

### Fitting

One Ridge regression per room. Do not use a single global model.

```python
from sklearn.linear_model import RidgeCV
from sklearn.metrics import mean_absolute_error

def fit_room_model(room_id, df, adjacency, ac_coverage):
    features = [f"T_{room_id}_lag1"]
    for neighbor in adjacency[room_id]:
        features.append(f"T_{neighbor}_lag1")
    for ac_id, covers in ac_coverage.items():
        if room_id in covers:
            features.append(f"AC_on_{ac_id}")   # binary on/off, not setpoint
    features += ["T_outdoor_lag1", "hour_sin", "hour_cos"]

    X = df[features].dropna()
    y = df.loc[X.index, f"T_{room_id}"]

    model = RidgeCV(alphas=[0.01, 0.1, 1.0, 10.0], cv=5)
    model.fit(X, y)
    mae = mean_absolute_error(y, model.predict(X))
    print(f"{room_id}: MAE = {mae:.3f}°C, α = {model.alpha_}")
    return model, features
```

### Validation — two passes

**Pass 1 — thermal model only**: run rollout on held-out 2 weeks using *observed* `AC_on_j` values. Target MAE < 1.0°C per room. This isolates thermal model error from switching model error.

**Pass 2 — full loop**: run rollout using *simulated* switching (derive `AC_on_j` from `AC_sensor_j` and `Setpoint_j` via the switching rule). Any increase in error vs Pass 1 quantifies switching model contribution. Target MAE < 1.5°C.

---

## Phase 4 — Forward simulator (`model/simulate.py`)

The simulator encapsulates both DAG layers and steps the full house state forward.

```python
class HouseSimulator:
    def __init__(self, room_models, config):
        self.room_models  = room_models   # {room_id: (model, feature_list)}
        self.ac_units     = config["ac_units"]
        self.hysteresis_f = config["sensor_resolution"]["hysteresis_f"]
        self.timestep_h   = config["mpc"]["timestep_minutes"] / 60

    def derive_ac_on(self, state_f, setpoints_f, ac_on_prev):
        """
        Layer 2: derive AC on/off from controller sensor temps and setpoints.
        state_f: {room_id: temp_F}  — use ac_sensor_temp values here, not room sensors
        setpoints_f: {ac_id: setpoint_F}  (integer)
        ac_on_prev: {ac_id: int}  — required when hysteresis > 0
        """
        ac_on = {}
        for ac in self.ac_units:
            ac_id       = ac["id"]
            sensor_room = ac["sensor_room"]
            T_f         = state_f[sensor_room]   # temperature the controller sees
            sp_f        = setpoints_f[ac_id]

            if self.hysteresis_f > 0:
                if ac_on_prev[ac_id]:
                    ac_on[ac_id] = 1 if T_f > (sp_f - self.hysteresis_f) else 0
                else:
                    ac_on[ac_id] = 1 if T_f > sp_f else 0
            else:
                ac_on[ac_id] = 1 if T_f > sp_f else 0
        return ac_on

    def step(self, state_c, state_f, setpoints_f, outdoor_temp_c, hour, ac_on_prev=None):
        """
        state_c: room temperatures in Celsius (for thermal model)
        state_f: AC controller sensor readings in °F (for switching logic)
        Returns (next_state_c, next_state_f, ac_on)
        """
        if ac_on_prev is None:
            ac_on_prev = {ac["id"]: 0 for ac in self.ac_units}

        ac_on = self.derive_ac_on(state_f, setpoints_f, ac_on_prev)

        next_state_c = {}
        for room_id, (model, features) in self.room_models.items():
            row = self._build_feature_row(room_id, state_c, ac_on, outdoor_temp_c, hour)
            next_state_c[room_id] = model.predict([row])[0]

        # Convert next state to °F for switching logic on next step
        next_state_f = {r: celsius_to_fahrenheit(t) for r, t in next_state_c.items()}
        return next_state_c, next_state_f, ac_on

    def rollout(self, initial_state_c, initial_state_f, setpoint_schedule, outdoor_forecast_c, start_hour):
        """
        setpoint_schedule: list of {ac_id: setpoint_F} dicts, one per timestep
        Returns list of Celsius state dicts at each timestep.
        """
        states  = [initial_state_c]
        s_f     = initial_state_f
        ac_on   = {ac["id"]: 0 for ac in self.ac_units}
        for t, setpoints in enumerate(setpoint_schedule):
            hour = (start_hour + t * self.timestep_h) % 24
            next_c, s_f, ac_on = self.step(states[-1], s_f, setpoints,
                                            outdoor_forecast_c[t], hour, ac_on)
            states.append(next_c)
        return states
```

---

## Phase 5 — Weather forecast (`control/forecast.py`)

Use the **Open-Meteo API** (free, no API key required).

```python
import requests

def get_outdoor_forecast(lat: float, lon: float, hours: int = 24) -> list[float]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "forecast_days": 2,
        "timezone": "auto"
    }
    r = requests.get(url, params=params)
    data = r.json()
    temps = data["hourly"]["temperature_2m"]   # Celsius
    return temps[:hours]
```

Interpolate hourly values down to 10-minute resolution before passing to the simulator.

---

## Phase 6 — MPC optimizer (`control/mpc.py`)

### Decision variables

The MPC's only lever is the **integer setpoint written to each AC controller**:

```
Setpoint_j(t) ∈ {sp_min, sp_min+1, ..., sp_max}   (integer °F)
```

AC on/off states are not decision variables — they emerge from the simulator's switching logic given the setpoints and current sensor temperatures.

### Why not sigmoid relaxation

The sensor reports rounded °F, making the switching condition `AC_on = (T_sensor_F > Setpoint_F)` a discrete integer comparison. The sigmoid approximation `AC_on ≈ σ(k · (T_sensor - Setpoint))` is only valid when both quantities are continuous with fine resolution. With 1°F quantization on both sides, the sigmoid loses accuracy and can mislead the optimizer into believing small fractional setpoint changes have effect when they do not. Use MILP.

### MILP formulation

```
Decision variables:
  sp[t, j]    ∈ {sp_min, ..., sp_max}   integer setpoint per AC per timestep
  ac_on[t, j] ∈ {0, 1}                  binary, linked to sp via Big-M constraints

Minimize:
  Σ_t Σ_rooms  w_c · (T_room(t) - T_target)²
+ Σ_t Σ_j      w_e · power_j · ac_on[t, j]

Subject to:
  # Switching constraint (Big-M encoding of AC_on = T_sensor > sp):
  T_sensor_j(t) - sp[t, j] ≤  M · ac_on[t, j]
  T_sensor_j(t) - sp[t, j] ≥ -M · (1 - ac_on[t, j]) + ε

  # Thermal dynamics (linearized from simulator around current operating point):
  T(t+1) = A · T(t) + B · ac_on(t) + c(t)

  # Setpoint bounds:
  sp[t, j] ∈ [sp_min_F, sp_max_F]
```

Where `M = 30` (°F, a safe large constant) and `ε = 0.01`.

```python
import cvxpy as cp
import numpy as np

def solve_mpc(simulator, initial_state_c, initial_state_f, current_ac_on,
              outdoor_forecast_c, targets, config):
    H      = int(config["mpc"]["horizon_hours"] * 60 / config["mpc"]["timestep_minutes"])
    n_r    = len(simulator.rooms)
    n_a    = len(simulator.ac_units)
    sp_min = config["mpc"]["setpoint_range_f"]["min"]
    sp_max = config["mpc"]["setpoint_range_f"]["max"]
    M      = 30.0
    eps    = 0.01

    # Linearize simulator: T(t+1) ≈ A·T(t) + B·ac_on(t) + c(t)
    A, B, c_vec = simulator.linearize(initial_state_c, outdoor_forecast_c)

    T      = cp.Variable((H + 1, n_r))
    sp     = cp.Variable((H, n_a), integer=True)
    ac_on  = cp.Variable((H, n_a), boolean=True)

    cost        = 0
    constraints = [T[0] == state_to_vector(initial_state_c, simulator.rooms)]

    for t in range(H):
        T_target  = targets_vector(targets, simulator.rooms)
        T_sens    = sensor_temps_vector(initial_state_f, simulator, t)  # °F per AC

        cost += config["comfort_weight"] * cp.sum_squares(T[t + 1] - T_target)
        cost += config["energy_weight"]  * (ac_on[t] @ power_vector(simulator))

        # Thermal dynamics
        constraints += [T[t + 1] == A @ T[t] + B @ ac_on[t] + c_vec[t]]

        # Switching constraints
        for j in range(n_a):
            constraints += [
                T_sens[j] - sp[t, j] <= M * ac_on[t, j],
                T_sens[j] - sp[t, j] >= -M * (1 - ac_on[t, j]) + eps,
            ]

        constraints += [sp[t] >= sp_min, sp[t] <= sp_max]

    prob = cp.Problem(cp.Minimize(cost), constraints)
    prob.solve(solver=cp.GLPK_MI, verbose=False)

    if prob.status not in ("optimal", "optimal_inaccurate"):
        raise RuntimeError(f"MPC solve failed: {prob.status}")

    # Return first setpoint only — receding horizon replans next tick
    return {ac["id"]: int(sp.value[0, j]) for j, ac in enumerate(simulator.ac_units)}
```

### Hysteresis in the MILP

If hysteresis `h > 0` was detected, the switching constraint becomes asymmetric and depends on the previous AC state. Add:

```python
# ac_on[t-1] for t=0 is initialized from current observed AC state
for j in range(n_a):
    h = config["sensor_resolution"]["hysteresis_f"]
    prev = ac_on[t - 1, j] if t > 0 else float(current_ac_on[simulator.ac_units[j]["id"]])
    # Turns off only when T_sensor < sp - h (when previously on)
    # Turns on when T_sensor > sp (when previously off)
    constraints += [
        T_sens[j] - sp[t, j] + h * prev <= M * ac_on[t, j],
        T_sens[j] - sp[t, j] + h * prev >= -M * (1 - ac_on[t, j]) + eps,
    ]
```

---

## Phase 7 — Home Assistant bridge (`ha_bridge/controller.py`)

Use the **Home Assistant REST API** to read sensors and write setpoints.

```python
import requests
import os

HA_URL   = os.environ["HA_URL"]      # e.g. http://homeassistant.local:8123
HA_TOKEN = os.environ["HA_TOKEN"]    # long-lived access token

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}

def get_state(entity_id: str) -> dict:
    r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=HEADERS)
    return r.json()

def get_room_temperature_c(sensor_entity: str) -> float:
    return float(get_state(sensor_entity)["state"])

def get_ac_state(climate_entity: str) -> dict:
    s     = get_state(climate_entity)
    attrs = s["attributes"]
    return {
        "hvac_action":    attrs.get("hvac_action"),
        "setpoint_f":     int(attrs.get("temperature", 72)),
        "ac_sensor_temp_f": float(attrs.get("current_temperature", 72)),
        "ac_on":          int(attrs.get("hvac_action") == "cooling")
    }

def set_setpoint(climate_entity: str, setpoint_f: int):
    """Write an integer setpoint (°F) to an AC controller."""
    requests.post(
        f"{HA_URL}/api/services/climate/set_temperature",
        json={"entity_id": climate_entity, "temperature": setpoint_f},
        headers=HEADERS
    )

def apply_setpoints(optimal_setpoints: dict, config: dict):
    """optimal_setpoints: {ac_id: setpoint_f (integer)}"""
    for ac in config["ac_units"]:
        sp = optimal_setpoints[ac["id"]]
        set_setpoint(ac["climate_entity"], sp)
```

**Security**: store `HA_URL` and `HA_TOKEN` in a `.env` file excluded from version control. Load with `python-dotenv`.

---

## Phase 8 — Main scheduler loop (`scheduler.py`)

```python
import time
import logging
from datetime import datetime
from model.simulate import HouseSimulator
from control.mpc import solve_mpc
from control.forecast import get_outdoor_forecast
from ha_bridge.controller import get_room_temperature_c, get_ac_state, apply_setpoints
from config_loader import load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main_loop():
    config    = load_config()
    simulator = HouseSimulator.load("model/weights/")

    while True:
        try:
            now = datetime.now()
            logger.info(f"MPC tick at {now}")

            # 1. Read current room temperatures (Celsius, from independent sensors)
            current_state_c = {
                r["id"]: get_room_temperature_c(r["sensor_entity"])
                for r in config["rooms"]
            }

            # 2. Read AC states (setpoints, controller sensor temps in °F, on/off)
            current_ac = {
                ac["id"]: get_ac_state(ac["climate_entity"])
                for ac in config["ac_units"]
            }
            current_state_f = {
                ac["sensor_room"]: current_ac[ac["id"]]["ac_sensor_temp_f"]
                for ac in config["ac_units"]
            }
            current_ac_on = {ac_id: s["ac_on"] for ac_id, s in current_ac.items()}

            # 3. Get outdoor forecast (Celsius)
            forecast_c = get_outdoor_forecast(
                config["lat"], config["lon"],
                hours=config["mpc"]["horizon_hours"] + 1
            )

            # 4. Get current comfort targets
            targets = get_current_targets(config, now)

            # 5. Solve MPC — returns integer setpoints in °F
            optimal_setpoints = solve_mpc(
                simulator, current_state_c, current_state_f, current_ac_on,
                forecast_c, targets, config
            )

            # 6. Apply setpoints
            apply_setpoints(optimal_setpoints, config)

            logger.info(f"Room temps (C):    {current_state_c}")
            logger.info(f"AC sensor temps:   {current_state_f}")
            logger.info(f"Optimal setpoints: {optimal_setpoints}")

        except Exception as e:
            logger.error(f"MPC tick failed: {e}", exc_info=True)

        time.sleep(config["mpc"]["timestep_minutes"] * 60)

if __name__ == "__main__":
    main_loop()
```

---

## Phase 9 — Monthly recalibration (`recalibrate.py`)

Run on a cron job (e.g. first of each month). Refits room models on the last 60 days, validates on the last 2 weeks, saves new weights only if both validation passes improve.

```python
def recalibrate():
    # 1. Extract last 60 days from HA DB
    #    (room sensors + AC on/off from hvac_action + AC sensor temps)
    # 2. Preprocess to 10-min aligned DataFrame
    # 3. Fit all room models (same pipeline as Phase 3)
    # 4. Validate:
    #    Pass 1 — thermal only (observed AC_on): MAE < 1.0°C per room
    #    Pass 2 — full loop (simulated switching): MAE < 1.5°C per room
    # 5. If both pass: save new weights, archive old with timestamp
    # 6. If either fails: keep old weights, emit warning for manual review
    pass
```

---

## Implementation order for the agent

Execute phases strictly in this order. Do not proceed to the next phase until the current one passes its validation check.

| Step | Task | Validation |
|------|------|------------|
| 0 | Ask user to fill `house.yaml` (rooms, adjacency, AC zones with `sensor_room`) | Config loads without errors |
| 1 | Run sensor resolution and hysteresis checks on raw HA data | Findings recorded in `house.yaml`; solver choice confirmed |
| 2 | Implement `extract.py` — room sensors + AC attributes (`setpoint`, `ac_sensor_temp`, `hvac_action`) | DataFrames non-empty, cover >6 months |
| 3 | Implement `preprocess.py` | Aligned 10-min DataFrame; `AC_on_j` binary column present; no NaN in features |
| 4 | Fit DAG room models using `AC_on_j` as AC feature | Pass 1 MAE < 1.0°C per room on held-out 2 weeks |
| 5 | Build simulator with two-layer switching (`simulate.py`) | Pass 2 full-loop 2-hour rollout drift < 1.5°C vs ground truth |
| 6 | Implement forecast fetch (`forecast.py`) | Returns Celsius array, correctly interpolated to 10-min |
| 7 | Implement MILP MPC with integer setpoints (`mpc.py`) | Solves in < 10 seconds; setpoints are integers within valid range |
| 8 | Implement HA bridge (`controller.py`) | Reads all room sensors and AC sensor temps; writes setpoints successfully |
| 9 | Wire up `scheduler.py` | Runs 24h without crash; logs show integer setpoints applied each tick |
| 10 | Monitor for 1 week, tune `energy_weight` | Comfort maintained; duty cycle logs show energy reduction |
| 11 | Set up monthly `recalibrate.py` cron | Reruns cleanly; weights versioned with timestamp |

---

## Dependencies (`requirements.txt`)

```
pandas
numpy
scikit-learn
cvxpy
requests
pyyaml
python-dotenv
schedule
```

Install MILP solver: `apt install glpk-utils` (for GLPK_MI, sufficient for this problem size) or `pip install highspy` (HiGHS — faster, recommended if solve time exceeds 10 seconds).

---

## Key design decisions and rationale

| Decision | Rationale |
|----------|-----------|
| Setpoints as decision variables, not AC on/off | Setpoints are the only true control lever; on/off is a physical consequence of the switching rule |
| `AC_on_j` (not setpoint) as DAG regressor | Thermal effect on rooms is driven by whether the AC runs, not by the setpoint value |
| `ac_sensor_temp` separate from room sensor | The AC controller compares against its own sensor; this may differ from the independent room sensor |
| MILP over sigmoid relaxation | Rounded °F sensors make the threshold a coarse discrete comparison; sigmoid is inappropriate |
| Big-M constraints for switching | Cleanly encodes `AC_on = (T_sensor > Setpoint)` as linear constraints within the MILP |
| Integer setpoints in °F | Matches physical controller resolution; avoids commanding values the hardware will silently round |
| Joint optimization over all AC setpoints | Cross-room entanglement requires joint reasoning — optimizing each AC independently ignores the coupling |
| Two-pass validation (thermal-only then full-loop) | Separates thermal model error from switching model error for easier debugging |
| Hysteresis as explicit simulator state | If present, the switching threshold is asymmetric and depends on AC history, not just current temps |
| One regression per room | Interpretable, modular, easy to debug per-room failures |
| Ridge over Lasso | Rooms have true partial correlations; Ridge is more stable than sparse selection |
| 10-minute timestep | Matches HVAC thermal dynamics; shorter adds noise, longer loses resolution |
| 3-hour MPC horizon | Enough to pre-cool before hot afternoons; beyond that forecast error dominates |
| Receding horizon (apply only first setpoint) | Standard MPC robustness — replans every tick to correct model and switching prediction errors |
| Monthly recalibration | Seasonal drift is slow; monthly is frequent enough without overfitting to short-term anomalies |
