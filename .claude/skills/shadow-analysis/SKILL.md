---
name: shadow-analysis
description: Analyze a thermal_control shadow-run log (shadow.csv) — MPC vs real thermostat agreement, comfort-band violations, decision stability, cost evolution, and a 3-panel MPC-vs-actual plot. Use when the user asks to analyze, review, or plot a shadow run.
---

# Shadow run analysis

Analyze a log produced by `thermal_control/shadow_run.py` (one row per minute:
sensor temps, the MPC's desired AC states, the real thermostats' actual
actions, and the cost of all 8 AC combinations).

## How to run

From the repo root:

```bash
python .claude/skills/shadow-analysis/analyze_shadow.py [path/to/shadow.csv]
```

- Default log path is `shadow.csv` at the repo root; fresh logs land in
  `thermal_control/logs/shadow.csv` on the server (see CLAUDE.md deployment).
- Writes `shadow_mpc_vs_actual.png` (override with `--plot`, skip with
  `--no-plot`). Read the PNG and show/describe it to the user.
- Comfort bands are resolved per row from the `active_schedule` column +
  `thermal_control/config/control.yaml`, so schedule edits are picked up
  automatically.

## How to interpret the output

- **Low tick-level agreement is not necessarily bad.** Real units duty-cycle
  in short bursts; the MPC holds multi-hour decisions (its ON = "setpoint
  65°F, let the unit cycle itself"). Judge whether they agree on *when*
  cooling is needed, not tick parity.
- **The headline check is bedroom_ac vs bedroom temperatures.** Its controller
  sensor is in the dining room, so the real unit historically overcools the
  bedrooms (e.g. 67–69°F against a 74–76°F sleeping band) — the problem this
  project exists to fix. High "% below band" on bedrooms with actual cooling
  ~100% and MPC-on ~0% means the MPC is right, not broken.
- **A monotonically climbing `cost_chosen` is expected in shadow mode**: the
  too-cold penalty (`control/mpc.py` `_cost`) accumulates from the real
  controller's overcooling, and a cooling-only system can't fix it. It is a
  constant offset across all 8 combos, so decisions are unaffected — but the
  absolute cost is not comparable across time. A jump at a schedule
  transition (e.g. 06:00) is the new bands re-pricing the same temperatures.
- **Chosen-vs-runner-up margins near 0.04** = comfort tie broken by the
  energy weight (`energy_weight: 0.05` in control.yaml). By design.
  Decisions that matter have margins in the tens or hundreds.
- **MPC flips** should be few (a handful per night) and aligned with schedule
  transitions or band-edge crossings. Frequent flips = chattering, worth
  flagging.
- Rooms flagged `<-- check` spend >25% of the log outside their band — look
  at whether the MPC's plan addresses them or the real system caused it.
- `nicolas_office` is a known internal heat source: it warms overnight with
  all ACs idle.

Reference: a full worked analysis of the 2026-06-10 overnight run is in the
conversation that created this skill; CLAUDE.md gotchas apply (everything °F,
don't read the big CSVs whole).
