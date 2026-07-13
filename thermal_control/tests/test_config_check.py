"""Tests for control/config_check.py — config structure validation."""

import pytest

from thermal_control import config_reload as cr
from thermal_control.control.config_check import validate_config_structure


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
