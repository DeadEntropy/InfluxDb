# Thermal Control

Model Predictive Controller (MPC) for a 3-zone residential AC system.
The MPC optimises AC setpoints every 10 minutes to maintain comfort
in all rooms while minimising energy consumption.

## How it works

```
MPC sets Setpoint_j on AC controller j
       ↓
AC_j compares Setpoint_j to its own sensor temperature
       ↓
AC_j switches on if sensor_temp > Setpoint_j
       ↓
AC_j cools its zone (may include rooms other than the controller's room)
```

Because the bedroom AC's sensor lives in the dining room (cooled by the
living AC), all three setpoints must be optimised jointly.

## Folder structure

| Folder | Purpose |
|--------|---------|
| `config/` | House layout, AC zone mapping, sensor resolution findings |
| `analysis/` | Exploratory data analysis — zone validation, sensor spot checks |
| `preprocess/` | Data cleaning and feature engineering pipeline |
| `model/` | DAG definition, Ridge regression fitting, forward simulator |
| `control/` | MPC optimiser, weather forecast |
| `ha_bridge/` | Home Assistant REST API integration |

## Running order

```
preprocess/01_stale_data.py      # identify stale sensor periods
preprocess/02_filter.py          # apply sentinel + stale filters
preprocess/03_ac_states.py       # resample AC states to 10-min grid
preprocess/04_merge.py           # merge rooms + AC into one DataFrame
model/fit.py                     # fit one Ridge model per room
model/simulate.py                # forward simulator (validation)
scheduler.py                     # main loop (runs every 10 min)
recalibrate.py                   # monthly model refit
```

## House

14 rooms, 3 AC zones, Davie FL. 8 rooms have temperature sensors and
are modelled; 6 rooms (bathrooms, playroom, entrance) have no sensors
and are excluded from the thermal model but still receive conditioned
air from their respective AC zones.

## Key design decisions

- **MILP solver**: AC sensors report in whole °F — sigmoid relaxation
  is inappropriate; integer setpoints require mixed-integer programming.
- **No hysteresis**: all three AC units switch at delta = 0 (confirmed
  from data; residual non-zero transitions are logging lag artefacts).
- **Ridge regression per room**: interpretable, stable under correlated
  inputs, fast to evaluate inside the MPC loop.
- **Forward-fill resampling for AC states**: preserves binary AC_on
  signal, consistent with MPC simulator's hard 0/1 switching logic.
- **Outdoor temperature**: measured by pool soffit sensor ("Pool Temp"
  CSV column), not a dedicated outdoor sensor.
