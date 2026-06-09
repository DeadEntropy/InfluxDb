"""
01_stale_data.py
────────────────
Detects stale sensor readings: consecutive identical values lasting
≥ 8 hours (48 × 10-min steps), which indicate a dead or dying battery
rather than a genuinely stable temperature.

Produces a report of all stale periods per sensor and a plot
highlighting them on the raw temperature timeline.

Outputs:
  thermal_control/preprocess/01_stale_data.png
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Config ────────────────────────────────────────────────────────────────
CSV_PATH      = "homeassistant_temp.csv"
SENTINEL_F    = -10.0
RESAMPLE      = "10min"
STALE_MIN_STEPS = 48          # 8 hours at 10-min resolution
OUTPUT_PNG    = "thermal_control/preprocess/01_stale_data.png"

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

# ── Load & clean ──────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
df["time"] = pd.to_datetime(df["time"], utc=True)
df = df.set_index("time")
df = df[[v for v in ROOM_COLUMNS.values()]]
df.columns = list(ROOM_COLUMNS.keys())
df[df <= SENTINEL_F] = np.nan
df = df.resample(RESAMPLE).mean()

# ── Stale detection ───────────────────────────────────────────────────────
def find_stale_periods(series, min_steps):
    """
    Returns a boolean Series (True = stale) and a list of
    (start, end, duration_hours) tuples for each stale run.
    """
    is_stale = pd.Series(False, index=series.index)
    periods  = []

    # Group consecutive runs of identical non-NaN values
    rounded  = series.round(2)           # treat tiny float drift as equal
    changed  = rounded.ne(rounded.shift())
    run_id   = changed.cumsum()

    for _, grp in series.groupby(run_id):
        if grp.isna().all():
            continue
        if len(grp) >= min_steps:
            is_stale.loc[grp.index] = True
            periods.append((
                grp.index[0],
                grp.index[-1],
                len(grp) * 10 / 60,      # hours
            ))

    return is_stale, periods

# ── Report ────────────────────────────────────────────────────────────────
all_stale  = pd.DataFrame(False, index=df.index, columns=df.columns)
total_rows = len(df)

print(f"Total rows: {total_rows:,}  ({df.index.min().date()} → {df.index.max().date()})")
print(f"Stale threshold: {STALE_MIN_STEPS} steps = {STALE_MIN_STEPS * 10 / 60:.0f} hours\n")
print(f"{'Sensor':<18}  {'Stale periods':>13}  {'Stale rows':>10}  {'% of data':>9}")
print("─" * 58)

for col in df.columns:
    is_stale, periods = find_stale_periods(df[col], STALE_MIN_STEPS)
    all_stale[col]    = is_stale
    pct = is_stale.sum() / total_rows * 100
    print(f"{col:<18}  {len(periods):>13}  {is_stale.sum():>10,}  {pct:>8.1f}%")
    for start, end, hours in periods:
        print(f"    {start.date()} {start.strftime('%H:%M')} → "
              f"{end.date()} {end.strftime('%H:%M')}  ({hours:.1f}h)")

print("─" * 58)
total_stale = all_stale.any(axis=1).sum()
print(f"{'Rows stale in any sensor':<18}  {'':>13}  {total_stale:>10,}  "
      f"{total_stale/total_rows*100:>8.1f}%")

# ── Plot ──────────────────────────────────────────────────────────────────
rooms    = [c for c in df.columns if c != "outdoor"]
n        = len(rooms)
fig, axes = plt.subplots(n, 1, figsize=(16, 2.2 * n), sharex=True)

for ax, col in zip(axes, rooms):
    ax.plot(df.index, df[col], linewidth=0.6, color="#4C72B0", label=col)
    # Shade stale periods in red
    stale_mask = all_stale[col]
    if stale_mask.any():
        ax.fill_between(df.index, df[col].min() - 1, df[col].max() + 1,
                        where=stale_mask, color="red", alpha=0.3, label="stale")
    ax.set_ylabel("°F", fontsize=8)
    ax.set_title(col, fontsize=9, loc="left", pad=2)
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(True, alpha=0.2)

axes[-1].set_xlabel("Time")
fig.suptitle(f"Room temperatures with stale periods highlighted (≥ {STALE_MIN_STEPS*10//60}h unchanged)",
             y=1.01)
plt.tight_layout()
plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"\nPlot saved → {OUTPUT_PNG}")
