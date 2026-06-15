"""
config_check.py
───────────────
Shared structural validation for the hot-reloaded config (house.yaml /
control.yaml).

Two consumers:
  • scheduler.py rejects a bad edit before it replaces the live config.
  • dashboard/app.py re-validates the on-disk config to decide whether to
    show a live "config broken" banner.

Kept dependency-free (plain dict checks) so importing it from either process
is cheap and has no side effects.
"""

# Keys the runtime indexes unconditionally — a syntactically valid file that
# drops one of these would otherwise blow up mid-tick, so we reject it first.
# Pure-syntax errors (missing commas/brackets/indent) are caught earlier, when
# the yaml is parsed.
REQUIRED_HOUSE_KEYS   = ("rooms", "ac_units", "location")
REQUIRED_CONTROL_KEYS = ("mpc", "targets")
REQUIRED_MPC_KEYS     = ("tick_minutes", "horizon_steps",
                         "setpoint_on_f", "setpoint_off_f")


def validate_config_structure(house, control):
    """
    Raise ValueError if the parsed config is missing structure the control loop
    depends on. Deliberately shallow — it guards the keys read without a guard;
    the HouseSimulator/BangBangMPC constructors are the deeper check for
    anything room/AC-specific.
    """
    if not isinstance(house, dict):
        raise ValueError("house.yaml: top-level YAML is not a mapping")
    if not isinstance(control, dict):
        raise ValueError("control.yaml: top-level YAML is not a mapping")
    for key in REQUIRED_HOUSE_KEYS:
        if key not in house:
            raise ValueError(f"house.yaml: missing required key '{key}'")
    if "timezone" not in house["location"]:
        raise ValueError("house.yaml: missing required key 'location.timezone'")
    if not house["rooms"]:
        raise ValueError("house.yaml: 'rooms' is empty")
    for key in REQUIRED_CONTROL_KEYS:
        if key not in control:
            raise ValueError(f"control.yaml: missing required key '{key}'")
    for key in REQUIRED_MPC_KEYS:
        if key not in control["mpc"]:
            raise ValueError(f"control.yaml: missing required key 'mpc.{key}'")
