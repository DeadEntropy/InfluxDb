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

### Config-error banner
- A red banner pinned to the top of the page whenever the config **currently on
  disk** (`control.yaml` / `house.yaml`) is invalid — broken YAML syntax or a
  missing required key. This mirrors the scheduler: a bad edit is rejected and
  the live MPC keeps running on its last-good config, so the dashboard falls back
  to that same last-good config and just adds the banner (the rest of the page
  keeps working).
- Liveness is re-validated on every render with the shared
  `control/config_check.py::validate_config_structure` — the **same** check the
  scheduler runs before adopting an edit — so the banner clears the instant the
  config is fixed, even though `errors.log` still records the past breakage.
  `errors.log` (written by the scheduler) only supplies the "since" time.

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
- **On/off + room-temps plots** — one interactive Plotly chart per AC zone
  (bedroom/living/extension). AC on/off is always shown as a thick red/blue
  strip along the bottom (blue = on, red = off) so it doesn't dominate the
  temperature lines. Each room's temperature (distinct color per room) is
  plotted on the right-hand °F axis; clicking a room in the legend hides/shows
  its temp line together with its target-range band (same color, two dotted
  lines — the piecewise-constant upper/lower bound, not filled). A left-hand
  control panel (1D/3D/1W/1M/ALL/Custom) picks the time window shown across
  all three panels.
  Band values are resolved per-row with the same `resolve_targets_for_rooms()`
  the live MPC uses (`thermal_control/control/schedule.py`), so weekday/
  weekend schedule entries and (item 8) away/holiday mode are both correct for
  that row's actual historical moment — away periods are reconstructed from
  `away_activated`/`away_deactivated` events in `user_inputs.log`, since away
  isn't itself a decision-log column. Manual overrides and presence-based
  "unoccupied" bands are *not* reconstructed historically (only away is), so a
  room that was under an active override or briefly unoccupied at some past
  tick will show its plain scheduled band there instead.
  Data comes from `GET /plots/onoff_data.json?ac=<id>&range=...` (JSON);
  rendering happens client-side in `static/plots.js` so switching the range or
  toggling a room never reloads the page. Plotly.js itself is served locally
  at `/vendor/plotly.min.js` (sourced from the pinned `plotly` pip package,
  not a CDN) so the dashboard stays fully self-contained/offline.
  The static matplotlib version of this figure (3 panels in one PNG, with
  breach markers, schedule-only bands) still exists in `onoff_plot.py` as
  `make_onoff_plot` — it's what the `live-analysis` skill uses for its offline
  `ac_onoff.png`, just no longer wired into this page.

## Data sources (all files)
| File | Used for |
|------|----------|
| `thermal_control/config/control.yaml` | schedule, bands, override duration |
| `thermal_control/config/house.yaml`   | room list, AC-zone grouping |
| `remote_logs/mpc_decision_log.csv`     | current + recent decisions, temps, costs |
| `remote_logs/user_inputs.log`          | active overrides / presence events |
| `remote_logs/errors.log`               | "since" time for the config-error banner |

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
