"""
04_merge.py
───────────
Merges the cleaned room temperature and AC states DataFrames onto a
shared 10-minute index, creates lagged features, and splits into
train and validation sets.

Feature set per room:
  T_<room>           current room temperature (°F)
  T_<room>_lag1      room temperature at previous timestep

Feature set per AC unit:
  ac_on_<ac>         binary 1=cooling, 0=idle (from hvac_action)
  ac_sensor_<ac>     temperature read by AC controller sensor (°F)
  ac_sensor_<ac>_lag1

Shared:
  T_outdoor          outdoor temperature (°F)
  T_outdoor_lag1

Excluded by design:
  hour_sin / hour_cos — outdoor temperature already captures the daily
  thermal cycle; time-of-day features would be largely redundant.

Lag propagation:
  If row t−1 was NaN (filtered as sentinel or stale), the lag feature
  at row t is also set to NaN and the row is dropped. This prevents
  the model from learning across data gaps.

Train / validation split:
  Validation = last 2 weeks of the dataset (held out).
  Train       = everything before that.

Outputs:
  thermal_control/preprocess/train.csv
  thermal_control/preprocess/val.csv
"""

import pandas as pd
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────
ROOM_CSV   = "thermal_control/preprocess/room_temps_clean.csv"
AC_CSV     = "thermal_control/preprocess/ac_states_clean.csv"
TRAIN_CSV  = "thermal_control/preprocess/train.csv"
VAL_CSV    = "thermal_control/preprocess/val.csv"
VAL_WEEKS  = 2

ROOMS = [
    "master_bedroom", "kids_bedroom", "anna_office",
    "dining_room", "kitchen", "family_room",
    "tv_room", "nicolas_office",
]
AC_UNITS = ["bedroom_ac", "living_ac", "extension_ac"]

# ── Load ──────────────────────────────────────────────────────────────────
rooms = pd.read_csv(ROOM_CSV, index_col="time", parse_dates=True)
ac    = pd.read_csv(AC_CSV,   index_col="time", parse_dates=True)

# Restrict to overlap period (AC data starts May 2025)
ac = ac.dropna()
start = ac.index.min()
rooms = rooms[rooms.index >= start]

print(f"Overlap period: {start.date()} → {rooms.index.max().date()}")
print(f"Rows in window: {len(rooms):,}")

# ── Merge ─────────────────────────────────────────────────────────────────
df = rooms.join(ac, how="inner")
print(f"Rows after inner join: {len(df):,}")

# ── Lagged features ───────────────────────────────────────────────────────
lag_cols = (
    [f"T_{r}" for r in ROOMS]
    + [f"ac_sensor_{a}" for a in AC_UNITS]
    + ["outdoor"]
)

# Rename outdoor → T_outdoor for consistency
df = df.rename(columns={"outdoor": "T_outdoor"})
lag_cols = [c if c != "outdoor" else "T_outdoor" for c in lag_cols]

# Also rename room columns to T_<room> prefix
df = df.rename(columns={r: f"T_{r}" for r in ROOMS})

for col in lag_cols:
    if col in df.columns:
        df[f"{col}_lag1"] = df[col].shift(1)
        # Null out lag where the source was NaN (gap in data)
        df.loc[df[col].isna(), f"{col}_lag1"] = np.nan

# ── Drop rows with any NaN ────────────────────────────────────────────────
before = len(df)
df = df.dropna()
print(f"Rows after dropping NaN (gaps + lag boundaries): {len(df):,}  "
      f"(dropped {before - len(df):,})")

# ── Column order ──────────────────────────────────────────────────────────
room_cols = [f"T_{r}" for r in ROOMS]
lag_room  = [f"T_{r}_lag1" for r in ROOMS]
ac_on     = [f"ac_on_{a}" for a in AC_UNITS]
ac_sensor = [f"ac_sensor_{a}" for a in AC_UNITS]
ac_sens_l = [f"ac_sensor_{a}_lag1" for a in AC_UNITS]
setpoints = [f"setpoint_{a}" for a in AC_UNITS]
outdoor   = ["T_outdoor", "T_outdoor_lag1"]

ordered_cols = room_cols + lag_room + ac_on + ac_sensor + ac_sens_l + setpoints + outdoor
df = df[ordered_cols]

print(f"\nFinal columns ({len(df.columns)}):")
for c in df.columns:
    print(f"  {c}")

# ── Train / validation split ──────────────────────────────────────────────
val_start = df.index.max() - pd.Timedelta(weeks=VAL_WEEKS)
train = df[df.index < val_start]
val   = df[df.index >= val_start]

print(f"\nTrain: {len(train):,} rows  ({train.index.min().date()} → {train.index.max().date()})")
print(f"Val:   {len(val):,} rows  ({val.index.min().date()} → {val.index.max().date()})")

# ── Save ──────────────────────────────────────────────────────────────────
train.to_csv(TRAIN_CSV)
val.to_csv(VAL_CSV)
print(f"\nSaved → {TRAIN_CSV}")
print(f"Saved → {VAL_CSV}")
