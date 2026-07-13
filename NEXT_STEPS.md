# Next Steps

Observations from the 2026-06-12 live run.

---

## 1. Widen comfort bands to reduce cross-zone overcooling

Two zones are driving AC decisions that overcool other rooms:

**anna_office (bedroom_ac zone)**
anna_office is physically open to family_room (living_ac zone) and the thermal model
learned cross-zone coupling between them. When the MPC projects anna_office approaching
its 77°F upper bound, it turns living_ac ON to cool it indirectly — overcooling
dining_room and family_room below their 75°F lower bound as a side effect.
Fix: widen anna_office's daytime band in `config/control.yaml` (e.g. 74–78°F).

**nicolas_office (extension_ac zone)**
nicolas_office has a tight daytime band (74–76°F). The MPC keeps extension_ac running
continuously from 10:40 to 15:00 to prevent nicolas_office reaching 76°F at the 3h
horizon — while tv_room (the sensor room for that AC unit) is held below its own 75°F
lower bound as a result.
Fix: widen nicolas_office's daytime band (e.g. 74–77°F or 74–78°F).

---

## 2. Thermal model may be miscalibrated for tv_room

When extension_ac is ON, the model projects tv_room warming back up to ~75.5–76°F within
3 hours. In reality tv_room stays at ~74.8–74.9°F throughout — the model underestimates
how much extension_ac cools the sensor room.

This miscalibration compounds the band issue above: the MPC thinks the tv_room
undercooling is transient (low accumulated cost) when it is actually persistent across
the full 18-step horizon. Retraining the model on live-run data (where the MPC's own
setpoints are in effect) may correct this. Alternatively, validate whether the
discrepancy disappears after widening nicolas_office's band (less aggressive AC cycling
→ less overcooling of tv_room).

---

## 3. Mount config externally so it can be updated without rebuilding the image

Currently `config/house.yaml` and `config/control.yaml` are baked into the Docker image.
Any band or parameter change requires a full rebuild and redeploy. Instead, mount the
`thermal_control/config/` directory as a volume in `docker-compose.yml` so the files can
be edited on the server and picked up on the next scheduler tick without touching the
image. The model weights (`model/weights/`) could be treated the same way to allow
retraining without a rebuild.

**Status: implemented 2026-06-12.** `docker-compose.yml` mounts `config/` and
`model/weights/` read-only into both services (one-time setup: copy those dirs next to
the compose file on the server). `scheduler.py` checks the yaml mtimes each tick and
rebuilds the simulator + MPC on change — a reload also re-reads the mounted weights. A
mid-edit/invalid yaml is logged and skipped; the controller keeps its last-good config
and retries next tick. Shadow runs pick up changes on their next start (they exit after
24 h anyway).

---

## 4. Discount future steps in the MPC cost function

The thermal model accumulates error over the rollout horizon — predictions at t+3h are
materially less reliable than predictions at t+10min. The current cost function weights
all 18 steps equally, which gives the same influence to a near-certain present reading
and a speculative 3h projection.

Proposal: apply a linearly decreasing discount factor across the horizon, from 1.0 at
step 1 (t+10min) down to 0.25 at step 18 (t+180min), with intermediate steps
interpolated linearly. This makes the MPC more responsive to rooms that are out of band
right now and less willing to accept a present breach in exchange for avoiding a
projected future one.

Implementation: in `control/mpc.py`, compute a weight vector of length `horizon_steps`
and multiply each step's discomfort and energy contribution by the corresponding weight
before summing into the total cost.

**Status: implemented 2026-06-12** (`discount_start`/`discount_end` in `control.yaml`,
defaults to no-op when keys absent). The energy term is scaled by mean(weight) so the
discomfort/energy balance is unchanged. Open-loop replay of the 2026-06-12 live log:
11/54 decisions flip, bedroom_ac pre-cooling drops 19→10 ON ticks (the discount removes
cooling driven by far-horizon projections), but extension_ac barely changes (45→43) —
the nicolas_office pressure is near-term, so item 1 (band widening) is still needed for
that zone. Note: logged `cost_*` values drop in scale (~×0.6) from this change onward.

