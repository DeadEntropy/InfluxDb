"""Tests for control/config_check.py — config structure validation."""

import pytest

from thermal_control import config_reload as cr
from thermal_control.control.config_check import (collect_band_warnings,
                                                  validate_config_structure)

OCCUPIED = {"min_f": 65, "max_f": 76}


def _cfg(room_value, room="nicolas_office", name="evening"):
    """Minimal (house, control) pair whose one schedule entry holds room_value."""
    house = {"rooms": [{"id": "nicolas_office",
                        "presence_entity": "binary_sensor.presence_nicolas_office"},
                       {"id": "kitchen"}]}
    control = {"targets": {
        "default":  {"min_f": 75, "max_f": 78},
        "schedule": [{"name": name, "time": "20:45", "rooms": {room: room_value}}],
    }}
    return house, control


def test_validate_config_structure_accepts_real_config():
    house, control = cr.load_configs()
    validate_config_structure(house, control)               # must not raise


@pytest.mark.parametrize("mangle, needle", [
    (lambda h, c: h.pop("rooms"),                  "rooms"),
    (lambda h, c: h.pop("ac_units"),               "ac_units"),
    (lambda h, c: h["location"].pop("timezone"),   "location.timezone"),
    (lambda h, c: h.__setitem__("rooms", []),      "empty"),
    (lambda h, c: c.pop("mpc"),                    "mpc"),
    (lambda h, c: c["mpc"].pop("setpoint_on_f"),   "mpc.setpoint_on_f"),
])
def test_validate_config_structure_rejects_missing_keys(mangle, needle):
    house, control = cr.load_configs()
    mangle(house, control)
    with pytest.raises(ValueError, match=needle):
        validate_config_structure(house, control)


def test_validate_config_structure_rejects_non_mapping():
    with pytest.raises(ValueError, match="house.yaml"):
        validate_config_structure(["not", "a", "dict"], {"mpc": {}})


# ── collect_band_warnings (NEXT_STEPS item 11) ──────────────────────────────
def test_real_config_has_no_band_warnings():
    house, control = cr.load_configs()
    assert collect_band_warnings(house, control) == []


@pytest.mark.parametrize("value", [
    {"min_f": 65, "max_f": 78},                        # flat
    {"occupied": OCCUPIED},                            # conditional, shorthand
    {"occupied": OCCUPIED, "unoccupied": {"min_f": 65, "max_f": 78}},
])
def test_clean_bands_produce_no_warnings(value):
    assert collect_band_warnings(*_cfg(value)) == []


def test_mixed_band_warns_and_names_the_flat_fallback():
    warnings = collect_band_warnings(*_cfg(
        {"min_f": 65, "max_f": 85, "occupied": OCCUPIED}))
    joined = " | ".join(warnings)
    assert "evening / nicolas_office" in joined
    assert "both a flat band and occupied/unoccupied" in joined
    assert "65–85°F" in joined          # tells the operator what is actually live


@pytest.mark.parametrize("value, needle", [
    ({"occuppied": OCCUPIED},     "no 'min_f'/'max_f'"),        # typo'd key
    ({"min_f": 65},               "missing 'max_f'"),
    ({"occupied": {"max_f": 76}}, "not a complete"),
    ("nonsense",                  "expected a band mapping"),
])
def test_invalid_band_warns(value, needle):
    joined = " | ".join(collect_band_warnings(*_cfg(value)))
    assert needle in joined
    assert "evening / nicolas_office" in joined


def test_conditional_band_without_a_presence_sensor_warns():
    # kitchen has no presence_entity → get_presence omits it → callers fail safe
    # to occupied → the 'occupied' branch would silently apply forever.
    warnings = collect_band_warnings(*_cfg({"occupied": OCCUPIED}, room="kitchen"))
    joined = " | ".join(warnings)
    assert "presence_entity" in joined and "kitchen" in joined


def test_conditional_outside_a_schedule_entry_warns():
    house, control = _cfg({"min_f": 65, "max_f": 78})
    control["targets"]["nicolas_office"] = {"occupied": OCCUPIED}   # static block
    control["targets"]["default"] = {"min_f": 75, "max_f": 78,
                                     "unoccupied": {"min_f": 65, "max_f": 85}}
    joined = " | ".join(collect_band_warnings(house, control))
    assert "targets.nicolas_office" in joined
    assert "targets.default" in joined
    assert "only supported inside a schedule entry" in joined


def test_collect_band_warnings_never_raises_on_junk():
    # It runs on every dashboard render and on every accepted reload; a structural
    # problem is the raiser's job, not this one's.
    for house, control in [({}, {}), ({}, {"targets": None}),
                           ({}, {"targets": {"schedule": "nope"}}),
                           ({}, {"targets": {"schedule": [None, {"rooms": 3}]}})]:
        assert collect_band_warnings(house, control) == []
