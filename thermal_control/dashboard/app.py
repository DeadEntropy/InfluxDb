"""
dashboard/app.py
────────────────
A small, read-only Flask dashboard for the thermal-control MPC.

Files only — reads config yaml + remote_logs/, never touches Home Assistant.
Run from the repo root:
    python thermal_control/dashboard/app.py   # → http://localhost:5111

See DASHBOARD.md for the full description.
"""

import os
import sys
import csv
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

# thermal_control.* imports (mirrors the other entry points)
DASH_DIR = Path(__file__).parent
TC_DIR   = DASH_DIR.parent
ROOT     = TC_DIR.parent
sys.path.insert(0, str(ROOT))

import yaml
from flask import Flask, render_template, request, Response, abort, jsonify
import plotly.offline as plotly_offline

from thermal_control.control.schedule import resolve_targets, resolve_targets_for_rooms
from thermal_control.control.config_check import validate_config_structure
from thermal_control.analysis.onoff_plot import AC_UNITS, AC_ROOMS

# ── Paths ─────────────────────────────────────────────────────────────────────
# Overridable via env so the same image works locally (remote_logs/) and on the
# server (where the live logs land in a different mounted directory).
CONFIG_DIR   = Path(os.environ.get("MPC_CONFIG_DIR", TC_DIR / "config"))
LOGS_DIR     = Path(os.environ.get("MPC_LOGS_DIR", ROOT / "remote_logs"))
CONTROL_YAML = CONFIG_DIR / "control.yaml"
HOUSE_YAML   = CONFIG_DIR / "house.yaml"
DECISION_LOG = LOGS_DIR / "mpc_decision_log.csv"
USER_INPUTS  = LOGS_DIR / "user_inputs.log"
MPC_VERSION_FILE = LOGS_DIR / "mpc_version.txt"   # stamped by scheduler.py at startup
ERROR_LOG    = LOGS_DIR / "errors.log"            # rejected config reloads (scheduler.py)

WIDE_BAND = {"min_f": 65, "max_f": 85}   # the "don't care" band
TZ_NAME   = "America/New_York"
STALE_MINUTES = 15                       # tick older than this → staleness warning

# Distinct per-room line colors for the on/off charts, assigned by each room's
# position in AC_ROOMS[ac] (max 3 rooms/panel) — cycles if a zone ever grows.
ROOM_COLORS = ["#4aa3ff", "#ffb454", "#3fd67a", "#ff6b6b", "#c792ea", "#5eead4"]

app = Flask(__name__)

# Plotly's bundled JS, served locally (no CDN) so the dashboard stays fully
# self-contained/offline — sourced from the pinned `plotly` dependency itself,
# not checked into the repo. Built once at import time (~4.8MB).
_PLOTLYJS = plotly_offline.get_plotlyjs().encode("utf-8")


