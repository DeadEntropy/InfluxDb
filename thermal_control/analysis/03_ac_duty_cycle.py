"""
03_ac_duty_cycle.py
───────────────────
Plots the typical 24-hour AC duty cycle for all three units.
For each hour of the day, shows the average number of minutes
the AC was actively cooling, computed over the full overlap period
(May 2025 – Jun 2026).

Confirms expected usage patterns:
  - bedroom_ac  : peaks overnight / early morning (sleep hours)
  - living_ac   : peaks midday / afternoon (occupied living areas)
  - extension_ac: peaks during office hours

Output:
  thermal_control/analysis/03_ac_duty_cycle.png
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Config ────────────────────────────────────────────────────────────────
AC_STATES_CSV = "thermal_control/preprocess/ac_states_clean.csv"
TIMEZONE      = "America/New_York"
OUTPUT_PNG    = "thermal_control/analysis/03_ac_duty_cycle.png"

AC_UNITS = {
    "bedroom_ac"   : "#4C72B0",
    "living_ac"    : "#DD8452",
    "extension_ac" : "#55A868",
}

# ── Load ──────────────────────────────────────────────────────────────────
ac = pd.read_csv(AC_STATES_CSV, index_col="time", parse_dates=True)
ac = ac.dropna()   # drop pre-overlap rows (no AC data before May 2025)
ac.index = ac.index.tz_convert(TIMEZONE)
ac["hour"] = ac.index.hour

print(f"Rows: {len(ac):,}  ({ac.index.min().date()} → {ac.index.max().date()})")

# ── Plot ──────────────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)

for ax, (ac_id, color) in zip(axes, AC_UNITS.items()):
    col = f"ac_on_{ac_id}"
    # Each ac_on=1 row = 10 minutes cooling; avg fraction × 60 = avg min/hour
    by_hour = ac.groupby("hour")[col].mean() * 60

    ax.bar(by_hour.index, by_hour.values, color=color, alpha=0.85, width=0.8)
    ax.set_ylabel("Avg min / hour", fontsize=9)
    ax.set_title(ac_id, fontsize=10, loc="left")
    ax.set_ylim(0, 60)
    ax.axhline(30, color="black", linewidth=0.5, linestyle="--", alpha=0.4)
    ax.grid(True, axis="y", alpha=0.3)

    overall = ac[col].mean() * 60
    ax.text(0.98, 0.88, f"Overall avg: {overall:.1f} min/h",
            transform=ax.transAxes, ha="right", fontsize=9,
            bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.7))

axes[-1].set_xlabel("Hour of day (local time)")
axes[-1].set_xticks(range(24))
fig.suptitle("Typical 24h AC duty cycle — average cooling minutes per hour\n(May 2025 – Jun 2026)", y=1.01)
plt.tight_layout()
plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"Saved → {OUTPUT_PNG}")