---

## 5. Account for Expected Weather change

right not the future outdoor temperature is extrapolated flat from the latest reading 
coming from the outdoor thermostat. that is problematic as we get closer to the night 
and the temperature goes down

Proposal: use an external free server Meteo-something (see other docs) and pull 3h forecast
then compute temperature deltas between now and the next 3h and apply this to the temp
coming from the exterior sensor. use that as forecast exterior temperature.

**Status: implemented.**
`control/forecast.py` fetches an hourly 3-hour forecast from Open-Meteo (free, no API key)
and applies only the forecast *trend* (delta from now) to the current pool-soffit sensor
reading. The level always comes from the local sensor to avoid Open-Meteo's grid bias.
`use_forecast: true` is already set in `config/control.yaml`. Falls back to a flat series on
any network or parse failure.

---

## 6. Weekend Scheduling

The current schedule is calibrated for weekdays. On weekends (Saturday + Sunday) we sleep
longer, so the sleeping block should run until 07:30 instead of 06:00.

Scope for now: only the sleeping block end time differs — all other slots stay the same.

Implementation: add an optional `days` field (e.g. `weekday` / `weekend`) to each schedule
entry in `config/control.yaml`. `control/schedule.py` selects entries whose `days` matches
the current weekday. Entries with no `days` field apply every day (backwards-compatible).
Weekend = Saturday + Sunday.

**Status: implemented 2026-06-13.** `_entry_applies()` in `control/schedule.py` filters
entries by their optional `days` field before the latest-time-wins selection; absent `days`
applies every day. The `daytime` entry is now split into a weekday copy (06:00, `days:
weekday`) and a weekend copy (07:30, `days: weekend`) — on weekends the every-day `sleeping`
block stays active until 07:30 since the 06:00 entry is filtered out. `now.weekday()`
(local tz, already passed in) drives the choice. An unknown `days` value raises ValueError.

---

## 7. Manual Short-Term Override

Someone in the house is too hot or too cold and wants to temporarily shift the comfort band
for their room without touching the scheduler config.

Design:
- **Granularity**: per room (8 rooms modelled).
- **Effect**: shifts the entire band up or down by N °F (both min and max move together).
- **Duration**: 1 hour from activation, then the scheduled band resumes. Duration is
  configurable via `override_duration_minutes` in `config/control.yaml`.
- **HA side** (to be created): one `input_number` helper per room, range e.g. −5 to +5 °F,
  default 0. A non-zero value means an override is active; writing it back to 0 from the
  scheduler cancels it after the duration expires. Entity naming TBD (e.g.
  `input_number.mpc_override_nicolas_office`).
- **MPC side**: `ha_bridge/controller.py` reads the override values each tick; `control/
  schedule.py` applies the shift on top of the scheduled band before passing targets to the
  MPC. The scheduler also logs override activation/expiry to the user-actions log (item 10).

**Home Assistant setup.** Create one `input_number` helper per modelled room. Either add the
block below to `configuration.yaml` (then restart HA / reload helpers), or build each one via
Settings → Devices & Services → Helpers → **+ Create Helper → Number**:

```yaml
input_number:
  mpc_override_master_bedroom:   {name: "MPC Override – Master Bedroom",  min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_kids_bedroom:     {name: "MPC Override – Kids Bedroom",    min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_anna_office:      {name: "MPC Override – Anna Office",     min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_dining_room:      {name: "MPC Override – Dining Room",     min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_kitchen:          {name: "MPC Override – Kitchen",         min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_family_room:      {name: "MPC Override – Family Room",     min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_tv_room:          {name: "MPC Override – TV Room",         min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
  mpc_override_nicolas_office:   {name: "MPC Override – Nicolas Office",  min: -5, max: 5, step: 1, mode: slider, unit_of_measurement: "°F", icon: mdi:thermometer}
```

- **Naming**: entities follow `input_number.mpc_override_<room_id>`, matching the room ids in
  `config/house.yaml`. Declare the entity id per room in `house.yaml` (e.g. an `override_entity:`
  key) so the mapping stays data, not code.
