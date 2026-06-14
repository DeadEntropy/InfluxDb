# Code Review — Issues & Proposed Improvements

Reviewed: all of `thermal_control/` (configs, preprocess, model, control, ha_bridge,
scheduler, analysis) against the spec (`home_thermal_control.md`) and the READMEs.
Date: 2026-06-10.

Overall: the pipeline is clean, well-documented, and the bang-bang pivot is well
argued. The issues below are ordered by priority. Each item has a checkbox so you
can tick them off as you address them. Open questions for you are at the bottom —
a few items depend on your answers.

---

## P0 — Safety & correctness (fix before running unattended)

### 1. No failsafe if the scheduler dies while ACs are forced ON
- [x] `scheduler.py` — **Partially fixed 2026-06-10** (in-process exit paths)

If the process crashes, is killed, or the machine reboots after a tick wrote
`65°F` setpoints, the ACs stay forced ON indefinitely — cooling toward 65°F with
nothing to stop them. The physical control loop you deliberately bypassed is no
longer there to save you.

**Fix applied (layer 1 — in-process):**
- `_write_safe_setpoints()` writes 76°F to all AC units. 76°F hands control back
  to each thermostat's own sensor — neither forcing ON nor forcing OFF.
- SIGTERM registered via `signal.signal` to raise `SystemExit`, which escapes
  the inner `except Exception` tick handler.
- `while True` wrapped in `try / except (KeyboardInterrupt, SystemExit) / finally`
  — the `finally` calls `_write_safe_setpoints()` on every exit path: Ctrl-C,
  `systemctl stop`, unhandled exception escaping the tick.

**Still needed (layer 2 — HA watchdog):**
Add an HA automation that resets all climate entities to 76°F if a heartbeat
`input_datetime` helper (updated each tick) is older than ~30 min. This covers
power loss and kernel OOM kill, which `finally` cannot handle.

### 2. Constant-action horizon causes overshoot bias and possible chattering
- [ ] `control/mpc.py:120-131`

Each of the 8 combos is held constant for the entire 3-hour horizon. The cost of
"AC on" is therefore the cost of *3 hours* of continuous cooling — which, with a
2°F-wide band (75–77) and quadratic too-cold penalty, heavily penalises any combo
that overshoots by the end of the horizon. Consequences:

- The MPC delays cooling until rooms are well above `max_f` (because 3h-ON looks
  worse than 3h-OFF until discomfort is large), then flips — a limit cycle biased
  to the warm side of the band, re-deciding every 10 min (chattering risk).
