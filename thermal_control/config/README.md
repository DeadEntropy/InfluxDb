# config/

Static configuration files that describe the physical house and control
parameters. These are read by every other module — change here, affects
everywhere.

## Files

### `house.yaml`

Describes the physical house:

- **rooms** — each observable room with its HA sensor entity and CSV
  column name. Rooms without sensors are listed but marked excluded.
- **adjacency** — pairs of rooms that share walls or open air paths,
  used as features in the thermal DAG regression.
- **ac_units** — the three AC units: their HA entity IDs, which room
  their controller sensor lives in (`sensor_room`), and which rooms
  they thermally cover (`covers`).
- **outdoor** — the pool soffit sensor used as outdoor temperature.
- **location** — lat/lon for the Open-Meteo weather forecast.
- **data** — paths to the raw CSVs and data quality parameters.
- **sensor_resolution** — findings from the pre-implementation sensor
  checks: whole °F confirmed, no hysteresis detected.

### `control.yaml` *(not yet created)*

Will contain MPC parameters: horizon, timestep, comfort weights,
energy weights, setpoint bounds, and comfort target schedules.

## Notable facts recorded here

- `bedroom_ac` sensor lives in `dining_room` (cooled by `living_ac`)
  → cross-zone entanglement requiring joint MPC optimisation.
- `master_bath` excluded: shower activity distorts temperature.
- `laundry_room` excluded: dryer distorts temperature.
- `kitchen` retained but flagged: counter-heat plateaus observed;
  sensor mounted under wall shelf above countertop.
- `anna_office` cooled by `bedroom_ac` but adjacent to `kitchen` and
  `family_room` — adjacency edges added to capture cross-zone
  conduction.
