# preprocess/

Data cleaning and feature engineering pipeline. Scripts are numbered
in run order; each step depends on the output of the previous one.

## Scripts

### `01_stale_data.py`

Identifies stale sensor readings: consecutive identical values lasting
≥ 8 hours (48 × 10-min steps), which indicate a dead or dying battery
rather than a genuinely stable temperature. Produces a report and a
plot highlighting stale periods on the raw temperature timeline.

**Key findings:**
- `nicolas_office`: worst affected (46 periods, 3.6% of data) —
  two dense clusters: May 2025 and April–May 2026.
- `master_bedroom` / `kids_bedroom`: 20–26 periods each, concentrated
  in May–June 2026; periods overlap, suggesting a shared Zigbee event.
- `outdoor` (pool soffit): 17 stale periods, all April–June 2026,
  overlapping with the bedroom sensors.
- `dining_room`: zero stale periods across the full dataset.

### `02_filter.py`

Applies two filters to the raw room temperature CSV and saves a clean
10-minute resampled CSV:

1. **Sentinel filter** (≤ −10°F → NaN): Zigbee sensors report −50°C
   (−58°F) when offline instead of null. Removed 9,884 cells (0.99%).
2. **Stale filter** (≥ 8h unchanged → NaN): frozen values from dying
   batteries. Removed 8,331 cells (0.84%).

Total NaN after both filters: 4.4%. Output: `room_temps_clean.csv`.

### `03_ac_states.py`

Resamples the event-driven AC states log to the same 10-minute grid
as `room_temps_clean.csv`. Extracts three signals per AC unit:
`setpoint_<ac>`, `ac_sensor_<ac>`, `ac_on_<ac>`.

**Resampling strategy — forward-fill + end-of-window sample (Option 1)**

The AC log only records rows on state changes. Within each 10-minute
window, the last observed value is carried forward. This preserves the
binary 0/1 nature of `ac_on`, which is required for consistency with
the MPC simulator's hard switching logic.

*Future benchmarking note*: Option 2 (majority vote) and Option 3
(fraction-on) are documented in the script's docstring and worth
testing if the thermal model shows systematic error around switching
transitions. Option 3 would require changes to `model/simulate.py`.

Output: `ac_states_clean.csv`.

### `04_merge.py` *(not yet created)*

Merges `room_temps_clean.csv` and `ac_states_clean.csv` on the shared
10-minute index, creates lagged features, and produces the final
training DataFrame used by `model/fit.py`.

## Output files

| File | Description |
|------|-------------|
| `room_temps_clean.csv` | Filtered room temperatures, 10-min grid |
| `ac_states_clean.csv` | Resampled AC setpoints, sensor temps, on/off |
| `merged_features.csv` | *(pending)* Final training DataFrame |