- Pre-cooling strategies (run now because it'll be hotter at 5pm) can never win,
  which defeats part of the purpose of using a forecast.

**Proposed fix:** enumerate two action blocks instead of one — e.g. combo A for
the first 3 steps (30 min), combo B for the remaining 15 steps. That's 8×8 = 64
rollouts (trivially cheap; today you do 8), apply only block A (receding horizon
as today). This lets the optimiser express "cool hard for 30 min, then stop",
which removes the overshoot bias. Scale the energy term by actual on-steps per
block (see issue 12).

A cheaper stopgap: shorten the horizon to ~60 min, which limits how much
hypothetical overshoot a constant action can accumulate.

**Discussion 2026-06-12** — concern re-raised ("barrier risk": oscillation around
a single target temp). Clarified that the comfort *band* (75–77°F, zero cost
inside) already provides a 2°F deadband, so oscillation around a single setpoint
can't happen; the real limit-cycle mechanism is the constant-action horizon
described above (ON/OFF costs near-tie at the warm band edge). A faster tick was
also considered and rejected: `tick_minutes` must equal the model timestep (the
RidgeCV models predict ΔT per 10-min step, training data is on a 10-min grid),
so going to 5 min requires reprocessing + retraining — and a faster tick halves
the minimum compressor cycle, making issue 14 worse. Agreed plan, in order:
1. Measure first: 24h shadow run, check switches/AC/day (shadow-analysis skill,
   decision-stability section) before adding mechanism.
2. Anti-short-cycling guard (issue 14).
3. Two-block enumeration (the proposed fix above).

### 3. Simulator switches ACs on *room* sensor temps, not AC controller sensor temps
- [ ] `model/simulate.py:54-68`

`_derive_ac_on()` compares the simulated **room** temperature of `sensor_room`
against the setpoint. The physical controller compares **its own sensor**
(`ac_sensor_entity` — wall thermostat, whole-°F resolution), which the spec
explicitly warns about (`home_thermal_control.md` Phase 1: "Always use
`ac_sensor_temp` in the switching model, not the room sensor"). The
`ac_sensor_<ac>` columns exist in `train.csv`/`val.csv` (with lags) but are never
used.

**Impact today:**
- In production bang-bang mode the impact is small — 65/84 are chosen to be
  outside any realistic sensor reading, so switching is forced regardless of
  which sensor you use. (But see open question Q4 on the 84°F margin for the
  kitchen.)
- Pass 2 validation **is** affected: it replays historical mid-range setpoints
  (~75°F), where a 1–2°F offset between the Zigbee room sensor and the
  thermostat's own sensor flips switching decisions. The reported Pass 2 numbers
  may not mean what they appear to mean.

**Proposed fix:** estimate a per-AC offset from data
(`median(ac_sensor_<ac> − T_<sensor_room>)` on the training set), apply it (and
round to whole °F) inside `_derive_ac_on`, and re-run Pass 2. If the offsets
turn out to be near zero, document that and keep the simple code with a comment.

### 4. Sensor-failure fallback fabricates a comfortable temperature
- [x] `scheduler.py`, `control/mpc.py` — **Fixed 2026-06-10**

When a room sensor is stale >30 min, `fill_missing()` substitutes 74°F — right
at the comfort band. A bedroom with a dead battery will read as "comfortable"
forever, and the MPC will simply never cool it. This is the exact failure mode
(dead Zigbee batteries) your own preprocessing showed is common — master_bedroom
was dark for a month in May 2026.

**Proposed fix:** instead of fabricating a temperature, exclude the room from
the cost function for that tick (equivalent to a wide band), log a `WARNING`
every tick it's missing, and surface it (HA notification — you already have HA).
Fabricated data should never silently steer a controller.

### 5. ACs may not be in a cooling-capable mode — `set_temperature` alone doesn't guarantee cooling
- [x] `ha_bridge/controller.py`, `scheduler.py` — **Fixed 2026-06-10**

`climate/set_temperature` sets the target but does not change `hvac_mode`. If a
unit is `off` (power blip, HA restart default), writing 65°F does nothing and the
MPC believes it commanded cooling. There is no read-back verification anywhere
in the loop.

**Q1 answered:** units are always operated in `cool` mode by changing the target
temperature; they are never manually turned off. Mode drift can only occur from
HA restarts or power blips, so correction is a rare safety net, not a routine action.

**Fix applied — three changes:**

1. `get_ac_states()` now returns `hvac_mode` (the entity `state` field) alongside
   `hvac_action`. No extra API call — same response was already fetched.

2. `scheduler.py` calls `get_ac_states()` at the start of every tick and logs
   `mode`, `action`, `sensor temp`, and `setpoint` for each unit. This makes mode
   drift immediately visible in the log history. After solving the MPC, any ON-
   commanded unit whose `hvac_mode != "cool"` is corrected via the new
   `set_hvac_mode()` helper before setpoints are written. In normal operation
   (always `cool`) this block logs nothing and issues no extra calls.

3. `apply_setpoints()` now reads back each climate entity's `temperature`
   attribute after the write loop and logs a `WARNING` on any mismatch. This
   catches silent HA failures where the HTTP call returns 200 but the entity
   state was not actually updated.

---

## P1 — Bugs

### 6. `forecast.py` returns the forecast for *midnight onwards*, not *now onwards*
- [x] `control/forecast.py:49-58` — **Fixed 2026-06-10**

Open-Meteo's `hourly.temperature_2m` array starts at 00:00 local time of the
current day. The code takes `temps_c[:hours_needed]` and interpolates assuming
index 0 = now. With `use_forecast: true`, at 3pm the MPC would receive the
00:00–05:00 temperature profile. Latent today only because `use_forecast` is
`false`.

**Fix applied:** parse `hourly.time` from the response, find the first entry
≥ the current hour (wall-clock comparison), and slice
`[now_idx : now_idx + hours_needed]`. Gracefully returns `None` if the slice
is too short. The `forecast_days: 2` param already provides enough headroom.
A unit test with a mocked response is still TODO.

### 7. Validation pairs/windows silently span data gaps
- [x] `model/simulate.py:135-141, 161-176` — **Fixed 2026-06-10**

`val.csv` had NaN rows dropped in `04_merge.py`, so its index has holes. Pass 2a
treats `iloc[i]` → `iloc[i+1]` as one 10-min step, and Pass 2b builds 12-step
windows by position — across a gap, the "10-minute step" can actually be hours.
Same for the full-rollout plot. This corrupts the reported MAEs.

**Fix applied:** Pass 2a skips pairs where `val.index[i+1] − val.index[i] ≠ 10 min`
and reports how many were skipped. Pass 2b checks all diffs within each window and
skips non-contiguous ones, also reporting the count.

**Re-run results (2026-06-10):** 105 step-pairs skipped in Pass 2a; 66 windows
skipped in Pass 2b. Updated Pass 2b MAEs:

| Room | MAE (°C) old | MAE (°C) new | Δ |
|---|---|---|---|
| anna_office   | ≤ 0.52 | 0.309 | improved (gap resets were masking drift) |
| dining_room   | ≤ 0.52 | 0.234 | improved |
| family_room   | ≤ 0.52 | 0.203 | improved |
| kids_bedroom  | ≤ 0.52 | 0.441 | improved |
| kitchen       | ≤ 0.52 | 0.142 | improved |
| master_bedroom| 0.510  | 0.526 | slightly worse (gaps were hiding true drift) |
| nicolas_office| ≤ 0.52 | 0.222 | improved |
| tv_room       | ≤ 0.52 | 0.277 | improved |

All rooms remain well within the 1.5°C target. The previous numbers were
mildly optimistic because cross-gap windows reset state to observed values,
adding many near-zero-error data points.

### 8. A failed setpoint write aborts the remaining writes
- [x] `ha_bridge/controller.py:108-117` — **Fixed 2026-06-10**

`apply_setpoints` calls `raise_for_status()` inside the loop. If the first write
fails, the tick aborts (scheduler catches it) and the other two units keep their
*previous* setpoints — leaving a combo the MPC never chose (e.g. bedroom ON from
last tick + living OFF from this tick).

**Fix applied:** each unit's `requests.post` + `raise_for_status()` is now wrapped
in its own `try/except`. All three writes are always attempted. Failures are
collected and a single `RuntimeError` is raised at the end listing which units
failed, so the scheduler loop logs it and the next tick repairs the state.

### 9. `requirements.txt` is missing the thermal_control dependencies
- [x] `requirements.txt` (repo root) — **Fixed 2026-06-10**

The code imports `scikit-learn`, `joblib`, `pyyaml`, `requests`, `scipy`,
`seaborn` — none are in `requirements.txt` (it still reflects the old InfluxDB
notebooks). A fresh environment cannot run the project. Conversely, `cvxpy` and
`schedule` from the spec's dependency list are *not* needed anymore (bang-bang
enumeration replaced MILP; stdlib `time.sleep` replaced `schedule`).

**Fix applied:** created `thermal_control/requirements.txt` with all runtime
dependencies, pinning `scikit-learn>=1.4,<2.0` (joblib weights are version-
sensitive). `cvxpy` and `schedule` are absent (no longer used). `model/fit.py`
now records `sklearn.__version__` in `metrics.json` at fit time so the version
is auditable alongside each set of weights.

---

## P2 — Design gaps & robustness improvements

### 10. The documented schedule feature doesn't exist in code or config
- [x] `control/schedule.py`, `control/mpc.py`, `scheduler.py`, `config/control.yaml` — **Fixed 2026-06-10**

Both READMEs describe `targets.schedule` (time-of-day windows, midnight
wrap-around, priority order static-override > schedule > default, "the scheduler
resolves the active entry before each solve"). None of it is implemented:
`control.yaml` has no `schedule` block, `BangBangMPC` reads targets once at
init, and `scheduler.py` never touches `mpc.targets`. Night-time behaviour today
is just the static 75–77 band for every room, including the offices the README
says should be ignored at night.

**Proposed implementation** (matches the documented design):
1. Add the `schedule` block to `control.yaml` (the README example is ready to
   paste).
2. New function `resolve_targets(control_cfg, now) -> {room: {min_f, max_f}}`
   implementing the documented priority order — pure function, easy to unit-test
   (include the midnight wrap-around case).
3. Change `BangBangMPC.solve(state, outdoor_series, targets)` to take targets
   per call (keeps the MPC stateless w.r.t. the schedule, as documented).
4. Watch out: the current override scan (`mpc.py:75-77`) treats *every*
   non-`default` key under `targets:` as a room override — a `schedule:` key
   would be silently swallowed into it. Filter to known room ids.

### 11. No persistent record of MPC decisions
- [x] `scheduler.py`, `control/mpc.py` — **Fixed 2026-06-10**

`explain()` used `print()` (bypassing logging), and nothing was written to disk,
making step 10 ("monitor for 1 week, tune `energy_weight`") impossible.

**Fix applied:**

- `explain()` now builds and **returns a string** instead of printing. The
  scheduler logs it via `logger.info(mpc.explain())`, keeping it in the same
  stream as all other tick output. Also fixed the hardcoded `10 min/step` — now
  uses `self.tick_minutes` from config (also resolves the issue 19 item).

- New `decision_record()` method on `BangBangMPC` returns a flat dict with every
  field needed for analysis: `T_<room>`, `on_<ac>`, `sp_<ac>`, `cost_chosen`,
  `cost_<3-bit-combo>` for all 8 combinations, `proj_<room>` at horizon end.

- `_append_decision_log(record)` in `scheduler.py` appends one CSV row per tick
  to `thermal_control/logs/mpc_decision_log.csv`, creating the file and header on first
  run. Write failures in the log helper are caught and logged as `WARNING` so
  they never crash the control loop.

- The setpoint apply step is now wrapped in its own try/except so a write failure
  sets `write_ok=False` in the record but still produces a log row. Previously a
  write failure would skip the log entirely.

**CSV columns per row:** `timestamp`, `T_outdoor`, `write_ok`, then all
`decision_record()` fields — 8 room temps, 3 on/off flags, 3 setpoints,
`cost_chosen`, 8 combo costs, 8 projected room temps. Sufficient to plot duty
cycles, replay decisions offline, and tune `energy_weight`.

### 12. Energy term doesn't scale with the horizon
- [ ] `control/mpc.py:102-106`

Discomfort is summed over 18 steps; the energy term is added once per combo. The
effective comfort/energy trade-off therefore changes whenever you change
`horizon_steps`, which makes `energy_weight` non-portable and will bite when you
tune it (step 10) or change the horizon (issue 2). Multiply by the number of
ON-steps in the rollout (which becomes meaningful once actions can vary within
the horizon).

### 13. Forced multi-hour ON is far outside the training distribution
- [ ] `model/fit.py`, `control/mpc.py`

Historical duty cycles are ~6–19 min/h; the model never saw multi-hour
continuous cooling, and a linear model extrapolates a *constant* °F-per-step
cooling rate forever (real cooling rate decays as the room approaches supply-air
temperature). Rollouts under ON-for-3h are extrapolation; predicted end temps
are likely too cold, which feeds the overshoot bias of issue 2.

**Proposed mitigations:** clamp simulated temps to a sane floor (e.g. 65°F);
prefer short action blocks (the issue 2 fix reduces reliance on long
extrapolation); once running, compare predicted vs realised temps from the
decision log (issue 11), and consider refitting after a few weeks — closed-loop
operation will generate much richer excitation data than the historical logs.

### 14. No anti-short-cycling protection
- [ ] `control/mpc.py` / `scheduler.py`

Nothing prevents ON→OFF→ON every 10 minutes if costs hover near a tie (likely
around band edges, see issue 2). Compressors dislike short cycles.

**Proposed fix:** minimum on-time and off-time (e.g. 20–30 min = 2–3 ticks)
enforced in the scheduler: if a unit switched recently, restrict the enumeration
to combos that keep its current state. Optionally also a switching penalty
`switch_weight · Σ|combo − previous_combo|` in the cost as a softer version.

**Discussion 2026-06-12** — confirmed as the right place to fix chattering (see
issue 2 addendum for the agreed order: shadow-run measurement first). A
temperature-hysteresis alternative (cool to `target − 0.5°F` before releasing)
was considered and rejected: it duplicates the deadband the comfort band already
provides and fights the MPC's own optimisation. Hysteresis in *time* (min on/off
or switching penalty) is what actually protects the compressor. Note the 10-min
tick is currently the only de-facto short-cycling protection — revisit this
issue before any change to `tick_minutes`.

### 15. Outdoor temperature fallbacks are weak
- [ ] `scheduler.py:107, 129-135`

Two related problems: the outdoor cache never expires (a value cached at startup
can be reused days later), and the ultimate fallback is `FALLBACK_TEMP_F = 74` —
unrealistically cool for a Florida summer, which would make every rollout
underestimate heat ingress and choose too little cooling.

**Proposed fix:** give the outdoor cache its own staleness limit (a few hours is
fine); beyond it, fall back to a simple monthly climatology (month → typical
temp table for Davie) rather than 74, and log loudly.

### 16. `recalibrate.py` doesn't exist and the pipeline needs manual CSV exports
- [ ] `README.md:88`, spec Phase 9

The monthly refit script is referenced but unwritten, and the preprocess
pipeline starts from manually exported CSVs (`homeassistant_temp.csv`,
`homeassistant_states.csv`), so recalibration can't be automated as-is. You
already have InfluxDB ingestion in this repo (`fetch_states.py`,
`influx_kwh.py`) — wiring the preprocess steps to query Influx directly would
make `recalibrate.py` a thin orchestration of existing steps plus the validation
gate from the spec (save new weights only if Pass 1/Pass 2 pass; archive old
weights with a timestamp).

### 17. Hold-out methodology nits
- [ ] `model/fit.py:17-19, 108`, `model/simulate.py`

Three small things that together overstate confidence:
- `fit.py`'s docstring calls Pass 1 a "rollout", but it's one-step-ahead
  prediction on observed lags. With 10-min steps the own-lag carries ~all the
  signal, so 0.02–0.08 °C MAE mostly measures persistence. Report the
  **persistence baseline** (`T̂(t) = T(t−1)`) next to model MAE so the model's
  actual skill is visible.
- `RidgeCV(cv=5)` uses standard K-fold on time-series rows (look-ahead within
  folds). Low risk for ridge-alpha selection, but `TimeSeriesSplit` is a
  drop-in.
- The 2-week hold-out is June-only; the model has never been validated on a
  mild-season regime. Fine for now (summer is what matters), but worth a second
  validation window before trusting it in December.

### 18. Feature/NaN handling assumes clean inputs
- [ ] `model/fit.py:101-109`, `model/simulate.py:83-92`

`fit.py` does no `dropna()` of its own — it works only because `04_merge.py`
dropped NaN rows. If anyone reorders the pipeline or adds a feature column, the
fit crashes. Similarly `simulate.step()` builds rows with
`state.get(room, np.nan)` — a missing room produces NaN and `model.predict`
raises mid-rollout.

**Proposed fix:** `dropna()` on `features + [target]` in `fit.py` with a logged
row count; in `simulate.step()`, assert all required state keys are present
(the scheduler's `fill_missing` already guarantees it — make the contract
explicit).

---

## P3 — Minor / cosmetic

### 19. Assorted small items — **All fixed 2026-06-10**

- [x] `scheduler.py` — `datetime.utcnow()` replaced with `datetime.now(timezone.utc)`
  in both `fill_missing()` and the outdoor-cache update. The sentinel `datetime.min`
  is now `datetime.min.replace(tzinfo=timezone.utc)` so comparisons remain consistent.
  Timestamps throughout are now uniformly UTC.

- [x] `control/mpc.py:194` — hardcoded `10 min/step` already fixed as part of
  issue 11 (now uses `self.tick_minutes` from config).

- [x] `control/mpc.py:77-81` — `dict(overrides.get(room, default))` now copies
  the default dict for each room instead of sharing the same object, so per-room
  target mutations (e.g. from the schedule feature) cannot bleed across rooms.

- [x] `dry_run.py:44` — `if outdoor else` → `if outdoor is not None`. Treats
  a legitimate 0.0°F reading correctly.

- [x] `model/fit.py:54` — deleted `r["sensor_entity"] != "null"`. YAML `null`
  loads as Python `None`; the preceding `r.get("sensor_entity")` truthiness check
  already handles it.

- [x] `preprocess/03_ac_states.py:98-106` — both `ffill()` calls at the merge
  and reindex steps are now `ffill(limit=18)` (3 hours at 10-min resolution).
  A longer HA outage becomes NaN and is dropped in `04_merge.py` rather than
  silently propagating frozen AC states.

- [x] `ha_bridge/controller.py` — new `check_sensor_units(house_config)` function
  reads `unit_of_measurement` from each room sensor's attributes at startup and
  logs `WARNING` for anything that isn't `"°F"`. Called once from `run()` before
  the main loop.

- [x] Weights portability — sklearn version already recorded in `metrics.json`
  as part of issue 9 fix.

- [x] `dry_run.py` — now imports and uses `build_outdoor_series()` from
  `control/forecast.py` instead of duplicating the fallback/forecast logic.
  `mpc.explain()` now correctly calls `print(mpc.explain())` since `explain()`
  returns a string (fixed as part of issue 11). `build_outdoor_series()` is the
  single source of truth exercised by both the live scheduler and the dry run.

---

## Open questions for Nicolas

Answers affect the fixes above — happy to discuss any of them.

- **Q1 (issue 5):** How are the three AC units normally operated — permanently
  in `cool` mode, in `auto`, or switched off seasonally / by remote? This
  decides whether the bridge must manage `hvac_mode` or only verify it. (If
  they're in `auto`, note that a 65°F setpoint could in principle trigger
  *heating* logic on some units — worth confirming it behaves as cool-only.)
- **Q2 (issue 10):** Is the schedule feature "documented ahead of
  implementation" (next on your list — keep the docs, implement it), or stale
  docs to cut?
- **Q3:** `master_bedroom` and `kids_bedroom` have **no adjacency edges at all**
  (their models are just own-lag + AC + outdoor). Do they share a wall or
  hallway with each other (or with `anna_office`)? Same question for
  `dining_room` ↔ `family_room`, which have no edge. If yes, adding the edges
  and refitting is cheap and may help the bedroom models (currently the worst
  performers).
- **Q4:** Is 84°F a safe OFF setpoint for the *kitchen* thermostat (living_ac's
  sensor)? Your EDA notes cooking-heat plateaus on the kitchen Zigbee sensor; if
  the thermostat's own sensor also spikes during cooking, an "OFF" command could
  still trigger cooling. Worth checking the historical max of
  `ac_sensor_living_ac` in the states data.
- **Q5:** Do all three thermostats accept setpoints of 65 and 84? HA climate
  entities expose `min_temp`/`max_temp` — if a unit clamps at, say, 68, the
  forced-ON logic still works but the code should know the real bounds.