@app.route("/vendor/plotly.min.js")
def plotly_js():
    return Response(_PLOTLYJS, mimetype="application/javascript",
                     headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.template_global()
def qs_with(**overrides):
    """Current query string with the given keys overridden/removed (None or ''
    removes). Lets toggles (day, range, start, end) compose independently —
    switching one preserves the others instead of resetting the whole page."""
    params = request.args.to_dict()
    params.update(overrides)
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return "?" + urlencode(params)


@app.template_filter("half")
def round_half(v):
    """Round a temperature to the nearest 0.5°F for display; '74', '74.5', etc."""
    f = v if isinstance(v, (int, float)) else to_float(v)
    if f is None:
        return v if v not in (None, "") else "—"
    r = round(f * 2) / 2
    return f"{r:.1f}".rstrip("0").rstrip(".")


# ── Config loading ────────────────────────────────────────────────────────────
def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def ac_zones(house_cfg):
    """Ordered [(ac_id, [room_id, ...]), ...] for the modelled rooms only."""
    return [(ac["id"], list(ac.get("covers", [])))
            for ac in house_cfg.get("ac_units", [])]


def modelled_rooms(house_cfg):
    """Flat ordered list of rooms that belong to an AC zone (the modelled ones)."""
    rooms = []
    for _, covers in ac_zones(house_cfg):
        rooms.extend(covers)
    return rooms


# ── Section 1: schedule grid ──────────────────────────────────────────────────
def is_wide(band):
    return band["min_f"] <= WIDE_BAND["min_f"] and band["max_f"] >= WIDE_BAND["max_f"]


def band_class(band):
    """CSS class describing the band's cooling intent (tighter = stronger)."""
    if is_wide(band):
        return "band-wide"
    width = band["max_f"] - band["min_f"]
    if width <= 2:
        return "band-tight"
    if width <= 5:
        return "band-mid"
    return "band-loose"


def band_label(band):
    return "off" if is_wide(band) else f"{band['min_f']:.0f}–{band['max_f']:.0f}"


def apply_override(band, target):
    """The MPC's override rule: user value is the upper bound; the lower bound
    follows at the scheduled band's width (mirrors resolve_targets_for_rooms)."""
    width = band["max_f"] - band["min_f"]
    return {"min_f": target - width, "max_f": target}


def schedule_grid(control_cfg, rooms, day_type, current_hour=None, ovr_map=None):
    """
    {room_id: [ {hour, min_f, max_f, label, cls} for hour in 0..23 ]}.

    Bands are produced by the real resolve_targets() for an example date of the
    requested day_type, so the grid matches exactly what the MPC resolves.
    """
    tz = ZoneInfo(TZ_NAME)
    # Pick a reference date matching the requested day type (weekday/weekend).
    today = datetime.now(tz).date()
    ref = today
    want_weekend = (day_type == "weekend")
    for _ in range(7):
        if (ref.weekday() >= 5) == want_weekend:
            break
        ref = ref + timedelta(days=1)

    ovr_map = ovr_map or {}
    grid = {r: [] for r in rooms}
    for hour in range(24):
        now = datetime(ref.year, ref.month, ref.day, hour, 0, tzinfo=tz)
        resolved = resolve_targets(control_cfg, now)
        default = control_cfg["targets"]["default"]
        is_now = (hour == current_hour)
        for room in rooms:
            band = resolved.get(room, dict(default))
            cell = {
                "hour": hour,
                "min_f": band["min_f"],
                "max_f": band["max_f"],
                "label": band_label(band),
                "cls": band_class(band),
                "is_now": is_now,
                "title": f"{band['min_f']:.0f}–{band['max_f']:.0f}°F",
            }
            # An active override only applies right now → annotate the now-cell.
            if is_now and room in ovr_map:
                ob = apply_override(band, ovr_map[room])
                cell["label"] = f"{ob['min_f']:.0f}–{ob['max_f']:.0f} ({band_label(band)})"
                cell["cls"] = "band-override"
                cell["title"] = (f"override {ob['min_f']:.0f}–{ob['max_f']:.0f}°F "
                                 f"(scheduled {band_label(band)})")
            grid[room].append(cell)
    return grid


# ── Overrides (from user_inputs.log) ──────────────────────────────────────────
def parse_ts(s):
    return datetime.fromisoformat(s.strip())


def active_overrides(control_cfg):
    """
    Rooms whose most-recent override event is still inside the override window.
    Returns [{room, target, started, minutes_left}], newest first.
    """
    if not USER_INPUTS.exists():
        return []
    duration = control_cfg["mpc"].get("override_duration_minutes", 60)
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)

    latest = {}   # room -> (ts, value)  for override_activated/changed events
    with open(USER_INPUTS, newline="") as f:
        for row in csv.DictReader(f):
            event = (row.get("event") or "").strip()
            room = (row.get("room") or "").strip()
            if not room:
                continue
            if event.startswith("override"):
                try:
                    ts = parse_ts(row["timestamp"])
                except (ValueError, KeyError):
                    continue
                if event in ("override_cleared", "override_expired", "override_cancelled"):
                    latest.pop(room, None)
                else:  # override_activated / override_changed
                    val = (row.get("value") or "").strip()
                    latest[room] = (ts, val)

    out = []
    for room, (ts, val) in latest.items():
        elapsed = (now - ts).total_seconds() / 60.0
        left = duration - elapsed
        if left > 0:
            out.append({
                "room": room,
                "target": val,
                "started": ts.strftime("%H:%M"),
                "minutes_left": int(round(left)),
            })
    out.sort(key=lambda d: d["minutes_left"], reverse=True)
    return out


