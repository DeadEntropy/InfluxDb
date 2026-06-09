# control/

MPC optimiser and weather forecast module.

## Design

The controller uses a **bang-bang Model Predictive Controller**: each
AC is either fully ON (setpoint forced to minimum) or fully OFF
(setpoint forced to maximum). With 3 AC units there are 2³ = 8
possible combinations. The controller evaluates all 8 over a 3-hour
lookahead horizon and picks the one that minimises total discomfort
plus a small energy penalty.

### Why bang-bang?

The physical AC controllers have integer °F setpoints and switch on
a simple threshold (sensor temp > setpoint). Fractional setpoints
give no extra resolution — the effective control is binary. Bang-bang
is therefore not a simplification; it is the correct model for this
hardware.

### Why this solves the entanglement problem

The bedroom AC controller sensor lives in the dining room, which is
also cooled by the living AC. In practice this means:

- The bedroom AC may run even when bedrooms are already cold
  (dining room is warm due to cooking or living AC being off)
- The bedroom AC may not run when bedrooms are hot
  (dining room is cool because the living AC ran)

The MPC breaks this loop by:
1. Reading the **actual bedroom sensor** (independent Zigbee sensor)
2. Evaluating whether cooling the bedrooms is needed
3. Setting the bedroom AC setpoint to min (65°F) or max (84°F)
   regardless of what the dining room temperature is

The dining room temperature is irrelevant to the control decision —
it only matters as a pass-through for the switching hardware.

## Files

### `mpc.py`

`BangBangMPC` class. Core method: `solve(current_state, current_outdoor)`.

- `current_state`   : `{room_id: temp_F}` from independent room sensors
- `current_outdoor` : current outdoor temperature (°F)
- Returns           : `{ac_id: setpoint_F}` — 65°F (ON) or 84°F (OFF)

**Objective function:**
```
cost = Σ_t Σ_rooms  max(0, T − T_max)²   (too hot penalty)
                  + max(0, T_min − T)²   (too cold penalty)
     + energy_weight · Σ_j power_j · AC_on_j
```

The energy penalty is small (weight 0.05) and only breaks ties —
comfort always dominates.

**`explain()`** prints a ranked table of all 8 combinations with
their costs, useful for debugging and manual inspection.

### `forecast.py` *(not yet created)*

Will fetch outdoor temperature forecast from Open-Meteo API for use
as the outdoor input during the horizon rollout. Currently the MPC
uses the current observed outdoor temperature as a constant.

## Configuration

See `config/control.yaml`:
- `setpoint_on_f`  : 65°F — forces AC on (always below indoor temp)
- `setpoint_off_f` : 84°F — forces AC off (never reached indoors)
- `horizon_steps`  : 18 (3 hours at 10-min resolution)
- `targets`        : per-room comfort bands in °F
- `energy_weight`  : 0.05 (small, only breaks ties)
