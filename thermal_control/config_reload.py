"""
config_reload.py
─────────────────
Loads house.yaml/control.yaml and hot-reloads them when edited on disk
(volume-mounted in Docker, editable on the server). An edit is validated
(yaml syntax → required structure → simulator/MPC build) before it replaces
the live config; an invalid edit is rejected, the last-good config is kept,
and the failure is recorded to errors.log once per distinct broken edit.
"""

import logging
from pathlib import Path

import yaml

from thermal_control.model.simulate       import HouseSimulator
from thermal_control.control.mpc          import BangBangMPC
from thermal_control.control.config_check import (
    collect_band_warnings, validate_config_structure,
)

ROOT         = Path(__file__).parent
WEIGHTS_DIR  = ROOT / "model" / "weights"
HOUSE_YAML   = ROOT / "config" / "house.yaml"
CONTROL_YAML = ROOT / "config" / "control.yaml"
ERROR_LOG    = ROOT / "logs" / "errors.log"   # rejected config reloads land here
# Accepted-but-degraded edits (item 11) land here instead of errors.log: the
# dashboard date-stamps its "config rejected" banner from the last line of
# errors.log, so a warning written there would put a bogus "since" on an alarm
# about something else entirely.
WARNING_LOG  = ROOT / "logs" / "config_warnings.log"

logger = logging.getLogger(__name__)


def _make_file_logger(name, path, level):
    """
    Dedicated file logger for config feedback, kept separate from the main INFO
    stream so an operator who edits a yaml on the server has one file to tail
    for "did my edit take?". propagate=False so these lines don't also flood
    stdout; the main logger still records a copy.
    """
    fl = logging.getLogger(name)
    fl.setLevel(level)
    fl.propagate = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        fl.addHandler(handler)
    except OSError as exc:
        logger.warning(f"Could not open {path}: {exc}")
    return fl


config_error_logger   = _make_file_logger(
    "thermal_control.config_errors", ERROR_LOG, logging.ERROR)
config_warning_logger = _make_file_logger(
    "thermal_control.config_warnings", WARNING_LOG, logging.WARNING)

_last_reload_error_mtimes = None   # de-dupes repeated errors for one broken edit


def log_band_warnings(house, control):
    """
    Record presence-conditional band problems (item 11) for an edit that was
    *accepted*. Call sites are mtime-gated (initial load + each successful
    reload), so a standing problem is logged once per edit, not once per tick.
    """
    warnings = collect_band_warnings(house, control)
    for warning in warnings:
        config_warning_logger.warning(warning)
        logger.warning(f"Config warning — {warning}")
    return warnings


def load_configs():
    with open(HOUSE_YAML) as f:
        house = yaml.safe_load(f)
    with open(CONTROL_YAML) as f:
        control = yaml.safe_load(f)
    return house, control


def config_mtimes():
    return (HOUSE_YAML.stat().st_mtime, CONTROL_YAML.stat().st_mtime)


def maybe_reload(known_mtimes):
    """
    Detect on-disk changes to house.yaml/control.yaml (volume-mounted in
    Docker, editable on the server) and rebuild the simulator + MPC from
    the new config — also picking up any retrained weights in WEIGHTS_DIR.

    The edited config is validated (yaml syntax → required structure →
    simulator/MPC build) BEFORE it replaces the live config. Returns
    (house, control, sim, mpc, mtimes) on a successful, validated reload and
    None otherwise. On any failure (mid-edit/broken yaml, missing key, bad
    weights) the running controller keeps its last-good config; the failure is
    recorded in errors.log and retried on the next edit.
    """
    global _last_reload_error_mtimes
    try:
        mtimes = config_mtimes()
    except OSError as exc:
        logger.warning(f"Could not stat config files — keeping previous config: {exc}")
        return None
    if mtimes == known_mtimes:
        return None

    try:
        house, control = load_configs()
        validate_config_structure(house, control)
        sim = HouseSimulator(WEIGHTS_DIR, house)
        mpc = BangBangMPC(sim, house, control)
    except Exception as exc:
        msg = (f"Invalid config edit rejected — keeping previous live config "
               f"({type(exc).__name__}: {exc})")
        logger.error(msg)
        # De-dupe: log each distinct broken edit to errors.log once, not on
        # every tick it stays broken (mtimes are unchanged until the next save).
        if mtimes != _last_reload_error_mtimes:
            config_error_logger.error(msg)
            _last_reload_error_mtimes = mtimes
        return None

    _last_reload_error_mtimes = None
    logger.info("Config change detected — validated and reloaded "
                "house.yaml/control.yaml, rebuilt simulator and MPC")
    # Accepted — but a degraded presence band (item 11) still gets reported.
    log_band_warnings(house, control)
    return house, control, sim, mpc, mtimes
