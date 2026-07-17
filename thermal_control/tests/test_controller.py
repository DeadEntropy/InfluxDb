"""Tests for ha_bridge/controller.py — HA REST bridge (HTTP fully mocked)."""

import pytest

from thermal_control.ha_bridge import controller as ha


def make_get_state(states):
    """Return a fake _get_state that looks entities up in `states`, raising for
    anything missing (mimics an unreachable entity)."""
    def _get_state(entity_id):
        if entity_id not in states:
            raise RuntimeError(f"unreachable: {entity_id}")
        return states[entity_id]
    return _get_state


# ── check_sensor_units ──────────────────────────────────────────────────────
def test_check_sensor_units_warns_on_wrong_unit(monkeypatch, caplog, house_config):
    states = {
        r["sensor_entity"]: {"state": "75", "attributes": {"unit_of_measurement": "°C"}}
        for r in house_config["rooms"] if r.get("sensor_entity")
    }
    monkeypatch.setattr(ha, "_get_state", make_get_state(states))
    with caplog.at_level("WARNING"):
        ha.check_sensor_units(house_config)
    assert any("unit_of_measurement" in rec.message for rec in caplog.records)


# ── get_room_temps ──────────────────────────────────────────────────────────
def test_get_room_temps_skips_unavailable(monkeypatch, house_config):
    states = {}
    for r in house_config["rooms"]:
        e = r.get("sensor_entity")
        if e:
            states[e] = {"state": "74.5", "attributes": {}}
    # make one room unavailable
    states["sensor.kitchen_temperature"] = {"state": "unavailable", "attributes": {}}
    monkeypatch.setattr(ha, "_get_state", make_get_state(states))

    temps = ha.get_room_temps(house_config)
    assert temps["master_bedroom"] == 74.5
    assert "kitchen" not in temps          # unavailable omitted


# ── get_outdoor_temp ────────────────────────────────────────────────────────
def test_get_outdoor_temp(monkeypatch, house_config):
    ent = house_config["outdoor_sensor"]
    monkeypatch.setattr(ha, "_get_state",
                        make_get_state({ent: {"state": "88.0", "attributes": {}}}))
    assert ha.get_outdoor_temp(house_config) == 88.0

    monkeypatch.setattr(ha, "_get_state",
                        make_get_state({ent: {"state": "unavailable", "attributes": {}}}))
    assert ha.get_outdoor_temp(house_config) is None


# ── get_away_mode ───────────────────────────────────────────────────────────
def test_get_away_mode(monkeypatch, control_config):
    ent = control_config["targets"]["away"]["entity"]
    monkeypatch.setattr(ha, "_get_state", make_get_state({ent: {"state": "on"}}))
    assert ha.get_away_mode(control_config) is True

    monkeypatch.setattr(ha, "_get_state", make_get_state({ent: {"state": "off"}}))
    assert ha.get_away_mode(control_config) is False

    # read error → fail safe to home (False)
    monkeypatch.setattr(ha, "_get_state", make_get_state({}))
    assert ha.get_away_mode(control_config) is False


# ── get_presence ────────────────────────────────────────────────────────────
def test_get_presence_failsafe_occupied(monkeypatch, house_config):
    ent = "binary_sensor.presence_nicolas_office"
    monkeypatch.setattr(ha, "_get_state", make_get_state({ent: {"state": "on"}}))
    assert ha.get_presence(house_config) == {"nicolas_office": True}

    monkeypatch.setattr(ha, "_get_state", make_get_state({ent: {"state": "off"}}))
    assert ha.get_presence(house_config) == {"nicolas_office": False}

    # unavailable → fail safe to occupied
    monkeypatch.setattr(ha, "_get_state", make_get_state({ent: {"state": "unavailable"}}))
    assert ha.get_presence(house_config) == {"nicolas_office": True}


# ── get_killed_acs ──────────────────────────────────────────────────────────
def test_get_killed_acs_reads_per_ac_entity(monkeypatch, house_config):
    acs = {ac["id"]: ac["kill_entity"] for ac in house_config["ac_units"]}
    on_ac, off_ac = list(acs)[0], list(acs)[1]
    states = {ent: {"state": "off"} for ent in acs.values()}
    states[acs[on_ac]] = {"state": "on"}
    monkeypatch.setattr(ha, "_get_state", make_get_state(states))
    killed = ha.get_killed_acs(house_config)
    assert killed == {on_ac}
    assert off_ac not in killed


