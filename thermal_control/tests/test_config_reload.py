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