def override_map(overrides):
    """{room_id: target_f} from the active_overrides() list (numeric targets)."""
    m = {}
    for o in overrides:
        t = to_float(o["target"])
        if t is not None:
            m[o["room"]] = t
    return m


# ── Section 2: decision log ───────────────────────────────────────────────────
def read_decision_rows(n):
    """Last n rows of the decision log as dicts, oldest→newest. [] if missing."""
    if not DECISION_LOG.exists():
        return []
    with open(DECISION_LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def decision_summary(control_cfg, house_cfg, rooms, ovr_map=None):
    """Build the 'now' card + recent-ticks data from the decision log."""
    ovr_map = ovr_map or {}
    rows = read_decision_rows(12)
    if not rows:
        return None

    last = rows[-1]
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    try:
        ts = parse_ts(last["timestamp"])
        age_min = (now - ts).total_seconds() / 60.0
    except (ValueError, KeyError):
        ts, age_min = None, None

    # Resolved bands at the decision's wall-clock time (so temp vs band lines up).
    band_now = resolve_targets(control_cfg, ts or now)
    default = control_cfg["targets"]["default"]

    # AC states
    acs = []
    for ac_id, _ in ac_zones(house_cfg):
        on = last.get(f"on_{ac_id}")
        sp = last.get(f"sp_{ac_id}")
        acs.append({
            "id": ac_id,
            "on": on == "1",
            "setpoint": sp,
        })

    # Per-room temp vs band — apply any active override on top of the schedule
    # (mirrors what the MPC actually targeted), keeping the scheduled band for
    # reference.
    room_status = []
    for room in rooms:
        temp = to_float(last.get(f"T_{room}"))
        sched = band_now.get(room, dict(default))
        overridden = room in ovr_map
        band = apply_override(sched, ovr_map[room]) if overridden else sched
        if temp is None:
            state = "?"
        elif is_wide(band):
            state = "off"
        elif temp > band["max_f"]:
            state = "above"
        elif temp < band["min_f"]:
            state = "below"
        else:
            state = "in"
        # Row tint: red = needs cooling (above band), blue = already cool
        # (in or below band), neutral = don't-care / unknown.
        tone = {"above": "hot", "in": "cool", "below": "cool"}.get(state, "neutral")
        room_status.append({
            "room": room,
            "temp": temp,
            "min_f": band["min_f"],
            "max_f": band["max_f"],
            "wide": is_wide(band),
            "state": state,
            "tone": tone,
            "overridden": overridden,
            "sched_label": band_label(sched),
        })

    # Chosen cost vs cheapest alternative
    cost_chosen = to_float(last.get("cost_chosen"))
    combo_costs = {k: to_float(v) for k, v in last.items()
                   if k.startswith("cost_") and k != "cost_chosen"}
    combo_costs = {k: v for k, v in combo_costs.items() if v is not None}
    cheapest_alt = None
    if cost_chosen is not None and combo_costs:
        alts = sorted(v for v in combo_costs.values() if v > cost_chosen + 1e-9)
        cheapest_alt = alts[0] if alts else cost_chosen

    return {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M") if ts else last.get("timestamp"),
        "age_min": round(age_min, 1) if age_min is not None else None,
        "stale": (age_min is not None and age_min > STALE_MINUTES),
        "outdoor": to_float(last.get("T_outdoor")),
        "write_ok": last.get("write_ok") == "True",
        "acs": acs,
        "rooms": room_status,
        "cost_chosen": cost_chosen,
        "cheapest_alt": cheapest_alt,
    }


# ── On/off plot data (rendered client-side with Plotly) ────────────────────────
PLOT_RANGES = ("1d", "3d", "1w", "1m", "all", "custom")
DEFAULT_RANGE = "1d"
RANGE_DELTAS = {
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "1m": timedelta(days=30),
}


def _load_decision_df():
    """Whole decision log as a DataFrame with a tz-aware ET 'timestamp' column."""
    import pandas as pd

    df = pd.read_csv(DECISION_LOG)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ_NAME)
    return df.sort_values("timestamp").reset_index(drop=True)


