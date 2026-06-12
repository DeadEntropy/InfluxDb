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