- **Usage**: drag the slider (or call `input_number.set_value`) to a non-zero value to shift that
  room's band by that many °F; 0 = no override. The scheduler resets it to 0 once
  `override_duration_minutes` has elapsed, so the slider visibly snaps back when the override
  expires.
- **MPC side**: the existing `HA_TOKEN` already has read/write access — the scheduler reads each
  helper via the REST `/api/states/input_number.mpc_override_<room>` and cancels via the
  `input_number.set_value` service. No extra HA permissions needed.
- A simple Lovelace **Entities card** listing the eight sliders gives a one-screen control panel.

**Status: implemented 2026-06-13.** Each of the 8 modelled rooms has an `override_entity`
(`input_number.mpc_override_<room_id>`) in `house.yaml`; `mpc.override_duration_minutes`
(default 60) in `control.yaml` sets the window. `controller.get_overrides()` reads the integer
shifts (fail-safe to 0) and `controller.clear_override()` writes 0 back on expiry.
`schedule.update_override_tracker()` is the lifecycle engine: it carries a `{room: (shift,
started_at)}` tracker across ticks and emits `activated`/`changed`/`expired`/`cancelled` events
(a changed value restarts the timer). `schedule.resolve_targets_for_rooms(overrides=…)` shifts
both bounds of the resolved band — **beating presence** (an explicit request conditions the room)
but **yielding to away mode**. The prod scheduler reads, tracks, applies, and resets expired
sliders to 0; `dry_run.py` shows any non-zero slider as active without the duration lifecycle.
Transition events go to the logger; the structured `user_inputs.log` trail is item 10 (now done). Note
the precedence is now away > override > presence > static > schedule > default.

---

## 7b. Thermostat-Card UI for the Manual Override

Replace the per-room override **slider** (item 7: an `input_number` holding a relative °F
shift) with a per-room **thermostat card** in the HA app. The card shows the room's current
temperature and a single target temperature; that target is the **upper bound** (`max_f`) of
the comfort band, and the **lower bound follows it at constant width** so the user only ever
sets one number.

**Required behaviour (confirmed):**
1. When no override is active, the card **always displays the schedule's current upper bound**
   for that room. The scheduler keeps the card in sync — when the time-of-day band changes,
   the displayed target moves with it.
2. The user dragging the dial to a **different** value activates an override: the band tops
   out at the new value for the 60-min window.
3. At the end of the window the schedule resumes **and the card's displayed target reverts to
   the scheduled upper bound** (the dial visibly snaps back).

The override **lifecycle** (60-min duration, expiry, precedence `away > override > presence >
static > schedule > default`) is **kept exactly as item 7**; only the input/representation and
the new "keep the card synced to the schedule" behaviour are added. Decisions taken: backing
entity = HA-core `generic_thermostat`; duration = keep the existing 60-min expiry.

### Mechanism — write-back sync + read-back detection

Because the card must *show* the schedule and the user edits the *same* control, the scheduler
both **writes** the scheduled target onto each card and **reads it back** to detect edits.
Per room it carries one extra piece of state across ticks: `last_written` = the scheduled
upper bound it last pushed to the card. Each tick, for a room with no active override:

