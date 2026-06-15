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
import io
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# thermal_control.* imports (mirrors the other entry points)
DASH_DIR = Path(__file__).parent
TC_DIR   = DASH_DIR.parent
ROOT     = TC_DIR.parent
sys.path.insert(0, str(ROOT))

import yaml
from flask import Flask, render_template, request, Response, abort

from thermal_control.control.schedule import resolve_targets
from thermal_control.control.config_check import validate_config_structure
from thermal_control.analysis.onoff_plot import make_onoff_plot

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

app = Flask(__name__)


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


# ── On/off plot (reuses the live-analysis figure) ──────────────────────────────
# Render the same 3-panel "AC on/off + room temps" PNG the live-analysis skill
# produces (ac_onoff.png), served inline. Cached on the decision log's mtime so
# the page's 30 s auto-refresh doesn't redraw an unchanged figure every time.
_plot_cache = {"mtime": None, "png": None}


def _load_decision_df():
    """Whole decision log as a DataFrame with a tz-aware ET 'timestamp' column."""
    import pandas as pd

    df = pd.read_csv(DECISION_LOG)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ_NAME)
    return df.sort_values("timestamp").reset_index(drop=True)


def render_onoff_png(control_cfg):
    """PNG bytes of the on/off figure, regenerated only when the log changes."""
    mtime = DECISION_LOG.stat().st_mtime
    if _plot_cache["mtime"] == mtime and _plot_cache["png"] is not None:
        return _plot_cache["png"]

    df = _load_decision_df()
    buf = io.BytesIO()
    # tick_labels=False → no per-tick ON/OFF text (too noisy at dashboard scale).
    make_onoff_plot(df, control_cfg, buf, tick_labels=False)
    png = buf.getvalue()
    _plot_cache.update(mtime=mtime, png=png)
    return png


@app.route("/plots/ac_onoff.png")
def ac_onoff_plot():
    if not DECISION_LOG.exists():
        abort(404)
    png = render_onoff_png(load_yaml(CONTROL_YAML))
    return Response(png, mimetype="image/png")


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
