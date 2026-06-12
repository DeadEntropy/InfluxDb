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

`BangBangMPC` class. Core method: `solve(current_state, outdoor_series)`.

- `current_state`  : `{room_id: temp_F}` from independent room sensors
- `outdoor_series` : `list[float]` (°F), one value per horizon step —
  either a real forecast from `forecast.py` or `[current_outdoor] * horizon`
- Returns          : `{ac_id: setpoint_F}` — 65°F (ON) or 84°F (OFF)

Before each `solve()` call the scheduler resolves the active schedule
entry from `control.yaml` and updates `mpc.targets` accordingly. Rooms
marked inactive in the current schedule window carry a wide comfort band
(65–85°F), making their discomfort cost effectively zero so the MPC will
not run an AC unit purely to service them. The MPC itself is stateless
with respect to the schedule — it only sees the resolved target dict.

**Objective function:**
```
cost = Σ_t w_t · [ Σ_rooms  max(0, T − T_max)²   (too hot penalty)
                          + max(0, T_min − T)² ] (too cold penalty)
     + mean(w) · energy_weight · Σ_j power_j · AC_on_j
```

`w_t` is a per-step discount, linear from `discount_start` (1.0 at
t+10min) to `discount_end` (0.25 at the 3h horizon end). The thermal
model accumulates error over the rollout, so a breach predicted 3 hours
out counts for a quarter of one happening now — the MPC reacts strongly
to present breaches and is less willing to spend energy on speculative
future ones. The energy term is scaled by `mean(w)` (equivalent to
spreading it per-step and discounting), so the discount does not shift
the discomfort/energy balance.

The energy penalty is small (weight 0.05) and only breaks ties —
comfort always dominates.

**`explain()`** prints three sections after `solve()`:

1. **Comfort diagnosis** — each room's current temperature vs its target
   band, highlighting which rooms are too hot or too cold.
2. **All 8 combinations ranked by cost** — shows setpoints and total
   cost for every combo, with the chosen one marked.
3. **Projected outcome** — for the chosen combo, each room's current
   temperature, predicted temperature at end of horizon, trend
   direction, and whether it lands inside the comfort band.

### `forecast.py`

Fetches the outdoor temperature forecast from Open-Meteo (free, no API
key). Interpolates hourly values to 10-minute resolution and converts
Celsius to °F. Returns a list of length `horizon_steps`, or `None` on
any network or parse failure (callers fall back to constant temp).

Enabled by `use_forecast: true` in `config/control.yaml`.

## Configuration

See `config/control.yaml`:
- `setpoint_on_f`  : 65°F — forces AC on (always below indoor temp)
- `setpoint_off_f` : 84°F — forces AC off (never reached indoors)
- `horizon_steps`  : 18 (3 hours at 10-min resolution)
- `targets`        : per-room comfort bands in °F
- `energy_weight`  : 0.05 (small, only breaks ties)
- `discount_start` / `discount_end` : 1.0 → 0.25 — per-step cost discount
  across the horizon (set both to 1.0 to disable)
- `use_forecast`   : false — set true to use Open-Meteo forecast over the horizon
