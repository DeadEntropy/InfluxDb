"""
03_ac_states.py
───────────────
Resamples the event-driven AC states log to a fixed 10-minute grid,
aligned with room_temps_clean.csv.

The AC states file records a row only when something changes (setpoint
adjusted, hvac_action transitions, etc.). Three signals are extracted
per AC unit:

  - setpoint_<ac>      : target temperature set on the controller (°F)
  - ac_sensor_<ac>     : temperature read by the AC's own sensor (°F)
  - ac_on_<ac>         : binary 1 = cooling, 0 = idle/off

Resampling strategy
───────────────────
All three signals use FORWARD-FILL then sample at the END of each
10-minute window. Rationale: the state at the close of a window is
what drives conditions at the start of the next — consistent with
the causal structure of the thermal model and the MPC simulator,
where AC_on is always a hard 0/1 derived from sensor vs setpoint.

FUTURE BENCHMARKING NOTE:
  Option 2 (majority vote) and Option 3 (fraction-on) are alternative
  aggregation strategies worth testing if the thermal model shows
  systematic error around AC switching transitions.
  To implement Option 3, replace the ffill().resample().last() chain
  with:
      df["ac_on_<ac>"] = (
          df["ac_on_<ac>"]
          .resample("10min")
          .mean()          # fraction of window where cooling=1
      )
  and retrain the Ridge model. The MPC simulator would then need
  a fractional AC_on input, breaking the current binary switching
  assumption — see model/simulate.py for the impact.

Output:
  thermal_control/preprocess/ac_states_clean.csv
"""

import pandas as pd
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────
CSV_PATH   = "homeassistant_states.csv"
OUTPUT_CSV = "thermal_control/preprocess/ac_states_clean.csv"
RESAMPLE   = "10min"

# Maps HA entity_id → ac unit id from house.yaml
AC_ENTITY_MAP = {
    "bedroom_thermostat"  : "bedroom_ac",
    "thermostat_central"  : "living_ac",
    "thermostat_extension": "extension_ac",
}

# ── Load ──────────────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH, index_col=0)
df["Time"] = pd.to_datetime(df["Time"], utc=True)
df = df.sort_values("Time")

print(f"Raw rows: {len(df):,}  ({df['Time'].min().date()} → {df['Time'].max().date()})")
print(f"Entities: {df['entity_id'].value_counts().to_dict()}\n")

# ── Process each AC unit ──────────────────────────────────────────────────
frames = []

for entity_id, ac_id in AC_ENTITY_MAP.items():
    sub = df[df["entity_id"] == entity_id].copy()
    sub = sub.set_index("Time")

    # Derive binary ac_on from hvac_action
    sub["ac_on"] = (sub["hvac_action_str"] == "cooling").astype(float)

    # Keep only the three signals we need
    sub = sub[["temperature", "current_temperature", "ac_on"]].copy()
    sub.columns = [f"setpoint_{ac_id}", f"ac_sensor_{ac_id}", f"ac_on_{ac_id}"]

    # Resample: forward-fill gaps then take end-of-window value
    # This preserves the hard 0/1 nature of ac_on (see module docstring)
    resampled = (
        sub
        .resample(RESAMPLE)
        .last()              # last observed value in each 10-min window
        .ffill()             # carry forward across empty windows
    )

    frames.append(resampled)
    print(f"{ac_id}:")
    print(f"  raw rows     : {len(sub):,}")
    print(f"  resampled    : {len(resampled):,}")
    print(f"  setpoint NaN : {resampled[f'setpoint_{ac_id}'].isna().sum():,}")
    print(f"  sensor NaN   : {resampled[f'ac_sensor_{ac_id}'].isna().sum():,}")
    on_pct = resampled[f"ac_on_{ac_id}"].mean() * 100
    print(f"  cooling duty : {on_pct:.1f}%\n")

# ── Merge & align ─────────────────────────────────────────────────────────
result = frames[0].join(frames[1], how="outer").join(frames[2], how="outer")
result = result.ffill()

# Align to the same index as room_temps_clean.csv
room_index = pd.read_csv(
    "thermal_control/preprocess/room_temps_clean.csv",
    index_col="time", parse_dates=True
).index
result = result.reindex(room_index, method="ffill")

print(f"Final shape: {result.shape}")
print(f"NaN per column:\n{result.isna().sum().to_string()}")

# ── Save ──────────────────────────────────────────────────────────────────
result.to_csv(OUTPUT_CSV)
print(f"\nSaved → {OUTPUT_CSV}")
