"""
compare_formulations.py
───────────────────────
Fits and compares two thermal model formulations for each room:

  Model A — Absolute temperatures (current approach)
    Features: T_X_lag1, T_Y_lag1 (neighbours), ac_on_j, T_outdoor_lag1

  Model B — Temperature differences (physics-based)
    Features: T_X_lag1,
              (T_outdoor_lag1 − T_X_lag1),
              (T_Y_lag1 − T_X_lag1) for each adjacent neighbour,
              ac_on_j
    Coefficients have direct physical meaning: thermal conductance
    (heat flows from hot to cold proportional to the difference).
    AC coefficient is the cooling power in °F per 10-min step.

Model B enforces Newton's law of cooling: the outdoor and neighbour
terms become zero when temperatures are equal, and flip sign when
the outside is cooler than the room — which is physically correct.

Outputs:
  thermal_control/model/compare_formulations.png
  thermal_control/model/weights_diff/<room>.joblib   (Model B weights)
  thermal_control/model/weights_diff/feature_lists.json
  thermal_control/model/weights_diff/metrics.json
"""

import json
import yaml
import joblib
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.linear_model import RidgeCV
from sklearn.metrics import mean_absolute_error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────
HOUSE_YAML   = "thermal_control/config/house.yaml"
TRAIN_CSV    = "thermal_control/preprocess/train.csv"
VAL_CSV      = "thermal_control/preprocess/val.csv"
WEIGHTS_A    = Path("thermal_control/model/weights")
WEIGHTS_B    = Path("thermal_control/model/weights_diff")
WEIGHTS_B.mkdir(parents=True, exist_ok=True)
OUTPUT_PNG   = "thermal_control/model/compare_formulations.png"

RIDGE_ALPHAS = [0.01, 0.1, 1.0, 10.0, 100.0]

# ── Load house config ─────────────────────────────────────────────────────
with open(HOUSE_YAML) as f:
    house = yaml.safe_load(f)

has_sensor = {
    r["id"] for r in house["rooms"]
    if r.get("csv_column") and r["csv_column"] != "null"
}
adjacency = {}
for pair in house["adjacency"]:
    a, b = pair
    adjacency.setdefault(a, []).append(b)
    adjacency.setdefault(b, []).append(a)

ac_coverage = {}
for ac in house["ac_units"]:
    for room in ac["covers"]:
        ac_coverage.setdefault(room, []).append(ac["id"])

rooms_to_model = sorted(
    r["id"] for r in house["rooms"]
    if r.get("csv_column") and r["id"] in ac_coverage
)

# ── Load data ─────────────────────────────────────────────────────────────
train = pd.read_csv(TRAIN_CSV, index_col="time", parse_dates=True)
val   = pd.read_csv(VAL_CSV,   index_col="time", parse_dates=True)

# ── Fit both formulations ─────────────────────────────────────────────────
results      = {}   # room → {A: {...}, B: {...}}
feature_lists_b = {}

for room_id in rooms_to_model:
    own_lag      = f"T_{room_id}_lag1"
    neighbor_ids = [n for n in adjacency.get(room_id, []) if n in has_sensor]
    ac_ids       = ac_coverage.get(room_id, [])

    # ── Model A: absolute temperatures ────────────────────────────────
    feats_a = (
        [own_lag]
        + [f"T_{n}_lag1" for n in neighbor_ids]
        + [f"ac_on_{a}" for a in ac_ids]
        + ["T_outdoor_lag1"]
    )
    model_a = RidgeCV(alphas=RIDGE_ALPHAS, cv=5)
    model_a.fit(train[feats_a], train[f"T_{room_id}"])
    mae_a_train = mean_absolute_error(train[f"T_{room_id}"], model_a.predict(train[feats_a]))
    mae_a_val   = mean_absolute_error(val[f"T_{room_id}"],   model_a.predict(val[feats_a]))

    # ── Model B: temperature differences ──────────────────────────────
    # Build difference features in a temporary DataFrame
    def make_diff_features(df, room_id, own_lag, neighbor_ids, ac_ids):
        X = pd.DataFrame(index=df.index)
        X[own_lag] = df[own_lag]
        X["dT_outdoor"] = df["T_outdoor_lag1"] - df[own_lag]
        for n in neighbor_ids:
            X[f"dT_{n}"] = df[f"T_{n}_lag1"] - df[own_lag]
        for a in ac_ids:
            X[f"ac_on_{a}"] = df[f"ac_on_{a}"]
        return X

    feats_b_names = (
        [own_lag, "dT_outdoor"]
        + [f"dT_{n}" for n in neighbor_ids]
        + [f"ac_on_{a}" for a in ac_ids]
    )

    X_train_b = make_diff_features(train, room_id, own_lag, neighbor_ids, ac_ids)
    X_val_b   = make_diff_features(val,   room_id, own_lag, neighbor_ids, ac_ids)

    model_b = RidgeCV(alphas=RIDGE_ALPHAS, cv=5)
    model_b.fit(X_train_b, train[f"T_{room_id}"])
    mae_b_train = mean_absolute_error(train[f"T_{room_id}"], model_b.predict(X_train_b))
    mae_b_val   = mean_absolute_error(val[f"T_{room_id}"],   model_b.predict(X_val_b))

    # ── Save Model B ───────────────────────────────────────────────────
    joblib.dump(model_b, WEIGHTS_B / f"{room_id}.joblib")
    feature_lists_b[room_id] = feats_b_names

    results[room_id] = {
        "A": {"mae_train": mae_a_train, "mae_val": mae_a_val,
              "alpha": model_a.alpha_, "coef": dict(zip(feats_a, model_a.coef_)),
              "intercept": model_a.intercept_},
        "B": {"mae_train": mae_b_train, "mae_val": mae_b_val,
              "alpha": model_b.alpha_, "coef": dict(zip(feats_b_names, model_b.coef_)),
              "intercept": model_b.intercept_},
    }