- Read the card's current target `T_read`.
- If `T_read != last_written` → the **user changed it** → start a 60-min override at `T_read`.
- Else (user hasn't touched it) → if the scheduled upper bound `S_now` has changed
  (schedule transition), call `set_temperature(S_now)` on the card and set `last_written =
  S_now` so the dial tracks the schedule.

On **expiry/cancel**, the scheduler writes the *current* scheduled upper bound back to the card
(`set_temperature(S_now)`, `last_written = S_now`) — that is what makes the dial snap back to
the schedule. The thermostat stays in `cool` mode at all times (it always shows a target);
`hvac_mode` is **not** used as an override signal. On the first tick the scheduler writes the
scheduled value to every card to seed `last_written`.

Note: a user who sets the card to *exactly* the scheduled value is indistinguishable from "no
edit" — that's a harmless no-op (the band is already that). Whole-°F precision makes the
`T_read != last_written` comparison exact.

### Key equivalence (why this reuses almost all of item 7)

Setting the absolute upper bound to `T` is identical to applying the item-7 shift
`T − scheduled_max_f` to *both* bounds: the max becomes `T` and the min becomes
`T − (scheduled_max_f − scheduled_min_f)` = `T − W`, i.e. the **scheduled band width `W` is
preserved automatically**. So the override-application engine stays; the override is just
parameterised by an absolute target instead of a shift.

The tracker keys on the **user's absolute target**, not the derived shift — otherwise a
schedule transition mid-window would change `scheduled_max_f`, change the derived shift, and
falsely restart the 60-min timer. Track the target; recompute the band fresh each tick.

### HA side (to be created) — `generic_thermostat` per room

A thermostat card needs a `climate` entity. The MPC's real AC units already have
`climate_entity`s (in `house.yaml` under `ac_units`); these new entities are **separate,
UI-only virtual thermostats** — the MPC never commands them to cool, it only reads/writes
their *target*. Each is a core `generic_thermostat` whose `current_temperature` mirrors the
room's existing sensor (so the card "shows the current temperature" for free) and whose
`heater` points at a throwaway `input_boolean` we ignore. Add to `configuration.yaml`
(restart HA), one block per modelled room:

```yaml
input_boolean:        # throwaway switch the generic_thermostats toggle; ignored by the MPC
  mpc_dummy_master_bedroom: {name: "MPC dummy (master_bedroom)"}
  # … one per room …

climate:
  - platform: generic_thermostat
    name: "MPC Master Bedroom"            # entity → climate.mpc_master_bedroom
    unique_id: mpc_master_bedroom
    heater: input_boolean.mpc_dummy_master_bedroom
    target_sensor: sensor.master_bedroom_temperature
    ac_mode: true                          # cooling semantics; stays in 'cool'
    initial_hvac_mode: cool                # always on so the card always shows a target
    min_temp: 65                           # bounds the draggable range
    max_temp: 85
    target_temp: 77                        # seeded; scheduler overwrites with the schedule
    precision: 1.0                         # whole-°F targets (HW switches on whole °F)
  # … one per room: kids_bedroom, anna_office, dining_room, kitchen, family_room,
  #    tv_room, nicolas_office …
```

- **The card is always "on" (cool) and always shows a target** — the scheduled upper bound
  when idle, the user's value while overriding. `hvac_mode` carries no override meaning here.
- **Cosmetic only:** generic_thermostat shows a heating/cooling ring and toggles its dummy
  switch based on current-vs-target; ignored. (A HACS *template climate* would avoid the fake
  ring/switch; rejected here to stay HA-core.)

### MPC side

- **`config/house.yaml`** — replace each room's `override_entity: input_number.mpc_override_<room>`
  with `thermostat_entity: climate.mpc_<room>` (keep it as data, not code).
- **`ha_bridge/controller.py`**
  - `get_overrides()` → **`get_room_targets(house)`**: returns `{room_id: target_f}` — the raw
    `attributes.temperature` of each card, rounded to int; omit a room whose card is
    `unavailable`/`unknown` (fail-safe → treated as "no edit / follow schedule").
  - `clear_override()` → **`set_room_target(house, room, temp_f)`**: call `climate.set_temperature`.
    Used both to sync the card to the schedule and to revert it on expiry. (The existing
    `set_hvac_mode(ac_id, …)` resolves ids against `ac_units`; add a small generic helper for
    these room entities rather than reusing it. Also ensure the card stays in `cool`.)
- **`control/schedule.py`**
  - `update_override_tracker()` — track the **absolute target** instead of the shift;
    `activated/changed/expired/cancelled` semantics unchanged. "No override" = room absent from
    the dict (replacing the `0` sentinel).
  - `resolve_targets_for_rooms()` — apply an active override as `{max_f: target, min_f: target −
    W}` where `W` is the room's scheduled band width for the tick (equivalently: shift `target −
    scheduled_max_f`). Still **beats presence, yields to away**; `W`/the base is the scheduled
    band *ignoring* presence (override beats presence).
- **`scheduler.py`** — owns the write-back sync described above: carry `last_written` per room,
  resolve the base scheduled band first (for `S_now`/`W`), detect edits via `T_read !=
  last_written`, push `S_now` on schedule transitions and on expiry. Log the override as an
  absolute `→ 79°F` target, not a `+2°F` shift.
- **`dry_run.py`** — shows any active target without the duration lifecycle (no writes, so no
  card sync/revert).
- **`scheduler._input_signature()` (the item-7a responsive sleep)** — **must be updated too**:
  it currently polls `get_overrides`; point it at `get_room_targets` so the ~20-s early-wake
  still fires when the user drags the card. Compare the poll against `last_written` so the
  scheduler's *own* write-back doesn't count as a change. (Without this the card regresses to
  up-to-10-min latency.)

### Dashboard

All 8 rooms get a `climate.mpc_<room>` entity, but the **main dashboard shows thermostat
cards only for the key rooms** — master_bedroom, nicolas_office, family_room — with the
remaining five on a secondary/detail view:

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.mpc_master_bedroom
  - type: thermostat
    entity: climate.mpc_nicolas_office
  - type: thermostat
    entity: climate.mpc_family_room
```

### Edge cases / notes

- Targets are whole °F (consistent with the integer-setpoint rule); `min_temp/max_temp` bound
  the draggable range to 65–85°F so a stray value can't escape the modelled range.
- Card `unavailable`/`unknown` → no edit detected (follow schedule), never an error.
- **away mode**: the synced/display target becomes the **away band's upper bound** — the
  scheduler pins each card to the away max while away, and a user edit during away does **not**
  take effect (away beats override) and is overwritten on the next tick. When away clears, the
  cards resync to the schedule.
- **presence**: presence is an automatic layer that still drops an unoccupied room's actual
  band to wide 65–85°F, but is **not** reflected on the dial (the card keeps showing the
  schedule); an explicit user edit still beats presence, as today.
- The dummy `input_boolean`s and the heating/cooling ring are cosmetic; the MPC keeps
  controlling the real ACs via setpoints exactly as before.
- **Tests**: update `tests/` that monkeypatch `get_overrides`/`clear_override`; add cases for
  target→band conversion, width preservation across a schedule transition, edit detection
  (`T_read != last_written` ⇒ override; `==` ⇒ none), schedule-transition write-back, and
  expiry ⇒ `set_temperature(scheduled)`.

**Status: MPC side implemented 2026-06-14; HA side pending (built in parallel).**
`house.yaml` now declares `thermostat_entity: climate.mpc_<room>` per room (replacing
`override_entity`). `controller.get_room_targets()` reads each card's target temperature
(fail-safe omit on unavailable) and `controller.set_room_target()` writes it via
`climate.set_temperature`. `schedule.update_override_tracker()` tracks the absolute target
(so a schedule transition no longer restarts the 60-min timer) and
`resolve_targets_for_rooms(override_targets=…)` applies it as `{max_f: target, min_f: target −
W}`. `scheduler.py` carries `card_synced` per room and runs the write-back sync via two pure
helpers — `_plan_card_detection` (first-tick seed / away-pin / edit-detect) and
`_plan_card_revert` (resync on schedule transition, revert on expiry/cancel); the item-7a
responsive-sleep `_input_signature` now polls `get_room_targets`. `dry_run.py`
uses a stateless "card ≠ scheduled max ⇒ override" (no writes, so no duration/expiry). Tests in
`tests/test_controller.py`, `tests/test_schedule.py`, `tests/test_scheduler.py` updated/added.
**Startup caveat:** on scheduler start the cards are reset to the current schedule value
(seeding `card_synced`), so any override left on a card across a restart is discarded.
**Still to do (HA side):** create the per-room `generic_thermostat` + dummy `input_boolean`
entities and the dashboard cards per the config above, then verify end-to-end against live HA.

---

## 8. Long-Term Override (Holiday Mode)

When the house is empty for multiple days the regular schedule wastes energy cooling rooms
nobody is in.

Design:
- **HA side** (to be created): one `input_boolean` switch (e.g. `input_boolean.mpc_away_mode`)
  toggled from the HA phone app upon departure/return.
- **Away temperatures**: configured per room in `config/control.yaml` under an `away:` block
  (e.g. `nicolas_office: {min_f: 76, max_f: 80}`). Rooms without an explicit away entry use a
  global `away_default` band (e.g. 76–80 °F).
- **Priority**: away mode overrides both the regular schedule and presence sensors (item 9) —
  the house is empty regardless of what a sensor reports.
- **MPC side**: `ha_bridge/controller.py` reads the boolean each tick; `control/schedule.py`
  substitutes the away band when the flag is set. Activation/deactivation is logged to the
  user-actions log (item 10).

**Home Assistant setup.** Create a single `input_boolean` helper. Either add the block below to
`configuration.yaml` (then restart HA / reload helpers), or build it via Settings → Devices &
Services → Helpers → **+ Create Helper → Toggle**:

```yaml
input_boolean:
  mpc_away_mode:
    name: "MPC Away Mode"
    icon: mdi:home-export-outline
```

- **Naming**: entity is `input_boolean.mpc_away_mode`. Put this id in `config/control.yaml`
  (alongside the `away:` band block) so it is configurable, not hardcoded.
- **Usage**: toggle it ON from the HA phone app on departure and OFF on return; the scheduler
  reads it each tick via REST `/api/states/input_boolean.mpc_away_mode`. Unlike the per-room
  override (item 7) it has **no auto-expiry** — it stays on until manually switched off.
- **Optional, nicer UX**: add it to a dashboard as a toggle, or wire HA automations to flip it
  from a phone's `device_tracker` zone (auto-on when everyone leaves the home zone, auto-off when
  someone returns). That automation lives entirely in HA; the MPC only reads the boolean.
