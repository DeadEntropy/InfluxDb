# Thermal Control

Model Predictive Controller (MPC) for a 3-zone residential AC system
in Davie, FL. The MPC reads actual room temperatures every 10 minutes,
determines the optimal combination of AC units to run, and writes
setpoints directly to the Home Assistant AC controllers.

---

## The problem this solves

The three AC units have their controller sensors in inconvenient locations:

| AC unit | Controller sensor location | Zone it cools |
|---|---|---|
| bedroom_ac | dining room | 7 bedroom-side rooms |
| living_ac | kitchen | dining room, kitchen, family room |
| extension_ac | tv room | tv room, nicolas office |

This creates two problems:

1. **Uncontrollable bedroom temperature**: the bedroom AC turns on or off
   based on the dining room temperature, not the bedroom temperature. The
   dining room is itself cooled by the living AC — so the bedroom AC is
   effectively triggered by a room it doesn't control, producing erratic
   bedroom temperatures.

2. **Cross-zone entanglement**: to reason about whether the bedroom AC
   should run, you have to know the dining room temperature, which depends
   on the living AC setpoint, which depends on the kitchen temperature.
   All three ACs are coupled.

The MPC breaks both problems by reading the **actual room sensors** and
deciding which ACs to run based on what the rooms actually need, ignoring
the physical controller sensor locations entirely.

---

## How it works

```
Every 10 minutes:
  1. Read all room temperatures from independent Zigbee sensors
  2. Resolve the active schedule entry → per-room comfort bands
     (e.g. offices get a wide band at night so the MPC ignores them)
  3. Evaluate all 2³ = 8 AC on/off combinations using the thermal model
     (predict what each room will be in 3 hours under each combination)
  4. Pick the combination that minimises total discomfort + energy use
  5. Write setpoints to AC controllers:
       ON  → setpoint = 65°F  (always below indoor temp → AC always runs)
       OFF → setpoint = 84°F  (always above indoor temp → AC never runs)
```

The thermal model (one Ridge regression per room) captures the causal
relationships between AC operation and room temperatures, including the
indirect effects that propagate through adjacent rooms.

---

## Folder structure

| Folder | Purpose |
|--------|---------|
| `config/` | House layout, AC zone mapping, control parameters |
| `analysis/` | Exploratory data analysis — zone validation, sensor checks |
| `preprocess/` | Data cleaning and feature engineering pipeline |
| `model/` | Ridge regression fitting and forward simulator |
| `control/` | Bang-bang MPC optimiser |
| `ha_bridge/` | Home Assistant REST API integration (read sensors, write setpoints) |

---

## Running order

```bash
# Data preparation (run once, or when new data is available)
python preprocess/01_stale_data.py      # identify stale sensor periods
python preprocess/02_filter.py          # apply sentinel + stale filters
python preprocess/03_ac_states.py       # resample AC states to 10-min grid
python preprocess/04_merge.py           # merge into train/val DataFrames

# Model calibration (run once, then monthly via recalibrate.py)
python model/fit.py                     # fit one Ridge model per room
python model/simulate.py               # validate: Pass 1 + Pass 2

# Operation (runs continuously)
python scheduler.py                     # main 10-min control loop
# python recalibrate.py                 # monthly model refit (not yet written)
```

---

## House

14 rooms, 3 AC zones, Davie FL (26.08°N, 80.25°W).

**8 rooms are modelled** (have independent Zigbee temperature sensors):
`master_bedroom`, `kids_bedroom`, `anna_office`, `dining_room`,
`kitchen`, `family_room`, `tv_room`, `nicolas_office`

**6 rooms excluded** (no sensor data):
`kids_playroom`, `kids_bath`, `anna_bath`, `entrance`, `nicolas_bath`

**2 rooms excluded despite having sensors:**
- `master_bath` — shower activity causes large temperature spikes
- `laundry_room` — dryer causes severe temperature distortion

---

## Data

| Source | Content | Period |
|---|---|---|
| `homeassistant_temp.csv` | Room temperatures (5-min, 11 sensors) | May 2024 – Jun 2026 |
| `homeassistant_states.csv` | AC setpoints, sensor temps, on/off (event-driven) | May 2025 – Jun 2026 |

Overlap period used for modelling: **May 2025 – Jun 2026** (12.4 months).

**Data quality issues discovered and handled:**
- Sentinel values (−58°F / −50°C): Zigbee sensors report this when offline → filtered out (< 1% of data)
- Stale readings: sensors freeze at a constant value when battery dies → periods ≥ 8h of identical readings nulled out (< 1% of data)
- Kitchen sensor: mounted under wall shelf above countertop → trapped heat from cooking causes plateau artefacts; retained in model but noted as lower-fidelity
- Master bedroom sensor: battery died May 2026, replaced June 2026 → stale periods detected and filtered

---

## Model performance

One `RidgeCV` regression per room. Features: own temperature lag,
adjacent room temperature lags, AC on/off (binary), outdoor temperature lag.
Hour-of-day features excluded — outdoor temperature already captures the
daily thermal cycle.

**Pass 1 — observed AC on/off (validation MAE):**

| Room | MAE (°C) |
|---|---|
| anna_office | 0.059 |
| dining_room | 0.057 |
| family_room | 0.057 |
| kids_bedroom | 0.066 |
| kitchen | 0.036 |
| master_bedroom | 0.077 |
| nicolas_office | 0.021 |
| tv_room | 0.043 |

All rooms well below the 1.0°C target.

**Pass 2 — simulated AC switching, 2-hour rollout (validation MAE):**

All rooms below 0.52°C. Target was 1.5°C. Master bedroom worst at 0.51°C.

---

## Key design decisions and how they evolved

| Decision | Outcome |
|---|---|
| **Bang-bang vs MILP** | Started with MILP per original spec. Revised to bang-bang: AC hardware switches on integer °F threshold so fractional setpoints give no benefit. 2³=8 combinations are trivially enumerable. |
| **Sensor resolution** | Confirmed whole °F on all 3 units from data. Sigmoid relaxation inappropriate. |
| **Hysteresis** | None detected: 96%+ of on/off transitions at delta=0. Residual non-zero values are event-log timing lag. |
| **Temperature formulation** | Compared absolute temperatures vs temperature differences (Newton's law of cooling). Both give identical MAE — the linear feature spaces are equivalent. Kept absolute for simplicity. |
| **Hour-of-day features** | Dropped. Outdoor temperature captures the daily cycle; sin/cos would be largely redundant. |
| **AC state resampling** | Forward-fill + end-of-window sample (Option 1). Preserves binary AC_on. Option 2 (majority vote) and Option 3 (fraction-on) documented in `preprocess/03_ac_states.py` for future benchmarking. |
| **Outdoor temperature source** | Pool soffit sensor ("Pool Temp" CSV column) — confirmed as outdoor air temp, not water temp. |
| **Adjacency** | `anna_office` added as adjacent to `kitchen` and `family_room` — explains high cross-zone correlation seen in EDA despite being cooled by bedroom_ac. |
| **Correlation analysis** | Level-based correlations inflated by shared outdoor temperature signal. First-difference correlations (Δ 10-min) confirm clean zone separation. |
