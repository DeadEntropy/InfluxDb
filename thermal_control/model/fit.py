"""
fit.py
──────
Fits one RidgeCV regression per observable room. Each model predicts
the next room temperature T_X(t) from:

  T_X_lag1            own temperature at previous timestep
  T_Y_lag1            each adjacent room's temperature at t-1
                      (adjacency from config/house.yaml)
  ac_on_<j>           binary on/off for each AC unit covering room X
                      (current state at t, which drove change from t-1→t)
  T_outdoor_lag1      outdoor temperature at previous timestep

RidgeCV automatically selects the regularisation strength λ via 5-fold
cross-validation from the candidate set [0.01, 0.1, 1, 10, 100].

Validation — Pass 1 (thermal model only):
  Rollout on held-out 2 weeks using *observed* ac_on values.
  Target: MAE < 1.0°C (1.8°F) per room.

Models are saved to model/weights/ via joblib.

Outputs:
  thermal_control/model/weights/<room_id>.joblib   fitted model per room
  thermal_control/model/weights/feature_lists.json  features per room
  thermal_control/model/weights/metrics.json        train/val MAE per room
"""

import json
import sklearn
import yaml
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV
from sklearn.metrics import mean_absolute_error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────
HOUSE_YAML  = "thermal_control/config/house.yaml"
TRAIN_CSV   = "thermal_control/preprocess/train.csv"
VAL_CSV     = "thermal_control/preprocess/val.csv"
WEIGHTS_DIR = Path("thermal_control/model/weights")
WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

RIDGE_ALPHAS = [0.01, 0.1, 1.0, 10.0, 100.0]

# ── Load house config ─────────────────────────────────────────────────────
with open(HOUSE_YAML) as f:
    house = yaml.safe_load(f)

# Build adjacency map  {room_id: [neighbor_ids]}  (only rooms with sensors)
has_sensor = {
    r["id"] for r in house["rooms"]
    if r.get("sensor_entity") and r.get("csv_column")
}

adjacency = {}
for pair in house["adjacency"]:
    a, b = pair
    adjacency.setdefault(a, []).append(b)
    adjacency.setdefault(b, []).append(a)

# Build AC coverage map  {room_id: [ac_ids]}
ac_coverage = {}
for ac in house["ac_units"]:
    for room in ac["covers"]:
        ac_coverage.setdefault(room, []).append(ac["id"])

# Rooms to model: have a sensor AND appear in ac_units covers
rooms_to_model = sorted(
    r["id"] for r in house["rooms"]
    if r.get("csv_column") and r["id"] in ac_coverage
)
print(f"Rooms to model: {rooms_to_model}\n")

# ── Load data ─────────────────────────────────────────────────────────────
train = pd.read_csv(TRAIN_CSV, index_col="time", parse_dates=True)
val   = pd.read_csv(VAL_CSV,   index_col="time", parse_dates=True)

# ── Fit one model per room ────────────────────────────────────────────────
metrics      = {}
feature_lists = {}

print(f"{'Room':<18}  {'Features':>8}  {'λ':>7}  {'Train MAE':>10}  {'Val MAE':>10}  {'Val MAE':>10}")
print(f"{'':18}  {'':8}  {'':7}  {'(°F)':>10}  {'(°F)':>10}  {'(°C)':>10}")
print("─" * 80)

for room_id in rooms_to_model:
    # ── Build feature list ─────────────────────────────────────────────
    features = [f"T_{room_id}_lag1"]

    for neighbor in adjacency.get(room_id, []):
        if neighbor in has_sensor:
            features.append(f"T_{neighbor}_lag1")

    for ac_id in ac_coverage.get(room_id, []):
        features.append(f"ac_on_{ac_id}")

    features.append("T_outdoor_lag1")

    # ── Extract X, y ───────────────────────────────────────────────────
    X_train = train[features]
    y_train = train[f"T_{room_id}"]
    X_val   = val[features]
    y_val   = val[f"T_{room_id}"]

    # ── Fit RidgeCV ────────────────────────────────────────────────────
    model = RidgeCV(alphas=RIDGE_ALPHAS, cv=5)
    model.fit(X_train, y_train)

    # ── Evaluate ───────────────────────────────────────────────────────
    mae_train_f = mean_absolute_error(y_train, model.predict(X_train))
    mae_val_f   = mean_absolute_error(y_val,   model.predict(X_val))
    mae_val_c   = mae_val_f / 1.8   # °F → °C

    print(f"{room_id:<18}  {len(features):>8}  {model.alpha_:>7.2f}  "
          f"{mae_train_f:>10.3f}  {mae_val_f:>10.3f}  {mae_val_c:>10.3f}")

    # ── Save model ─────────────────────────────────────────────────────
    joblib.dump(model, WEIGHTS_DIR / f"{room_id}.joblib")
    feature_lists[room_id] = features
    metrics[room_id] = {
        "mae_train_f": round(mae_train_f, 4),
        "mae_val_f":   round(mae_val_f,   4),
        "mae_val_c":   round(mae_val_c,   4),
        "alpha":       model.alpha_,
        "n_features":  len(features),
    }

print("─" * 80)

# ── Coefficients ──────────────────────────────────────────────────────────
print("\n── Fitted coefficients per room ────────────────────────────────────")
for room_id in rooms_to_model:
    model    = joblib.load(WEIGHTS_DIR / f"{room_id}.joblib")
    features = feature_lists[room_id]
    print(f"\n{room_id}  (intercept={model.intercept_:.3f})")
    for feat, coef in sorted(zip(features, model.coef_), key=lambda x: -abs(x[1])):
        print(f"  {feat:<35}  {coef:+.4f}")

# ── Save metadata ─────────────────────────────────────────────────────────
with open(WEIGHTS_DIR / "feature_lists.json", "w") as f:
    json.dump(feature_lists, f, indent=2)
with open(WEIGHTS_DIR / "metrics.json", "w") as f:
    json.dump({"sklearn_version": sklearn.__version__, **metrics}, f, indent=2)

print(f"\nModels saved → {WEIGHTS_DIR}/")
