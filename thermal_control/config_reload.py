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
from thermal_control.control.config_check import validate_config_structure

ROOT         = Path(__file__).parent
WEIGHTS_DIR  = ROOT / "model" / "weights"
HOUSE_YAML   = ROOT / "config" / "house.yaml"
CONTROL_YAML = ROOT / "config" / "control.yaml"
ERROR_LOG    = ROOT / "logs" / "errors.log"   # rejected config reloads land here

logger = logging.getLogger(__name__)


def _make_config_error_logger():
    """
    Dedicated logger for rejected config hot-reloads, writing to errors.log.

    Kept separate from the main INFO stream so an operator who edits a yaml on
    the server has one file to tail for "did my edit take?". propagate=False so
    these lines don't also flood stdout; the main logger still records a copy.
    """
    el = logging.getLogger("thermal_control.config_errors")
    el.setLevel(logging.ERROR)
    el.propagate = False
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(ERROR_LOG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        el.addHandler(handler)
    except OSError as exc:
        logger.warning(f"Could not open {ERROR_LOG}: {exc}")
    return el


config_error_logger = _make_config_error_logger()
_last_reload_error_mtimes = None   # de-dupes repeated errors for one broken edit


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
    return house, control, sim, mpc, mtimes
