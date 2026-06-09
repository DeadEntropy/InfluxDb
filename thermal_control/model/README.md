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

### `simulate.py` *(not yet created)*

Forward simulator: steps the full house state through time using the
fitted room models (Layer 3) and the AC switching logic (Layer 2).
Used for Pass 2 validation and as the forward model inside the MPC.

## weights/

Fitted models saved as joblib files, one per room.
`feature_lists.json` records which features each model uses.
`metrics.json` records train and validation MAE.
