"""
02_sensor_spot_checks.py
────────────────────────
Visual spot checks for individual sensors flagged as potentially
unreliable. Plots pairwise temperature comparisons over a reference
period to identify distortions (shower spikes, cooking plateaus, etc.).

Outputs:
  thermal_control/analysis/02_master_bath_vs_bedroom.png
  thermal_control/analysis/02_kitchen_vs_dining.png
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CSV_PATH   = "homeassistant_temp.csv"
SENTINEL_F = -10.0
RESAMPLE   = "10min"

# Reference window: last 10 days of June 2025
# (avoids master bedroom sensor outage in later data)
END   = pd.Timestamp("2025-06-30", tz="UTC")
START = END - pd.Timedelta(days=10)

# ── Load & clean ──────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
df["time"] = pd.to_datetime(df["time"], utc=True)
df = df.set_index("time")
df[df <= SENTINEL_F] = np.nan
df = df.resample(RESAMPLE).mean()
df = df[START:END]

def save_comparison(col_a, col_b, label_a, label_b, title, outpath):
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(df.index, df[col_a], label=label_a, linewidth=1)
    ax.plot(df.index, df[col_b], label=label_b, linewidth=1, alpha=0.85)
    ax.set_title(f"{title}\nLast 10 days of June 2025 — 10-min resolution")
    ax.set_ylabel("Temperature (°F)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(outpath, dpi=150)
    plt.close()
    print(f"Saved → {outpath}")

# ── Plot 1: master bath vs master bedroom ─────────────────────────────────
# Finding: master bath shows clear spikes from shower activity even at
# 10-min resolution. Dropped from thermal model.
save_comparison(
    "Master Bath", "Master Bedroom Temperature",
    "Master Bath", "Master Bedroom",
    "Master Bath vs Master Bedroom — shower spikes visible",
    "thermal_control/analysis/02_master_bath_vs_bedroom.png",
)

# ── Plot 2: kitchen vs dining room ────────────────────────────────────────
# Finding: kitchen sensor (mounted under wall shelf above countertop)
# shows plateau artefacts from trapped heat (cooking, rice cooker).
# Retained in model but noted as lower-fidelity signal.
save_comparison(
    "Kitchen Temperature", "Dining Room Temperature",
    "Kitchen", "Dining Room",
    "Kitchen vs Dining Room — plateau artefacts from counter heat",
    "thermal_control/analysis/02_kitchen_vs_dining.png",
)
