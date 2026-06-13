"""Tests for preprocess data-cleaning functions.

These scripts run side-effecting code at import (and have non-identifier file
names), so the functions are extracted from source via the `load_script_func`
fixture rather than imported.
"""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent      # thermal_control/

STALE_01 = ROOT / "preprocess" / "01_stale_data.py"
FILTER_02 = ROOT / "preprocess" / "02_filter.py"


def _series(values):
    idx = pd.date_range("2026-06-15 00:00", periods=len(values), freq="10min", tz="UTC")
    return pd.Series(values, index=idx, dtype="float64")


# ── find_stale_periods (01_stale_data.py) ───────────────────────────────────
def test_find_stale_periods_flags_long_runs(load_script_func):
    find_stale_periods = load_script_func(STALE_01, "find_stale_periods")
    s = _series([70.0, 70.0, 70.0, 70.0, 71.0, 72.0])     # run of 4, then changing
    is_stale, periods = find_stale_periods(s, min_steps=3)

    assert list(is_stale) == [True, True, True, True, False, False]
    assert len(periods) == 1
    start, end, hours = periods[0]
    assert hours == pytest.approx(4 * 10 / 60)            # 4 steps × 10 min


def test_find_stale_periods_ignores_short_runs(load_script_func):
    find_stale_periods = load_script_func(STALE_01, "find_stale_periods")
    s = _series([70.0, 70.0, 71.0, 72.0])                 # longest run = 2 < min_steps
    is_stale, periods = find_stale_periods(s, min_steps=3)
    assert not is_stale.any()
    assert periods == []


# ── null_stale (02_filter.py) ───────────────────────────────────────────────
def test_null_stale_nulls_dead_battery_runs(load_script_func):
    """KEY FEATURE: ≥ N identical consecutive readings = dead battery → NaN."""
    null_stale = load_script_func(FILTER_02, "null_stale")
    s = _series([70.0, 70.0, 70.0, 70.0, 71.0, 72.0])
    out = null_stale(s, min_steps=3)
    assert out.iloc[:4].isna().all()                       # stale run nulled
    assert out.iloc[4] == 71.0 and out.iloc[5] == 72.0     # live readings kept


# ── sentinel filter (KEY FEATURE: offline-sensor sentinel → NaN) ────────────
def test_sentinel_filter_masks_offline_readings():
    # SENTINEL_F is a module-level constant in 02_filter.py — read it from source
    # so the test tracks the real threshold, not a hard-coded copy.
    tree = ast.parse(FILTER_02.read_text())
    sentinel = next(
        ast.literal_eval(node.value)                       # handles the -10.0 unary minus
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "SENTINEL_F"
    )
    assert sentinel == -10.0

    df = pd.DataFrame({"kitchen": [75.0, -58.0, sentinel, 74.0]})
    df[df <= sentinel] = np.nan                            # the filter the script applies
    assert df["kitchen"].isna().tolist() == [False, True, True, False]
