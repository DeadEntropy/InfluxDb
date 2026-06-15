# MPC Dashboard

A small local Flask web app to visualise what the thermal-control MPC is doing.
It is **read-only and files-only**: it reads the config yaml and the logs in
`remote_logs/`, never touches Home Assistant, and needs no credentials or network.

## Run

```bash
pip install -r thermal_control/requirements.txt   # adds flask
python thermal_control/dashboard/app.py           # serves http://localhost:5111
```

The page auto-refreshes (~30 s). When pointed at the live logs (see below) it
shows near-real-time state — there is no separate sync step. All temperatures
are displayed rounded to the nearest **0.5 °F**.

## What it shows

### Section 1 — Schedule grid
- One row per modelled room, grouped under its AC zone (bedroom_ac / living_ac /
  extension_ac), with 24 hourly columns (00:00–23:00).
- Each cell is the **resolved comfort band** for that hour, computed by calling
  the real `control/schedule.py::resolve_targets()` for each hour — so the grid
  cannot drift from what the MPC actually sees.
- Colour scale by band tightness: a tight band (strong cooling intent) is
  highlighted; the wide 65–85 °F "don't care" band (shown as `off`) is greyed
  out. Tooltip shows the exact min/max °F.
- **Current-hour column is highlighted** (blue outline) so "what matters now"
  stands out. It only highlights when the displayed day matches today, so the
  highlight disappears when you toggle to the other day type.
- **Active overrides appear inline** in the room's now-cell: instead of the
  scheduled band it reads e.g. `72–74 (75–77)` — the override band, with the
  scheduled band in parentheses — tinted amber. (An override is a transient
  60-min thing, so it is only shown in the current-hour cell.)
- Weekday / Weekend toggle (the schedule differs between them).
- **Override panel** above the grid: parses `remote_logs/user_inputs.log`,
  surfaces any room whose most-recent override is still inside the
  `override_duration_minutes` (60 min) window, with target °F and minutes
  remaining; says "No active HA overrides" otherwise.

### Section 2 — MPC status & decision log
- **"Now" card** from the latest `mpc_decision_log.csv` row:
  - **AC units** — each AC ON/OFF and its written setpoint.
  - **Rooms vs band** — each room's current temp and its effective band, with the
    whole row tinted by what the room needs: **red = above band → needs cooling**,
    **blue = in/below band → already cool**, neutral for the don't-care band or an
    unreadable sensor. An active override is applied on top of the schedule (so
    the band and the tint match what the MPC actually targets), flagged with an
    `OVR` tag, an amber left border, and the scheduled band shown in parentheses.
  - **Header line** — outdoor temp, chosen-combo cost vs the cheapest
    alternative, `write_ok` health, last-tick time, and a STALE badge if the last
    tick is older than ~15 min.
- **On/off + room-temps plot** — the same 3-panel figure the `live-analysis`
  skill produces (`ac_onoff.png`): one panel per AC zone, the MPC on/off decision
  as a step plot, every covered room's temperature on the right axis, and red/blue
  breach markers. Rendered server-side from the shared
  `thermal_control/analysis/onoff_plot.py` (per-tick ON/OFF text labels are
  suppressed here to keep it readable) and served at `/plots/ac_onoff.png`,
  regenerated only when the decision log changes.

## Data sources (all files)
| File | Used for |
|------|----------|
| `thermal_control/config/control.yaml` | schedule, bands, override duration |
| `thermal_control/config/house.yaml`   | room list, AC-zone grouping |
| `remote_logs/mpc_decision_log.csv`     | current + recent decisions, temps, costs |
| `remote_logs/user_inputs.log`          | active overrides / presence events |

## Where it reads the logs from

The app does **not** hard-code the log location. On startup it resolves two
directories, each overridable by an environment variable (`app.py`):

| Env var | Default (when unset) | Points at |
|---------|----------------------|-----------|
| `MPC_LOGS_DIR`   | `<repo>/remote_logs/`        | `mpc_decision_log.csv`, `user_inputs.log` |
| `MPC_CONFIG_DIR` | `thermal_control/config/`    | `control.yaml`, `house.yaml` |

So the **same image** works in two situations:

- **Locally (dev):** no env vars set → it reads `remote_logs/`. On this dev
  machine that path is a read-only CIFS mount of the server's live log share, so
  you already see live data.
- **On the server (container):** `docker-compose.yml` sets
  `MPC_LOGS_DIR=/app/thermal_control/logs` and bind-mounts the host's
  `./thermal_control/logs` directory onto that path (read-only). That host
  directory is the **same one the prod scheduler writes its logs to** (the `prod`
  service mounts it read-write). Net effect: the dashboard container reads
  exactly the files the live controller is writing, in real time — no copying,
  no sync.

```
host: ./thermal_control/logs ──┬─(rw)→ prod container  (writes decision log)
                               └─(ro)→ dashboard container at /app/thermal_control/logs
                                        ↑ MPC_LOGS_DIR points here
```

Config is wired the same way (`MPC_CONFIG_DIR=/app/thermal_control/config` +
read-only mount of `./thermal_control/config`), so the schedule grid reflects the
exact yaml the prod scheduler is hot-reloading.

## Deployment (own container)

The dashboard ships as its **own** image (`deadentropy/mpc-dashboard`), built
from [`Dockerfile.dashboard`](../../Dockerfile.dashboard) and served by gunicorn
— separate from the MPC image so it can be run/updated independently on the
server. It is a `dashboard`-profiled service in `docker-compose.yml`:

```bash
docker build -f Dockerfile.dashboard --build-arg GIT_SHA=$(git describe --always --dirty) \
  -t deadentropy/mpc-dashboard:latest .
docker compose --profile dashboard up -d dashboard      # → http://server:8050
```

Profile-gated and read-only → safe to run beside prod; a bare `docker compose up`
never starts it. See [README.md](README.md) for full detail.

## Layout
```
dashboard/
  app.py            # Flask app + file-reading data helpers
  templates/index.html
  static/style.css
  DASHBOARD.md      # this file
  README.md         # run + deploy instructions
```