def test_get_killed_acs_failsafe_controlled(monkeypatch, house_config):
    # read error → fail safe to "not killed" (still MPC-controlled)
    monkeypatch.setattr(ha, "_get_state", make_get_state({}))
    assert ha.get_killed_acs(house_config) == set()


# ── get_room_targets ──────────────────────────────────────────────────────
def test_get_room_targets(monkeypatch, house_config):
    ent = "climate.mpc_kitchen"
    # kitchen has a real target; an unavailable card is skipped; others error out
    states = {r.get("thermostat_entity"): {"state": "cool", "attributes": {"temperature": 77}}
              for r in house_config["rooms"] if r.get("thermostat_entity")}
    states[ent] = {"state": "cool", "attributes": {"temperature": "78.0"}}
    states["climate.mpc_tv_room"] = {"state": "unavailable", "attributes": {}}
    monkeypatch.setattr(ha, "_get_state", make_get_state(states))
    targets = ha.get_room_targets(house_config)
    assert targets["kitchen"] == 78          # rounded int from attributes.temperature
    assert "tv_room" not in targets          # unavailable card omitted
    assert all(isinstance(v, int) for v in targets.values())


# ── set_room_target ─────────────────────────────────────────────────────────
def test_set_room_target_posts_set_temperature(monkeypatch, house_config):
    calls = []
    monkeypatch.setattr(ha.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or _ok())
    ha.set_room_target(house_config, "kitchen", 79)
    assert len(calls) == 1
    url, kw = calls[0]
    assert url.endswith("/api/services/climate/set_temperature")
    assert kw["json"] == {"entity_id": "climate.mpc_kitchen", "temperature": 79}


# ── get_ac_states ───────────────────────────────────────────────────────────
def test_get_ac_states_derives_ac_on(monkeypatch, house_config):
    states = {}
    for ac in house_config["ac_units"]:
        states[ac["climate_entity"]] = {
            "state": "cool",
            "attributes": {"current_temperature": 74, "temperature": 72,
                           "hvac_action": "cooling"},
        }
    monkeypatch.setattr(ha, "_get_state", make_get_state(states))
    result = ha.get_ac_states(house_config)
    assert result["living_ac"]["ac_on"] == 1
    assert result["living_ac"]["setpoint_f"] == 72


# ── set_hvac_mode ───────────────────────────────────────────────────────────
def test_set_hvac_mode_posts(monkeypatch, house_config):
    calls = []
    monkeypatch.setattr(ha.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or _ok())
    ha.set_hvac_mode("living_ac", "off", house_config)
    url, kw = calls[0]
    assert url.endswith("/api/services/climate/set_hvac_mode")
    assert kw["json"]["hvac_mode"] == "off"


# ── apply_setpoints ─────────────────────────────────────────────────────────
def test_apply_setpoints_writes_integers_and_verifies(monkeypatch, house_config):
    """KEY FEATURE: setpoints written to HA must be integers (the AC hardware
    switches on whole-°F thresholds)."""
    posted = []
    monkeypatch.setattr(ha.requests, "post",
                        lambda url, **kw: posted.append(kw["json"]) or _ok())
    # readback returns the written value → no mismatch warning
    readback = {ac["climate_entity"]: {"attributes": {"temperature": 65}}
                for ac in house_config["ac_units"]}
    monkeypatch.setattr(ha, "_get_state", make_get_state(readback))

    ha.apply_setpoints({"living_ac": 65.0}, house_config)   # float in → int out
    assert posted[0]["temperature"] == 65
    assert isinstance(posted[0]["temperature"], int)


def test_apply_setpoints_raises_on_write_failure(monkeypatch, house_config):
    def boom(*a, **k):
        raise RuntimeError("HA down")
    monkeypatch.setattr(ha.requests, "post", boom)
    with pytest.raises(RuntimeError):
        ha.apply_setpoints({"living_ac": 65}, house_config)


def test_apply_setpoints_warns_on_readback_mismatch(monkeypatch, caplog, house_config):
    """KEY FEATURE: readback verification catches silent HA write failures."""
    monkeypatch.setattr(ha.requests, "post", lambda url, **kw: _ok())
    # readback reports a different value than written
    readback = {ac["climate_entity"]: {"attributes": {"temperature": 84}}
                for ac in house_config["ac_units"]}
    monkeypatch.setattr(ha, "_get_state", make_get_state(readback))
    with caplog.at_level("WARNING"):
        ha.apply_setpoints({"living_ac": 65}, house_config)
    assert any("readback mismatch" in rec.message for rec in caplog.records)


class _OkResp:
    def raise_for_status(self):
        pass


def _ok():
    return _OkResp()
