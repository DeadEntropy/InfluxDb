"""
Shared fixtures for the thermal_control test suite.

Tests are hermetic: no network, no live Home Assistant. HTTP-touching code in
ha_bridge/controller.py and control/forecast.py is exercised by monkeypatching
requests / _get_state (see the individual test modules).

Fixtures load the *real* config/ and model/weights/ so the simulator and MPC
are tested against the deployed thermal model, not a toy stand-in.
"""

import ast
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT        = Path(__file__).resolve().parent.parent      # thermal_control/
REPO_ROOT   = ROOT.parent                                 # repo root
WEIGHTS_DIR = ROOT / "model" / "weights"
HOUSE_YAML  = ROOT / "config" / "house.yaml"
CONTROL_YAML = ROOT / "config" / "control.yaml"

sys.path.insert(0, str(REPO_ROOT))

from thermal_control.model.simulate import HouseSimulator
from thermal_control.control.mpc import BangBangMPC


# ── Config fixtures ─────────────────────────────────────────────────────────
@pytest.fixture
def house_config():
    with open(HOUSE_YAML) as f:
        return yaml.safe_load(f)


@pytest.fixture
def control_config():
    with open(CONTROL_YAML) as f:
        return yaml.safe_load(f)


# ── Model fixtures ──────────────────────────────────────────────────────────
@pytest.fixture
def simulator(house_config):
    return HouseSimulator(WEIGHTS_DIR, house_config)


@pytest.fixture
def mpc(simulator, house_config, control_config):
    return BangBangMPC(simulator, house_config, control_config)


@pytest.fixture
def ac_ids(house_config):
    return [ac["id"] for ac in house_config["ac_units"]]


@pytest.fixture
def all_rooms(simulator):
    return list(simulator.rooms)


# ── HA environment ──────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fake_ha_env(monkeypatch):
    """Every test runs with dummy HA credentials so url/header building works
    even when the real _get_state is monkeypatched away."""
    monkeypatch.setenv("HA_URL", "http://ha.test")
    monkeypatch.setenv("HA_TOKEN", "test-token")


class FakeResponse:
    """Minimal stand-in for requests.Response."""
    def __init__(self, json_data=None, raise_exc=None):
        self._json = json_data or {}
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._json


@pytest.fixture
def fake_response():
    return FakeResponse


# ── preprocess loader ───────────────────────────────────────────────────────
# The numbered preprocess scripts (01_*, 02_*) run side-effecting module-level
# code on import (read CSVs, save files) and their filenames are not valid
# module identifiers. Extract the target function via AST so we can unit-test
# the real source without triggering the script body.
def load_function_from_script(path: Path, name: str):
    src  = path.read_text()
    tree = ast.parse(src)
    func = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"pd": pd, "np": np}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


@pytest.fixture
def load_script_func():
    return load_function_from_script
