"""Tests for config_reload.py — config loading and hot-reload."""

from thermal_control import config_reload as cr


# ── load_configs / config_mtimes ────────────────────────────────────────────
def test_load_configs_returns_house_and_control():
    house, control = cr.load_configs()
    assert "ac_units" in house and "rooms" in house
    assert "mpc" in control and "targets" in control


def test_config_mtimes_returns_two_floats():
    mtimes = cr.config_mtimes()
    assert len(mtimes) == 2
    assert all(isinstance(m, float) for m in mtimes)


# ── maybe_reload (NEXT_STEPS item 3: config hot-reload) ─────────────────────
def test_maybe_reload_noop_when_unchanged():
    assert cr.maybe_reload(cr.config_mtimes()) is None


def test_maybe_reload_rebuilds_on_change():
    result = cr.maybe_reload((0.0, 0.0))                    # stale mtimes → reload
    assert result is not None
    house, control, sim, mpc, mtimes = result
    assert sim.rooms and mpc.ac_units
    assert mtimes == cr.config_mtimes()


def test_maybe_reload_keeps_last_good_on_invalid_yaml(monkeypatch):
    def boom():
        raise ValueError("mid-edit yaml")
    monkeypatch.setattr(cr, "load_configs", boom)
    assert cr.maybe_reload((0.0, 0.0)) is None              # failure swallowed → last-good kept


def test_maybe_reload_logs_invalid_edit_to_errors_log_once(monkeypatch):
    """A rejected edit is recorded in errors.log exactly once per broken save."""
    monkeypatch.setattr(cr, "_last_reload_error_mtimes", None)
    monkeypatch.setattr(cr, "config_mtimes", lambda: (1.0, 2.0))

    real_load = cr.load_configs

    def bad_config():
        house, control = real_load()
        house.pop("rooms")                                 # syntactically fine, structurally broken
        return house, control
    monkeypatch.setattr(cr, "load_configs", bad_config)

    logged = []
    monkeypatch.setattr(cr.config_error_logger, "error", lambda m: logged.append(m))

    assert cr.maybe_reload((0.0, 0.0)) is None              # rejected → last-good kept
    assert cr.maybe_reload((0.0, 0.0)) is None              # same broken mtimes again
    assert len(logged) == 1                                 # de-duped: logged only once
    assert "rooms" in logged[0]


# ── Band warnings (NEXT_STEPS item 11) ──────────────────────────────────────
def test_degraded_band_is_accepted_and_warned_not_rejected(monkeypatch):
    """A malformed presence band must NOT reject the edit — it degrades and warns.

    This is the whole point of the second severity: a typo in one room's presence
    rule cannot be allowed to strand the house on a stale config.
    """
    real_load = cr.load_configs

    def degraded_config():
        house, control = real_load()
        control["targets"]["schedule"][0]["rooms"]["nicolas_office"] = {
            "min_f": 65, "max_f": 85, "occupied": {"min_f": 65, "max_f": 76},
        }
        return house, control
    monkeypatch.setattr(cr, "load_configs", degraded_config)

    warned, errored = [], []
    monkeypatch.setattr(cr.config_warning_logger, "warning", lambda m: warned.append(m))
    monkeypatch.setattr(cr.config_error_logger, "error", lambda m: errored.append(m))

    result = cr.maybe_reload((0.0, 0.0))
    assert result is not None                               # accepted, MPC rebuilt
    assert warned                                           # …but reported
    assert "both a flat band and occupied/unoccupied" in " | ".join(warned)
    assert errored == []                                    # errors.log stays clean


def test_log_band_warnings_silent_on_the_real_config(monkeypatch):
    warned = []
    monkeypatch.setattr(cr.config_warning_logger, "warning", lambda m: warned.append(m))
    assert cr.log_band_warnings(*cr.load_configs()) == []
    assert warned == []


def test_warning_log_is_a_separate_file_from_errors_log():
    # The dashboard date-stamps its red banner from the last line of errors.log,
    # so a warning written there would misdate an unrelated alarm.
    assert cr.WARNING_LOG != cr.ERROR_LOG
