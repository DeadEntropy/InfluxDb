# model/

Thermal DAG calibration and forward simulator.

## Structural equation

For each room X, the model predicts the temperature at the next
10-minute timestep:

```
T_X(t+1) = α  · T_X(t)                           own thermal inertia
          + Σ_Y β_XY · T_Y(t)                     conduction from adjacent rooms
          + Σ_j γ_Xj · AC_on_j(t)                AC cooling (binary on/off)
          + δ  · T_outdoor(t)                      exterior coupling
          + ε                                      intercept
```

One `RidgeCV` model per room. Ridge is preferred over Lasso because
adjacent room temperatures are genuinely correlated — Ridge keeps all
features with stable shrinkage rather than zeroing some arbitrarily.

## Scripts

### `fit.py`

Fits one `RidgeCV` model per room from `preprocess/train.csv`.
Features per room are derived from `config/house.yaml` (adjacency,
AC coverage). Evaluates Pass 1 validation MAE on `preprocess/val.csv`
using observed AC on/off states.

**Pass 1 results (observed AC_on, val set):**

| Room | Features | λ | Val MAE (°F) | Val MAE (°C) |
|---|---|---|---|---|
| anna_office | 5 | 0.01 | 0.106 | 0.059 |
| dining_room | 4 | 0.01 | 0.103 | 0.057 |
| family_room | 6 | 100 | 0.102 | 0.057 |
| kids_bedroom | 3 | 0.01 | 0.120 | 0.067 |
| kitchen | 6 | 0.01 | 0.064 | 0.036 |
| master_bedroom | 3 | 0.01 | 0.139 | 0.077 |
| nicolas_office | 3 | 0.01 | 0.038 | 0.021 |
| tv_room | 4 | 0.01 | 0.077 | 0.043 |

All rooms pass the < 1.0°C target by a wide margin.

### `simulate.py`

`HouseSimulator` class. Core methods:

- `step(state, setpoints, T_outdoor)` — advances all rooms one 10-min
  timestep. Derives `AC_on` from controller sensor temps vs setpoints
  (Layer 2), then runs each room model (Layer 3).
- `rollout(initial_state, setpoint_schedule, outdoor_series)` — chains
  `step` calls for multi-step forward simulation.

The `__main__` block runs Pass 2 validation on `preprocess/val.csv`:

- **Pass 2a — one-step-ahead**: resets room temps to observed each step
  but derives AC_on from simulated switching. Isolates switching error.
- **Pass 2b — 2-hour rollout windows**: accumulates state over 12-step
  windows, averages MAE. Target < 1.5°C. All rooms pass.

Outputs: `thermal_control/model/simulate_validation.png`

### `compare_formulations.py`

Fits and compares two model formulations for each room:

- **Model A** — absolute temperatures (current production approach):
  features are `T_X_lag1`, neighbour `T_Y_lag1`, `ac_on_j`,
  `T_outdoor_lag1`.
- **Model B** — temperature differences (Newton's law of cooling):
  features are `T_X_lag1`, `dT_outdoor`, `dT_neighbour`, `ac_on_j`.
  Coefficients have direct physical meaning (thermal conductance,
  cooling power per step).

Both formulations give identical MAE in practice — the linear feature
spaces are equivalent. Model A is kept for production; Model B weights
are saved to `weights_diff/` for reference.

Outputs: `thermal_control/model/compare_formulations.png`,
`thermal_control/model/weights_diff/`

## weights/

Fitted models saved as joblib files, one per room.
`feature_lists.json` records which features each model uses.
`metrics.json` records train and validation MAE.
