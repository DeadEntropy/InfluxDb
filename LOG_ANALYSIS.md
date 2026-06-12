# LOG_ANALYSIS — AC power vs. cooling status

How the results behind `ac_power_status_check.py` and the Jun 2026 update of
`ac_power` in `thermal_control/config/control.yaml` were obtained.
Analysis window: **2026-06-04 → 2026-06-11**, all times US/Eastern.

## Goal

Cross-check the two independent records of AC activity:

1. **Power consumption** — Emporia Vue circuit monitors, logged to InfluxDB.
2. **Cooling status** — `hvac_action` (`cooling`/`idle`) of the three HA
   climate entities, from the Home Assistant REST history API.

If a thermostat reports `cooling` its circuits should draw kilowatts, and
draw ~nothing when `idle`. Sustained disagreement means a faulty sensor,
a wrong status, or a wrong zone-to-circuit assumption.

## Data sources

| Signal | Source | Detail |
|---|---|---|
| Power (W) | InfluxDB `"W"` measurement | `*_1min` Emporia entities, instantaneous watts (preferred over diffing the cumulative `*_1d` kWh counters used in `homeassistant_infux_kwh.ipynb`) |
| Status | HA `/api/history/period/...` | `attributes.hvac_action` per state-change row, per climate entity |

## Step 1 — map thermostats to circuits

Only 2 compressors + 2 air handlers are metered, but there are 3 zones.
Correlating each circuit's watts against each thermostat's cooling flag
(5-min grid, several days) gave:

| AC unit | Climate entity | Power circuits |
|---|---|---|
| living_ac | `climate.thermostat_central` | `air_compressor_1_12` + `air_handler_1_16` |
| bedroom_ac | `climate.bedroom_thermostat` | `air_compressor_2_14` + `air_handler_2_2` |
| extension_ac | `climate.thermostat_extension` | `sub_panel_1_10` + `sub_panel_2_9` |

The extension unit has no dedicated circuit: it appears as ~750 W on **each**
sub-panel leg when cooling (240 V unit metered across both legs). The
sub-panels also carry ~100 W of unrelated baseline load.

## Step 2 — the forward-fill trap

HA's InfluxDB integration writes a point **only on state change**. The watt
series therefore goes silent in two indistinguishable-looking ways:

- circuit idle at a constant 0 W → no change, no points (**valid data**);
- sensor/integration outage → no points (**missing data**; one 11.5 h gap
  was found starting mid-run).

Naive `ffill()` carried a high last-known wattage across an outage and
produced phantom all-night "power on while idle" episodes (agreement looked
as low as 66%). Fix, implemented in `ac_power_status_check.py`:

- gaps ≤ 15 min: always forward-filled;
- longer gaps: forward-filled **only if the last logged value was < 100 W**
  (an idle circuit legitimately stays silent at 0 W; a running compressor
  fluctuates and keeps logging, so silence after a high reading = outage);
- untrusted gaps are excluded from comparison and reported as coverage
  (~25% of the week for the two Emporia compressor circuits — real shared
  outages).

Slots within ±1 grid step of a status transition are also excluded from the
inconsistency counts (compressor spin-up/run-on lag, not a real mismatch).

## Step 3 — results

Steady-state agreement between "circuit power above threshold" and
"thermostat says cooling", 5-min grid:

| Unit | Agreement | Notes |
|---|---|---|
| living_ac | 96.3% | no sustained mismatch episodes |
| bedroom_ac | 98.3% | one 15-min power/idle episode |
| extension_ac | 99.9% | full coverage, essentially perfect |

**Conclusion: power consumption and cooling status are in line** for all
three units; residual disagreement is isolated transition-adjacent slots.

## Step 4 — measured cooling power → control.yaml

Steady-state **median** circuit power while `hvac_action=cooling`
(gap and transition slots excluded); extension net of its ~100 W
sub-panel baseline:

| Unit | Old config | Measured | New `ac_power` |
|---|---|---|---|
| living_ac | 1000 W | 2394 W | 2400 |
| bedroom_ac | 1200 W | 1571 W | 1600 |
| extension_ac | 900 W | 2065 − ~100 W | 1950 |

The old guesses were ~half of reality and mis-ranked the units (living_ac is
the most power-hungry, not the middle one). Since these values scale the
MPC's `energy_weight × energy` term, the energy penalty is now ~2× larger in
absolute terms; a `dry_run.py` solve still chose a sensible combination, so
`energy_weight: 0.05` was left unchanged.

## Reproducing

```bash
python ac_power_status_check.py --start-date 2026-06-04 --end-date 2026-06-11
python ac_power_status_check.py --start-date 2026-06-04 --csv mismatches.csv
```
