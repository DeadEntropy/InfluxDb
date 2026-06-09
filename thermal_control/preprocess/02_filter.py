"""
02_filter.py
────────────
Applies two filters to the raw room temperature CSV:

  1. Sentinel filter  — temps ≤ -10°F are Zigbee offline artefacts (-50°C)
  2. Stale filter     — runs of identical values lasting ≥ 8h are dead
                        battery readings

Both are replaced with NaN. Saves a clean CSV for downstream steps.

Output:
  thermal_control/preprocess/room_temps_clean.csv
"""

import pandas as pd
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────
CSV_PATH        = "homeassistant_temp.csv"
OUTPUT_CSV      = "thermal_control/preprocess/room_temps_clean.csv"
SENTINEL_F      = -10.0
RESAMPLE        = "10min"
STALE_MIN_STEPS = 48          # 8 hours at 10-min resolution

ROOM_COLUMNS = {
    "master_bedroom" : "Master Bedroom Temperature",
    "kids_bedroom"   : "Kids Bedroom Temperature",
    "anna_office"    : "Anna Office Temperature",
    "dining_room"    : "Dining Room Temperature",
    "kitchen"        : "Kitchen Temperature",
    "family_room"    : "Family Room Temperature",
    "tv_room"        : "Tv Room Temperature",
    "nicolas_office" : "Nicolas Office Temperature",
    "outdoor"        : "Pool Temp",
}

# ── Load & resample ───────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
df["time"] = pd.to_datetime(df["time"], utc=True)
df = df.set_index("time")
df = df[[v for v in ROOM_COLUMNS.values()]]
df.columns = list(ROOM_COLUMNS.keys())
df = df.resample(RESAMPLE).mean()

total_cells = df.size
print(f"Rows after resample: {len(df):,}  ({df.index.min().date()} → {df.index.max().date()})")

# ── Filter 1: sentinel ────────────────────────────────────────────────────
sentinel_mask = df <= SENTINEL_F
sentinel_count = sentinel_mask.sum().sum()
df[sentinel_mask] = np.nan
print(f"\nSentinel filter (≤ {SENTINEL_F}°F):  {sentinel_count:,} cells → NaN  "
      f"({sentinel_count/total_cells*100:.2f}%)")

# ── Filter 2: stale ───────────────────────────────────────────────────────
def null_stale(series, min_steps):
    out     = series.copy()
    rounded = series.round(2)
    changed = rounded.ne(rounded.shift())
    run_id  = changed.cumsum()
    for _, grp in series.groupby(run_id):
        if grp.isna().all():
            continue
        if len(grp) >= min_steps:
            out.loc[grp.index] = np.nan
    return out

stale_count = 0
for col in df.columns:
    before = df[col].isna().sum()
    df[col] = null_stale(df[col], STALE_MIN_STEPS)
    stale_count += df[col].isna().sum() - before

print(f"Stale filter  (≥ {STALE_MIN_STEPS*10//60}h unchanged):  {stale_count:,} cells → NaN  "
      f"({stale_count/total_cells*100:.2f}%)")

# ── Summary ───────────────────────────────────────────────────────────────
total_nan = df.isna().sum().sum()
print(f"\nTotal NaN after both filters: {total_nan:,} / {total_cells:,}  "
      f"({total_nan/total_cells*100:.1f}%)")
print(f"\nNaN per sensor:")
for col in df.columns:
    n = df[col].isna().sum()
    print(f"  {col:<18}  {n:>6,}  ({n/len(df)*100:.1f}%)")

# ── Save ──────────────────────────────────────────────────────────────────
df.to_csv(OUTPUT_CSV)
print(f"\nClean data saved → {OUTPUT_CSV}  ({len(df):,} rows × {len(df.columns)} sensors)")