def _latest_decision_ts():
    """Timestamp of the most recent decision-log row, or None if the log is empty."""
    rows = read_decision_rows(1)
    if not rows:
        return None
    try:
        return parse_ts(rows[-1]["timestamp"])
    except (ValueError, KeyError):
        return None


def _parse_local_dt(s):
    """Parse a datetime-local input value ('YYYY-MM-DDTHH:MM') as ET; None if unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(TZ_NAME))
    return dt


def resolve_plot_range(range_code, start_str, end_str, latest_ts):
    """
    Resolve the requested plot window to (range_code, start, end) — start/end
    are tz-aware datetimes, or (None, None) for "all" (no filtering).

    Falls back to DEFAULT_RANGE when the code is unrecognized, or when "custom"
    is missing/has unparseable start/end — never raises on bad query input.
    """
    if range_code == "all":
        return "all", None, None

    if range_code == "custom":
        start, end = _parse_local_dt(start_str), _parse_local_dt(end_str)
        if start and end:
            if start > end:
                start, end = end, start
            return "custom", start, end
        range_code = DEFAULT_RANGE

    if range_code not in RANGE_DELTAS:
        range_code = DEFAULT_RANGE
    if latest_ts is None:
        return range_code, None, None
    return range_code, latest_ts - RANGE_DELTAS[range_code], latest_ts


def _filter_range(df, start, end):
    if start is not None:
        df = df[df["timestamp"] >= start]
    if end is not None:
        df = df[df["timestamp"] <= end]
    return df.reset_index(drop=True)


def _room_color(room, ac):
    idx = AC_ROOMS.get(ac, []).index(room)
    return ROOM_COLORS[idx % len(ROOM_COLORS)]


def _away_intervals():
    """[(start, end), ...] tz-aware ET datetimes when away/holiday mode (item
    8) was active, reconstructed from away_activated/away_deactivated events
    in user_inputs.log. A still-open period (no matching deactivated event
    yet) gets end=now."""
    if not USER_INPUTS.exists():
        return []
    events = []
    with open(USER_INPUTS, newline="") as f:
        for row in csv.DictReader(f):
            event = (row.get("event") or "").strip()
            if event not in ("away_activated", "away_deactivated"):
                continue
            try:
                ts = parse_ts(row["timestamp"])
            except (ValueError, KeyError):
                continue
            events.append((ts, event))
    events.sort(key=lambda e: e[0])

    intervals = []
    start = None
    for ts, event in events:
        if event == "away_activated" and start is None:
            start = ts
        elif event == "away_deactivated" and start is not None:
            intervals.append((start, ts))
            start = None
    if start is not None:
        intervals.append((start, datetime.now(ZoneInfo(TZ_NAME))))
    return intervals


def _is_away(intervals, ts):
    return any(start <= ts <= end for start, end in intervals)


def _override_series():
    """{room_id: [(start, end, target_f), ...]} reconstructed from
    override_activated/changed/expired/cancelled/cleared events in
    user_inputs.log — the per-room analogue of _away_intervals(). A still-open
    interval (activated/changed with no closing event yet) gets end=now."""
    if not USER_INPUTS.exists():
        return {}
    tz  = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)

    events_by_room = {}
    with open(USER_INPUTS, newline="") as f:
        for row in csv.DictReader(f):
            event = (row.get("event") or "").strip()
            room  = (row.get("room") or "").strip()
            if not room or not event.startswith("override"):
                continue
            try:
                ts = parse_ts(row["timestamp"])
            except (ValueError, KeyError):
                continue
            events_by_room.setdefault(room, []).append((ts, event, (row.get("value") or "").strip()))

    out = {}
    for room, events in events_by_room.items():
        events.sort(key=lambda e: e[0])
        intervals = []
        start = value = None
        for ts, event, val in events:
            if event in ("override_activated", "override_changed"):
                if start is not None:
                    intervals.append((start, ts, value))
                start, value = ts, to_float(val)
            elif event in ("override_expired", "override_cancelled", "override_cleared"):
                if start is not None:
                    intervals.append((start, ts, value))
                    start = value = None
        if start is not None:
            intervals.append((start, now, value))
        out[room] = intervals
    return out


def _overrides_at(override_series, room_ids, ts):
    """{room_id: target_f} still active at `ts`, from _override_series()."""
    out = {}
    for room in room_ids:
        for start, end, value in override_series.get(room, []):
            if start <= ts <= end and value is not None:
                out[room] = value
                break
    return out


def _room_series(df, control_cfg, ac):
    """[{id, label, color, temp, band_lo, band_hi}, ...] for the rooms this AC
    covers that have a temperature column in df. band_lo/band_hi are the
    comfort band actually in effect at each row's own timestamp (piecewise
    constant) — schedule-resolved, except during a logged away/holiday period
    (every room gets the away band instead) or a logged manual override on
    that room (the band's upper bound follows the user's value). Both are
    reconstructed from user_inputs.log, since neither is itself a
    decision-log column, and mirror what the MPC actually targeted at that
    moment."""
    room_ids = [r for r in AC_ROOMS.get(ac, []) if f"T_{r}" in df.columns]
    if not room_ids:
        return []

    away_intervals = _away_intervals()
    override_series = _override_series()
    resolved = [
        resolve_targets_for_rooms(
            control_cfg, room_ids, ts,
            away=_is_away(away_intervals, ts),
            override_targets=_overrides_at(override_series, room_ids, ts),
        )
        for ts in df["timestamp"]
    ]

    out = []
    for room in room_ids:
        col = df[f"T_{room}"]
        lo = [r[room]["min_f"] for r in resolved]
        hi = [r[room]["max_f"] for r in resolved]
        out.append({
            "id": room,
            "label": room.replace("_", " "),
            "color": _room_color(room, ac),
            "temp": [None if v != v else round(float(v), 2) for v in col],  # v!=v → NaN
            "band_lo": lo,
            "band_hi": hi,
        })
    return out


@app.route("/plots/onoff_data.json")
def onoff_data():
    ac = request.args.get("ac")
    if ac not in AC_UNITS:
        abort(404)
    if not DECISION_LOG.exists():
        return jsonify({"ac": ac, "range": DEFAULT_RANGE, "timestamps": [], "on": [], "rooms": []})

    range_code, start, end = resolve_plot_range(
        request.args.get("range", DEFAULT_RANGE),
        request.args.get("start"), request.args.get("end"),
        _latest_decision_ts(),
    )
    df = _filter_range(_load_decision_df(), start, end)
    on_col = f"on_{ac}"
    if df.empty or on_col not in df.columns:
        return jsonify({"ac": ac, "range": range_code, "timestamps": [], "on": [], "rooms": []})

    control_cfg, _, _ = resolve_page_config()
    return jsonify({
        "ac": ac,
        "range": range_code,
        "timestamps": [ts.isoformat() for ts in df["timestamp"]],
        "on": [int(v) for v in df[on_col]],
        "rooms": _room_series(df, control_cfg, ac),
    })


# ── Live config-error banner ────────────────────────────────────────────────────
def _last_error_log_time():
    """
    Wall-clock of the most recent errors.log entry (scheduler stamps these in
    local ET), or None if the log is missing/empty/unparseable. Used only to
    show *when* the current breakage was first logged — never to decide
    liveness (that comes from re-validating the on-disk config below).
    """
    if not ERROR_LOG.exists():
        return None
    try:
        with open(ERROR_LOG) as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        ts = datetime.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S")
        return ts.replace(tzinfo=ZoneInfo(TZ_NAME))
    except (ValueError, IndexError):
        return None


# Last config that parsed + validated — what the live MPC is actually running
# on. When a broken edit lands on disk the dashboard falls back to this (just
# like the scheduler keeps its last-good config) and adds the banner.
_last_good_cfg = {"control": None, "house": None}


def resolve_page_config():
    """
    Load the config for a page render. Returns (control_cfg, house_cfg, error).

    Liveness is decided by re-validating the on-disk yaml right now — the exact
    check the scheduler runs before adopting an edit (yaml syntax → required
    structure). When it passes, the config is used and cached as last-good and
    error is None. When it fails, error = {message, since} and the page falls
    back to the cached last-good config (the one the MPC is still running), so a
    broken edit shows a banner without taking the whole dashboard down. The
    moment the edit is fixed the next render validates clean and the banner
    clears — regardless of what errors.log still records about past breakage.
    """
    try:
        house = load_yaml(HOUSE_YAML)
        control = load_yaml(CONTROL_YAML)
        validate_config_structure(house, control)
    except FileNotFoundError as exc:
        message = f"Config file missing: {exc}"
    except yaml.YAMLError as exc:
        message = f"Invalid YAML syntax: {exc}"
    except ValueError as exc:
        message = str(exc)
    except Exception as exc:                  # defensive: never let this break the page
        message = f"{type(exc).__name__}: {exc}"
    else:
        _last_good_cfg["control"], _last_good_cfg["house"] = control, house
        return control, house, None

    since = _last_error_log_time()
    error = {"message": message,
             "since": since.strftime("%Y-%m-%d %H:%M") if since else None}
    return _last_good_cfg["control"], _last_good_cfg["house"], error


# ── Build versions (footer) ────────────────────────────────────────────────────
def build_versions():
    """{'mpc': ..., 'dashboard': ...} — the git SHA each container was built from.

    The dashboard knows its own build from the MPC_VERSION env baked into its
    image; the MPC's build is read from the version file the scheduler stamps
    into the shared logs dir (absent until a scheduler that writes it has run).
    """
    dashboard = os.environ.get("MPC_VERSION", "dev")
    try:
        mpc = MPC_VERSION_FILE.read_text().strip() or "unknown"
    except OSError:
        mpc = "unknown"
    return {"mpc": mpc, "dashboard": dashboard}


# ── Route ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    day_type = request.args.get("day", "")
    if day_type not in ("weekday", "weekend"):
        day_type = "weekend" if datetime.now(ZoneInfo(TZ_NAME)).weekday() >= 5 else "weekday"

    range_code = request.args.get("range", DEFAULT_RANGE)
    if range_code not in PLOT_RANGES:
        range_code = DEFAULT_RANGE
    range_start_raw = request.args.get("start", "")
    range_end_raw = request.args.get("end", "")

    control_cfg, house_cfg, config_error = resolve_page_config()
    now = datetime.now(ZoneInfo(TZ_NAME))

    # Broken config on disk AND no last-good cached yet (dashboard started while
    # the config was already broken). Show just the banner — nothing else can be
    # resolved without a valid config.
    if control_cfg is None or house_cfg is None:
        return render_template(
            "index.html",
            config_error=config_error,
            day_type=day_type, zones=[], hours=list(range(24)),
            current_hour=None, grid={}, overrides=[], decision=None,
            range_code=range_code, range_start_raw=range_start_raw,
            range_end_raw=range_end_raw, ac_units=AC_UNITS,
            refreshed=now.strftime("%H:%M:%S"), versions=build_versions(),
        )

    rooms = modelled_rooms(house_cfg)
    zones = ac_zones(house_cfg)

    overrides = active_overrides(control_cfg)
    ovr_map = override_map(overrides)

    # The "now" highlight only makes sense when the displayed day matches today.
    showing_today = (now.weekday() >= 5) == (day_type == "weekend")
    current_hour = now.hour if showing_today else None

    return render_template(
        "index.html",
        config_error=config_error,
        day_type=day_type,
        zones=zones,
        hours=list(range(24)),
        current_hour=current_hour,
        grid=schedule_grid(control_cfg, rooms, day_type, current_hour, ovr_map),
        overrides=overrides,
        decision=decision_summary(control_cfg, house_cfg, rooms, ovr_map),
        range_code=range_code, range_start_raw=range_start_raw,
        range_end_raw=range_end_raw, ac_units=AC_UNITS,
        refreshed=now.strftime("%H:%M:%S"),
        versions=build_versions(),
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5111)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    # Reloader off by default: it forks a child process that survives the parent
    # and orphans the port. Pass --debug to enable it for development.
    app.run(host=args.host, port=args.port, debug=args.debug,
            use_reloader=args.debug)
