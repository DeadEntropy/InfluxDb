"""
dry_run.py
──────────
Reads live state from Home Assistant, solves the MPC, and prints
the optimal setpoints — without writing anything to HA.

Run from the repo root:
    cd /workspaces/InfluxDb
    python thermal_control/dry_run.py
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yaml
from thermal_control.model.simulate   import HouseSimulator
from thermal_control.control.mpc      import BangBangMPC
from thermal_control.control.forecast import build_outdoor_series
from thermal_control.control.schedule import resolve_targets_for_rooms
from thermal_control.ha_bridge        import controller as ha

FALLBACK_TEMP_F = 74.0

with open(ROOT / "config" / "house.yaml") as f:
    house = yaml.safe_load(f)
with open(ROOT / "config" / "control.yaml") as f:
    control = yaml.safe_load(f)

sim = HouseSimulator(ROOT / "model" / "weights", house)
mpc = BangBangMPC(sim, house, control)

# ── Read live state ───────────────────────────────────────────────────────
room_temps = ha.get_room_temps(house)
print("Live room temps:")
for r, t in sorted(room_temps.items()):
    print(f"  {r:<26} {t:.2f}°F")

outdoor = ha.get_outdoor_temp(house)
print(f"Outdoor: {outdoor}°F" if outdoor is not None else "Outdoor: unavailable (using fallback)")

ac_states = ha.get_ac_states(house)
print("\nAC states:")
for ac_id, s in ac_states.items():
    print(f"  {ac_id:<15}  sensor={s['ac_sensor_temp_f']}°F  "
          f"setpoint={s['setpoint_f']}°F  action={s['hvac_action']}")

# ── Resolve the active comfort bands (schedule + away + presence + override) ─
# One-shot snapshot: any thermostat card whose target differs from the scheduled
# upper bound is shown as an active override; the duration/expiry lifecycle and
# the card write-back only run in the live scheduler.
now_local  = datetime.now(tz=ZoneInfo(house["location"]["timezone"]))
away       = ha.get_away_mode(control)
presence   = {} if away else ha.get_presence(house)
unoccupied = {r for r, occ in presence.items() if not occ}
overrides  = {}
if not away:
    scheduled = resolve_targets_for_rooms(control, sim.rooms, now_local)
    overrides = {r: t for r, t in ha.get_room_targets(house).items()
                 if t != scheduled[r]["max_f"]}
targets    = resolve_targets_for_rooms(control, sim.rooms, now_local,
                                       away=away, unoccupied=unoccupied,
                                       override_targets=overrides)
print(f"\nAway mode: {'ON — holiday bands' if away else 'off'}")
if presence:
    print("Presence: " + ", ".join(f"{r}={'occupied' if occ else 'EMPTY'}"
                                    for r, occ in sorted(presence.items())))
if overrides:
    print("Overrides: " + ", ".join(f"{r}→{t}°F" for r, t in sorted(overrides.items())))

# ── Fill missing sensors with fallback ───────────────────────────────────
state = {}
print("\nState passed to MPC:")
print(f"  {'Room':<26}  {'Temp':>7}  {'Target':>12}  Source")
print("  " + "─" * 62)
for room_id in sim.rooms:
    tgt = targets[room_id]
    target_str = f"{tgt['min_f']}–{tgt['max_f']}°F"
    if room_id in room_temps:
        state[room_id] = room_temps[room_id]
        print(f"  {room_id:<26}  {state[room_id]:>6.1f}°F  {target_str:>12}  live")
    else:
        state[room_id] = FALLBACK_TEMP_F
        print(f"  {room_id:<26}  {FALLBACK_TEMP_F:>6.1f}°F  {target_str:>12}  fallback")

# ── Build outdoor series for MPC horizon ─────────────────────────────────
T_out = outdoor if outdoor is not None else FALLBACK_TEMP_F
outdoor_series, outdoor_desc = build_outdoor_series(T_out, house, control)
print(f"Outdoor series: {outdoor_desc}")

# ── Solve MPC (no writes) ─────────────────────────────────────────────────
setpoints = mpc.solve(state, outdoor_series, targets=targets)

print("\nOptimal setpoints (DRY RUN — nothing written to HA):")
for ac_id, sp in setpoints.items():
    label = "ON" if sp == control["mpc"]["setpoint_on_f"] else "OFF"
    print(f"  {ac_id:<15}  {sp}°F  ({label})")

print(mpc.explain())
