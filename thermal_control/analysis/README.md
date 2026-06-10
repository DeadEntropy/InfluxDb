# analysis/

Exploratory data analysis scripts run before any modelling. Each script
is numbered in intended run order and produces a matching PNG.

## Scripts

### `01_zone_correlation.py`

Validates AC zone assignments by computing pairwise correlations of
10-minute temperature **changes** (first differences) across all 8
observable rooms.

Using changes rather than levels removes the shared outdoor temperature
trend that inflates all level-based correlations in a Florida climate.

**Key findings:**
- Living AC and extension AC zones are tightly coherent (within-zone
  r = 0.27 each).
- Bedroom AC zone has a weaker internal correlation (r = 0.43) but
  is still clearly separated from between-zone averages.
- `anna_office` correctly clusters with the bedroom zone in the
  change-based analysis (the cross-zone correlation seen in level-based
  analysis was an outdoor temperature artefact).
- `kitchen` has a lower within-zone correlation (~0.17–0.19 with
  neighbours), consistent with counter-heat plateau distortion.

### `02_sensor_spot_checks.py`

Visual spot checks for sensors flagged as potentially unreliable.
Plots pairwise temperature comparisons over the last 10 days of
June 2025 (reference window chosen to avoid the master bedroom
sensor outage from the dead battery in later data).

**Key findings:**
- **master_bath vs master_bedroom**: clear shower-driven spikes
  visible at 10-min resolution → `master_bath` dropped from model.
- **kitchen vs dining_room**: plateau artefacts from trapped heat
  under wall shelf above countertop → `kitchen` retained but treated
  as lower-fidelity.

Outputs: `02_master_bath_vs_bedroom.png`, `02_kitchen_vs_dining.png`

### `03_ac_duty_cycle.py`

Plots the typical 24-hour duty cycle for all three AC units — average
cooling minutes per hour of day, computed over May 2025 – Jun 2026.

**Key findings:**
- `bedroom_ac` peaks overnight and early morning (sleep hours),
  consistent with cooling the bedrooms at night.
- `living_ac` peaks midday and afternoon (occupied living areas).
- `extension_ac` peaks during office hours.

Overall duty cycles: bedroom_ac ~19 min/h, living_ac ~6 min/h,
extension_ac ~8 min/h. The bedroom AC runs more because its sensor
(in the dining room) is exposed to heat from the open living area.
