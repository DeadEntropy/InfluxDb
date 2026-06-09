"""
Sanity check: do room temperature correlations cluster into the 3 AC zones?
Expected groups per house.yaml:
  bedroom_ac  : master_bedroom, kids_bedroom, anna_office, master_bath
  living_ac   : dining_room, kitchen, family_room
  extension_ac: tv_room, nicolas_office
"""

import pandas as pd
import numpy as np
from scipy.cluster import hierarchy
from scipy.spatial.distance import squareform
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

# ── Config ────────────────────────────────────────────────────────────────
CSV_PATH      = "homeassistant_temp.csv"
SENTINEL_F    = -10.0   # Zigbee dropout sentinel
RESAMPLE      = "1D"
OUTPUT_PNG    = "thermal_control/sanity_check_correlation.png"

ROOM_COLUMNS = {
    "master_bedroom" : "Master Bedroom Temperature",
    "kids_bedroom"   : "Kids Bedroom Temperature",
    "anna_office"    : "Anna Office Temperature",
    "master_bath"    : "Master Bath",
    "dining_room"    : "Dining Room Temperature",
    "kitchen"        : "Kitchen Temperature",
    "family_room"    : "Family Room Temperature",
    "tv_room"        : "Tv Room Temperature",
    "nicolas_office" : "Nicolas Office Temperature",
}

EXPECTED_ZONES = {
    "bedroom_ac"   : ["master_bedroom", "kids_bedroom", "anna_office", "master_bath"],
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

# Drop sentinel values then resample
df[df <= SENTINEL_F] = np.nan
df = df.resample(RESAMPLE).median()

# Restrict to period where all sensors overlap with AC states
df = df["2025-06-01":]

# Drop timestamps where any room is NaN
df_clean = df.dropna()
print(f"Clean rows after sentinel filter & resample: {len(df_clean):,}  "
      f"({df_clean.index.min().date()} → {df_clean.index.max().date()})")

# ── Correlation matrix ────────────────────────────────────────────────────
corr = df_clean.corr()

# Print sorted correlation matrix, grouped by expected zone
zone_order = [r for z in EXPECTED_ZONES.values() for r in z]
corr_sorted = corr.loc[zone_order, zone_order]

print("\n── Correlation matrix (grouped by expected AC zone) ──────────────")
print(corr_sorted.round(2).to_string())

# ── Within-zone vs between-zone average correlations ─────────────────────
print("\n── Average within-zone vs between-zone correlations ──────────────")
zones = list(EXPECTED_ZONES.keys())
for z in zones:
    rooms = EXPECTED_ZONES[z]
    within = corr.loc[rooms, rooms].values
    mask   = np.triu(np.ones_like(within, dtype=bool), k=1)
    avg_w  = within[mask].mean() if mask.any() else float("nan")
    print(f"  {z:15s}  within-zone avg r = {avg_w:.3f}")

print()
for i, z1 in enumerate(zones):
    for z2 in zones[i+1:]:
        r1, r2 = EXPECTED_ZONES[z1], EXPECTED_ZONES[z2]
        avg_b  = corr.loc[r1, r2].values.mean()
        print(f"  {z1} ↔ {z2:15s}  between-zone avg r = {avg_b:.3f}")

# ── Hierarchical clustering ───────────────────────────────────────────────
# Convert correlation distance (1 - r) to linkage
dist   = 1 - corr
# Clamp tiny negatives from floating-point
dist   = dist.clip(lower=0)
linkage = hierarchy.linkage(squareform(dist.values), method="average")

print("\n── Hierarchical clustering dendrogram (text) ─────────────────────")
# Use scipy's to-tree to print a simple indent structure
from scipy.cluster.hierarchy import fcluster
labels = corr.columns.tolist()
for n_clusters in [2, 3, 4]:
    assignments = fcluster(linkage, n_clusters, criterion="maxclust")
    clusters    = {}
    for room, c in zip(labels, assignments):
        clusters.setdefault(c, []).append(room)
    print(f"\n  k={n_clusters} clusters:")
    for c, rooms in sorted(clusters.items()):
        print(f"    [{c}] {', '.join(rooms)}")

# ── Plot: clustermap ──────────────────────────────────────────────────────
# Build row colour bar from expected zones
room_to_zone = {r: z for z, rooms in EXPECTED_ZONES.items() for r in rooms}
row_colors   = pd.Series(
    [ZONE_COLORS[room_to_zone[r]] for r in zone_order],
    index=zone_order,
    name="Expected zone"
)

fig = sns.clustermap(
    corr_sorted,
    method="average",
    metric="euclidean",
    cmap="RdYlGn",
    vmin=-1, vmax=1,
    annot=True, fmt=".2f",
    linewidths=0.5,
    figsize=(11, 9),
    row_colors=row_colors,
    col_colors=row_colors,
)
fig.fig.suptitle("Room Temperature Correlations\n(coloured by expected AC zone)", y=1.02)

# Add legend
from matplotlib.patches import Patch
legend_handles = [Patch(color=c, label=z) for z, c in ZONE_COLORS.items()]
fig.ax_heatmap.legend(
    handles=legend_handles,
    loc="upper left",
    bbox_to_anchor=(1.18, 1.1),
    title="Expected zone",
    frameon=True,
)

plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"\nClustermap saved → {OUTPUT_PNG}")
