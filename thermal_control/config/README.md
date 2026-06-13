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

### `control.yaml`

MPC parameters and comfort targets:

- `mpc.horizon_steps` — 18 steps (3 hours at 10-min resolution)
- `mpc.tick_minutes` — 10 (scheduler interval)
- `mpc.setpoint_on_f` — 65°F (forces AC on; always below indoor temp)
- `mpc.setpoint_off_f` — 84°F (forces AC off; never reached indoors)
- `mpc.energy_weight` — 0.05 (small; only breaks ties)
- `mpc.use_forecast` — false; set true to use Open-Meteo forecast over the horizon
- `targets.default` — comfort band applied to all rooms when no schedule
  entry or per-room override applies
- `targets.schedule` — list of time-of-day entries (24h local time) that
  override per-room bands for a time window; see structure below
- `targets.<room_id>` — static per-room override that applies at all times
  regardless of schedule (takes priority over schedule entries)
- `ac_power` — approximate watts per unit for energy penalty scaling:
  bedroom_ac 1200 W, living_ac 1000 W, extension_ac 900 W

### Schedule structure

Each schedule entry takes effect at its `time` and remains active until
the next entry. The list wraps around midnight so the last entry covers
the period back to the first entry's time.

```yaml
targets:
  default:
    min_f: 75
    max_f: 77

  schedule:
    - name: sleeping          # 22:00 → 07:00
      time: "22:00"
      rooms:
        master_bedroom: {min_f: 74, max_f: 76}   # tighter: active comfort zone
        kids_bedroom:   {min_f: 74, max_f: 76}
        nicolas_office: {min_f: 65, max_f: 85}   # wide: room unoccupied, ignore
        anna_office:    {min_f: 65, max_f: 85}
        tv_room:        {min_f: 65, max_f: 85}

    - name: daytime           # 07:00 → 22:00
      time: "07:00"
      rooms: {}               # all rooms fall back to default band

  # Static per-room overrides (applied at all times, beats schedule)
  # nicolas_office:
  #   min_f: 73
  #   max_f: 76
```

A wide band like `{min_f: 65, max_f: 85}` is the standard way to mark a
room as "don't care" — the discomfort penalty in the cost function becomes
zero at any realistic indoor temperature, so the MPC will not run an AC
unit purely to service that room.

### Priority order (highest wins)

0. **Away / holiday mode** (`targets.away`, item 8) — when the HA toggle
   `input_boolean.mpc_away_mode` is ON, every room gets its away band,
   overriding everything below.
0a. **Manual override** (item 7) — a non-zero per-room `override_entity` slider
   (in `house.yaml`) shifts the resolved band by N°F (both bounds) for
   `mpc.override_duration_minutes`, then the scheduler resets it to 0. Beats
   presence; yields to away mode.
0b. **Presence** (item 9) — a room whose `presence_entity` (in `house.yaml`)
   reports `off` drops to the wide 65–85°F "don't care" band. Beats the
   schedule/static band but yields to away mode and a manual override.
1. Static `targets.<room_id>` override
2. Active schedule entry `rooms.<room_id>`
3. `targets.default`

The scheduler resolves the active entry by finding the latest `time` ≤
current local time (wrapping midnight). Entries may carry an optional `days`
field (`weekday` / `weekend`; absent = every day) so e.g. the weekend sleeping
block can run longer. `BangBangMPC.solve()` receives the already-resolved
`{room_id: {min_f, max_f}}` dict; the MPC itself has no knowledge of the
schedule, away mode, or presence — those are all folded into the bands by
`resolve_targets_for_rooms()`.

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
