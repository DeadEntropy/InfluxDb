---
name: live-analysis
description: Analyze the live MPC decisions log (mpc_decision_log.csv) — AC on/off history, room temperatures vs comfort bands, cost margin, and two plots: (1) full-history on/off + temps with breach markers, (2) last 3h history + 3h forward projection under MPC vs all-off scenarios. Use when the user asks to analyze, review, or plot the live MPC run.
---

# Live MPC analysis

Analyze a log produced by `thermal_control/scheduler.py` (one row per 10-min tick:
sensor temps, MPC on/off decisions, setpoints written to HA, and 8-combo costs).

## How to run

From the repo root:

```bash
python .claude/skills/live-analysis/analyze_live.py [path/to/mpc_decision_log.csv]
    [--plot ac_onoff.png] [--plot2 ac_projection.png] [--no-plot]
```

- Default log: `remote_logs/mpc_decision_log.csv`
- Default plot 1: `ac_onoff.png` at repo root
- Default plot 2: `ac_projection.png` at repo root
- Comfort bands are resolved per tick from `thermal_control/config/control.yaml`;
  schedule transitions mid-log are handled correctly.
- Plot 2 requires `joblib` (and other `thermal_control/requirements.txt` deps) to
  load the fitted Ridge models for simulation. Run
  `pip install -r thermal_control/requirements.txt` if the import fails.

## Plot 1 — full history on/off + room temperatures (`ac_onoff.png`)

Three panels, one per AC zone. Each panel:
- **Left axis** (coloured): MPC on/off state as a step plot, with ON/OFF labels per tick.
- **Right axis** (grey shades): room temperatures for every room the AC directly covers.
  - `bedroom_ac`: master_bedroom, kids_bedroom, anna_office
  - `living_ac`:  dining_room, family_room, kitchen
  - `extension_ac`: tv_room, nicolas_office
- **Breach markers**: filled circles — red when a room is above its upper comfort bound,
  blue when below its lower bound. Bounds are schedule-aware (e.g. sleeping vs daytime).
- No bound lines are drawn; breaches speak for themselves.

## Plot 2 — 3h history + 3h projection (`ac_projection.png`)

Same three-panel layout, but time-windowed and extended:
- **History** (left of the dotted "now" line): last 3 hours (up to 18 ticks) of actual
  temperatures, same grey traces and red/blue breach markers as plot 1.
- **Left axis**: on/off history + dashed extension showing the current MPC decision
  held flat across the projection horizon.
- **MPC projection** (grey dashed): 3-hour forward rollout using `HouseSimulator` with
  the setpoints from the last logged tick held constant. Breach markers are circles (●).
- **All-off projection** (warm/orange dashed): same rollout but with all ACs forced OFF
  (setpoint = 84°F). Breach markers are diamonds (◆).
- The gap between MPC and all-off lines shows the cooling value of the current decision.

## How to interpret

- **bedroom_ac OFF with blue breach markers** = correct during morning warm-up. The MPC
  is letting overnight-overcooled bedrooms recover. Check that master_bedroom and
  kids_bedroom are trending toward the 75–77°F band over the 3h projection.
- **extension_ac oscillating** around nicolas_office's 74–76°F daytime upper edge is
  expected — nicolas_office is a known internal heat source. Brief ON/OFF cycles at
  the band edge are normal, not chattering.
- **living_ac brief ON pulses** = MPC pre-empting an overheating room. Verify the pulse
  brought dining_room/family_room back into band within 1–2 ticks.
- **MPC ≈ all-off projection** for a zone means that AC is genuinely not needed right now
  — the MPC agrees with doing nothing.
- **Large MPC vs all-off divergence** means the AC is actively holding the room in band;
  turning it off would cause a significant breach within the 3h window.
- **write_ok failures**: HA rejected a setpoint write — transient is fine, persistent
  means a network or auth issue.
- **cost margin**: median well above `energy_weight` (0.05) means decisions are
  comfort-driven. Margins near 0.05 = energy-tie-breaking only.

Reference: skill developed and validated against the 2026-06-12 live run (07:22–15:30 ET).
CLAUDE.md gotchas apply — everything in °F, don't read large CSVs whole.