- **Priority**: away mode beats both the schedule and presence sensors (item 9) — see the
  priority note above.

**Status: implemented 2026-06-13.** Config lives under `targets.away` in `control.yaml`
(`entity` = the HA toggle, `default` band, optional per-room `rooms` overrides — nested rather
than the flat `away_default` originally sketched). `controller.get_away_mode(control)` reads the
boolean and **fails safe to "home"** (normal schedule) on any read error or missing entity, so a
flaky helper never strands the house warm. `schedule.resolve_targets_for_rooms(..., away=True)`
substitutes the away band for every room, beating the schedule and static overrides (`away` is
also reserved in `resolve_targets` so the block isn't mistaken for a per-room override).
`scheduler.py` and `dry_run.py` read the flag each tick. ON/OFF
*transitions* are logged via the normal logger. `mpc_decision_log.csv` is deliberately left
unchanged — adding a column would give new rows more fields than the existing server log's header
and break the live-analysis pandas read. The structured per-tick audit trail (`user_inputs.log`) is
item 10 (now done). Presence-sensor precedence (item 9) is documented but item 9 isn't built
yet, so there is nothing for away mode to override there today.

---

## 9. Presence Sensor

Avoid cooling a room nobody is in, without requiring the user to manually adjust anything.

Design:
- **Scope**: nicolas_office only for now. Other rooms have no sensor and are treated as always
  occupied (current behaviour unchanged).
- **Presence proxy**: use the office **light** as the occupancy signal — light ON = someone is
  in the room, light OFF = empty. No new hardware required; nicolas_office already has a
  controllable light entity in HA. (A real motion/mmWave sensor stays a future upgrade if the
  light proves too coarse — e.g. someone working in the dark, or a light left on.)
- **HA side** (to be built): a `binary_sensor` derived from the light state (e.g.
  `binary_sensor.presence_nicolas_office`, `on` when the light is on). Entity name declared in
  `config/house.yaml` under the room's entry so it's data, not code.
- **Effect when unoccupied**: wide band 65–85 °F (MPC ignores the room, same as the existing
  "don't care" convention). No special energy mode — zero discomfort penalty is sufficient.
- **Priority**: away mode (item 8) overrides presence sensors. If no sensor is configured for
  a room, the MPC assumes the room is occupied.
- Presence changes are logged to the user-actions log (item 10).

**Home Assistant setup.** Presence is derived from the office light state. Two equivalent ways
to expose it to the MPC:

1. **Read the light entity directly** (simplest — no HA config at all). Put the office light's
   entity id (e.g. `light.nicolas_office`) in `config/house.yaml` as the room's
   `presence_entity`. The scheduler treats `on` = occupied, `off` = empty — a `light` and a
   `binary_sensor` both report `on`/`off`, so the MPC side needs no special-casing.
2. **A template `binary_sensor`** if you want a stable, explicitly-named presence entity and/or
   a debounce so a brief light-off (or someone toggling it) doesn't immediately drop the room.
   Add to `configuration.yaml`:

   ```yaml
   template:
     - binary_sensor:
         - name: "Presence Nicolas Office"
           unique_id: presence_nicolas_office
           device_class: occupancy
           state: "{{ is_state('light.nicolas_office', 'on') }}"
           delay_off: "00:15:00"        # stay 'occupied' 15 min after the light goes off
   ```

   This yields `binary_sensor.presence_nicolas_office` (`on` = occupied, `off` = empty); use
   that as the `presence_entity` instead of the raw light.

- **Naming / config**: declare the chosen entity id under nicolas_office in `config/house.yaml`
  (e.g. a `presence_entity:` key). Rooms with no `presence_entity` are treated as always
  occupied, so the eight-room behaviour is unchanged until you add one.
- **MPC side**: the scheduler reads the entity via REST `/api/states/<entity_id>` each tick; the
  existing `HA_TOKEN` already has read access. `off` → swap that room to the wide 65–85 °F
  "don't care" band; `on` (or entity missing/unavailable) → keep the scheduled band.
- **Light-as-proxy caveat**: a light left on reads as occupied (room stays cooled needlessly) and
  working in the dark reads as empty (cooling stops). The `delay_off` in option 2 only smooths
  brief gaps; for hard cases the away switch (item 8) or a manual override (item 7) is the escape
  hatch, and a motion sensor remains the more accurate long-term option.

**Status: implemented 2026-06-13.** `presence_entity: binary_sensor.presence_nicolas_office`
is declared under nicolas_office in `house.yaml` (only that room for now).
`controller.get_presence(house)` returns `{room_id: occupied_bool}` for rooms with a
`presence_entity`, **failing safe to occupied** on any read error or `unavailable`/`unknown`
state. `schedule.resolve_targets_for_rooms(..., unoccupied=…)` drops each empty room to the wide
`WIDE_BAND` (65–85°F), overriding the schedule/static band but yielding to away mode (when away,
presence is not even read). `scheduler.py` and `dry_run.py` read presence each
tick and log per-room occupied/UNOCCUPIED transitions via the normal logger. As with item 8, no
`mpc_decision_log.csv` column is added (would break the existing log header / live-analysis read); the
structured `user_inputs.log` trail is item 10 (now done). Rooms without a `presence_entity` are
omitted from the dict and treated as always occupied, so the other seven rooms are unchanged.

---

## 10. Logging of User Actions

**Status: implemented.** User-driven state changes are logged to `thermal_control/logs/user_inputs.log`
(CSV, same directory as `mpc_decision_log.csv`) so they can be correlated with comfort and energy
data during post-run analysis.

- **File**: `thermal_control/logs/user_inputs.log` — CSV with columns `timestamp`, `event`, `room`, `value`.
- **Events logged**: `away_activated`, `away_deactivated`, `presence_occupied`, `presence_unoccupied`,
  `override_activated`, `override_changed`, `override_expired`, `override_cancelled`.
- The scheduler appends a row on every state change via `_append_user_event()`; the file is not
  rotated (same retention as the decisions log).

---

## 11. Mount Full PROD folder

*(Deferred — revisit after items 6–10 are shipped.)*

Instead of mounting only the logs directory into the devcontainer, mount the entire
`Y:\thermal_controler` Windows host path so that config, weights, and logs are all accessible
for local analysis without manual copying.

Proposal: update `devcontainer.json` to add the volume mount.