# ── Save Model B metadata ─────────────────────────────────────────────────
with open(WEIGHTS_B / "feature_lists.json", "w") as f:
    json.dump(feature_lists_b, f, indent=2)
metrics_b = {
    r: {"mae_train_f": round(v["B"]["mae_train"], 4),
        "mae_val_f":   round(v["B"]["mae_val"],   4),
        "mae_val_c":   round(v["B"]["mae_val"] / 1.8, 4),
        "alpha":       v["B"]["alpha"]}
    for r, v in results.items()
}
with open(WEIGHTS_B / "metrics.json", "w") as f:
    json.dump(metrics_b, f, indent=2)

# ── Print comparison table ────────────────────────────────────────────────
print(f"\n{'Room':<18}  {'A val MAE':>10}  {'B val MAE':>10}  {'Δ (B-A)':>10}  {'Winner':>8}")
print(f"{'':18}  {'(°C)':>10}  {'(°C)':>10}  {'(°C)':>10}")
print("─" * 68)
for room_id in rooms_to_model:
    r   = results[room_id]
    a_c = r["A"]["mae_val"] / 1.8
    b_c = r["B"]["mae_val"] / 1.8
    d   = b_c - a_c
    win = "B ✓" if b_c < a_c else ("A ✓" if a_c < b_c else "tie")
    print(f"{room_id:<18}  {a_c:>10.4f}  {b_c:>10.4f}  {d:>+10.4f}  {win:>8}")
print("─" * 68)

# ── Print Model B coefficients ────────────────────────────────────────────
print("\n── Model B coefficients (temperature differences) ──────────────────")
for room_id in rooms_to_model:
    r = results[room_id]
    print(f"\n{room_id}  (intercept={r['B']['intercept']:.4f}, λ={r['B']['alpha']})")
    for feat, coef in sorted(r["B"]["coef"].items(), key=lambda x: -abs(x[1])):
        bar  = "█" * int(abs(coef) * 40)
        sign = "+" if coef > 0 else "-"
        print(f"  {feat:<35}  {coef:+.4f}  {sign}{bar}")

# ── Plot: MAE comparison per room ────────────────────────────────────────
n   = len(rooms_to_model)
fig, axes = plt.subplots(n, 1, figsize=(14, 2.8 * n), sharex=False)

for ax, room_id in zip(axes, rooms_to_model):
    r      = results[room_id]
    y_true = val[f"T_{room_id}"]

    # Model A predictions
    feats_a = list(r["A"]["coef"].keys())
    model_a = joblib.load(WEIGHTS_A / f"{room_id}.joblib")
    pred_a  = model_a.predict(val[feats_a])

    # Model B predictions
    model_b  = joblib.load(WEIGHTS_B / f"{room_id}.joblib")
    X_val_b  = make_diff_features(
        val, room_id, f"T_{room_id}_lag1",
        [n for n in adjacency.get(room_id, []) if n in has_sensor],
        ac_coverage.get(room_id, [])
    )
    pred_b = model_b.predict(X_val_b)

    ax.plot(val.index, y_true, color="black",   linewidth=1,   label="Actual", alpha=0.8)
    ax.plot(val.index, pred_a, color="#4C72B0", linewidth=0.8, label=f"Model A  MAE={r['A']['mae_val']/1.8:.3f}°C", linestyle="--", alpha=0.9)
    ax.plot(val.index, pred_b, color="#DD8452", linewidth=0.8, label=f"Model B  MAE={r['B']['mae_val']/1.8:.3f}°C", linestyle=":",  alpha=0.9)
    ax.set_title(room_id.replace("_", " "), fontsize=9, loc="left")
    ax.set_ylabel("°F", fontsize=8)
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(True, alpha=0.2)

fig.suptitle("Model A (absolute) vs Model B (temperature differences) — validation set", y=1.005)
plt.tight_layout()
plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"\nPlot saved → {OUTPUT_PNG}")
