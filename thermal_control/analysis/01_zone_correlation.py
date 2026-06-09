"""
01_zone_correlation.py
─────────────────────
Validates AC zone assignments by computing pairwise correlations of
10-minute temperature *changes* (first differences) across all observable
rooms. Using changes rather than levels removes the shared outdoor
temperature trend that otherwise inflates cross-zone correlations.

Outputs:
  thermal_control/analysis/01_zone_correlation.png
"""

import pandas as pd
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.patches import Patch

# ── Config ────────────────────────────────────────────────────────────────
CSV_PATH   = "homeassistant_temp.csv"
SENTINEL_F = -10.0
RESAMPLE   = "10min"
START_DATE = "2025-06-01"
OUTPUT_PNG = "thermal_control/analysis/01_zone_correlation.png"

# master_bath excluded (shower distortion); laundry_room excluded (dryer)
ROOM_COLUMNS = {
    "master_bedroom" : "Master Bedroom Temperature",
    "kids_bedroom"   : "Kids Bedroom Temperature",
    "anna_office"    : "Anna Office Temperature",
    "dining_room"    : "Dining Room Temperature",
    "kitchen"        : "Kitchen Temperature",
    "family_room"    : "Family Room Temperature",
    "tv_room"        : "Tv Room Temperature",
    "nicolas_office" : "Nicolas Office Temperature",
}

EXPECTED_ZONES = {
    "bedroom_ac"   : ["master_bedroom", "kids_bedroom", "anna_office"],
    "living_ac"    : ["dining_room", "kitchen", "family_room"],
    "extension_ac" : ["tv_room", "nicolas_office"],
}

ZONE_COLORS = {
    "bedroom_ac"   : "#4C72B0",
    "living_ac"    : "#DD8452",
    "extension_ac" : "#55A868",
}

# ── Load & clean ──────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
df["time"] = pd.to_datetime(df["time"], utc=True)
df = df.set_index("time")
df = df[[v for v in ROOM_COLUMNS.values()]]
df.columns = list(ROOM_COLUMNS.keys())

df[df <= SENTINEL_F] = np.nan
df = df.resample(RESAMPLE).mean()
df = df[START_DATE:]
df = df.diff().dropna()
df_clean = df.dropna()

print(f"Rows: {len(df_clean):,}  ({df_clean.index.min().date()} → {df_clean.index.max().date()})")

# ── Correlation matrix ────────────────────────────────────────────────────
corr      = df_clean.corr()
zone_order = [r for z in EXPECTED_ZONES.values() for r in z]
corr_sorted = corr.loc[zone_order, zone_order]

print("\n── Correlation matrix (Δ 10-min, grouped by AC zone) ─────────────")
print(corr_sorted.round(2).to_string())

print("\n── Within-zone vs between-zone avg correlations ───────────────────")
zones = list(EXPECTED_ZONES.keys())
for z in zones:
    rooms = EXPECTED_ZONES[z]
    vals  = corr.loc[rooms, rooms].values
    mask  = np.triu(np.ones_like(vals, dtype=bool), k=1)
    print(f"  {z:15s}  within = {vals[mask].mean():.3f}")
print()
for i, z1 in enumerate(zones):
    for z2 in zones[i+1:]:
        avg = corr.loc[EXPECTED_ZONES[z1], EXPECTED_ZONES[z2]].values.mean()
        print(f"  {z1} ↔ {z2:15s}  between = {avg:.3f}")

print("\n── Hierarchical clustering ────────────────────────────────────────")
dist    = (1 - corr).clip(lower=0)
lnk     = linkage(squareform(dist.values), method="average")
labels  = corr.columns.tolist()
for k in [2, 3, 4]:
    assignments = fcluster(lnk, k, criterion="maxclust")
    clusters    = {}
    for room, c in zip(labels, assignments):
        clusters.setdefault(c, []).append(room)
    print(f"  k={k}: " + " | ".join(", ".join(v) for v in sorted(clusters.values())))

# ── Clustermap ────────────────────────────────────────────────────────────
room_to_zone = {r: z for z, rooms in EXPECTED_ZONES.items() for r in rooms}
row_colors   = pd.Series(
    [ZONE_COLORS[room_to_zone[r]] for r in zone_order],
    index=zone_order, name="Expected zone"
)

fig = sns.clustermap(
    corr_sorted, method="average", metric="euclidean",
    cmap="RdYlGn", vmin=-1, vmax=1,
    annot=True, fmt=".2f", linewidths=0.5, figsize=(10, 8),
    row_colors=row_colors, col_colors=row_colors,
)
fig.fig.suptitle("Room Temperature Change Correlations (Δ 10-min)\nColoured by expected AC zone", y=1.02)
fig.ax_heatmap.legend(
    handles=[Patch(color=c, label=z) for z, c in ZONE_COLORS.items()],
    loc="upper left", bbox_to_anchor=(1.18, 1.1),
    title="Expected zone", frameon=True,
)
plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"\nSaved → {OUTPUT_PNG}")
