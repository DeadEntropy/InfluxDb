# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two related projects in one repo:

1. **Root level** — helper scripts/notebooks for pulling Home Assistant history out of InfluxDB (`influx_kwh.py`, `fetch_states.py`, the `*.ipynb` notebooks). These produced the CSV training data (`homeassistant_temp.csv`, `homeassistant_states.csv`).
2. **`thermal_control/`** — the main project: a Model Predictive Controller (MPC) for a 3-zone residential AC system in Davie, FL. It reads room temperatures from Home Assistant every 10 minutes, evaluates all 2³ = 8 AC on/off combinations against a learned thermal model, and writes bang-bang setpoints (65°F = force ON, 84°F = force OFF) back to the AC controllers.

`thermal_control/README.md` is the authoritative project doc (problem statement, model performance, design-decision history). `thermal_control/home_thermal_control.md` is the original spec — the implementation has since diverged from it (e.g. MILP was replaced by bang-bang enumeration). Each subfolder (`config/`, `control/`, `model/`, `preprocess/`) has its own README.

## Commands

There are no tests and no linter configured. Python 3.11, plain scripts (no package install; `sys.path` manipulation at the top of entry points makes `thermal_control.*` imports work).

**Always run from the repo root** (`/workspaces/InfluxDb`):

```bash
pip install -r thermal_control/requirements.txt   # MPC deps (root requirements.txt is for the Influx notebooks)

# Pipeline, in order (only needed when retraining):
python thermal_control/preprocess/01_stale_data.py   # then 02_filter, 03_ac_states, 04_merge
python thermal_control/model/fit.py                  # one RidgeCV model per room → model/weights/
python thermal_control/model/simulate.py             # validation (Pass 1 + Pass 2)

# Run modes:
python thermal_control/dry_run.py      # one MPC solve, print decision, write nothing
python thermal_control/shadow_run.py   # 24h loop: log MPC decisions vs real thermostats, write nothing
python thermal_control/scheduler.py    # LIVE 10-min control loop — writes setpoints to real ACs
```

**Shadow deployment** (Docker image run on a remote server, see `BUILD_HELP.bat`):

```bash
docker build --no-cache -f Dockerfile.shadow -t mpc-shadow .
docker tag mpc-shadow deadentropy/mpc-shadow:latest && docker push deadentropy/mpc-shadow:latest
# on the server: docker compose up  (uses docker-compose.yml; logs land in thermal_control/logs/)
```

The root `Dockerfile` is unrelated — it's the JupyterLab devcontainer image.

## Environment / secrets

- Root `.env`: `INFLUX_HOST/PORT/USERNAME/PASSWORD/DBNAME` (Influx scripts) plus `HA_URL`, `HA_TOKEN`.
- `thermal_control/.env`: `HA_URL`, `HA_TOKEN` only — this is what the MPC entry points load.
- Both `.env` files contain real credentials; never commit or print them.

## Architecture (thermal_control)

Data flow: `preprocess/` (CSV cleaning → `train.csv`/`val.csv`) → `model/fit.py` (one RidgeCV per room, saved to `model/weights/` as joblib + `feature_lists.json`) → `model/simulate.py` exposes `HouseSimulator`, the forward simulator reused at runtime.

Runtime loop (`scheduler.py` / `shadow_run.py` / `dry_run.py`):
- `ha_bridge/controller.py` — the only Home Assistant touchpoint (REST API, reads sensors, writes setpoints).
- `control/schedule.py` — resolves time-of-day comfort bands from `config/control.yaml` (wide band 65–85°F = "MPC ignores this room").
- `control/forecast.py` — outdoor temperature series for the horizon (Open-Meteo forecast optional via `use_forecast`).
- `control/mpc.py` — `BangBangMPC`: enumerates the 8 AC combinations, rolls each out 18 steps (3 h) with `HouseSimulator`, picks min(discomfort + energy_weight × energy).

Configuration is data, not code: `config/house.yaml` (rooms, sensor entities, AC zone mapping, adjacency) and `config/control.yaml` (comfort bands, schedule, MPC parameters). Prefer editing these over hardcoding room/AC names.

## Conventions and gotchas

- **All runtime temperatures are °F** (sensors, setpoints, comfort bands, MPC internals). Model validation MAE is reported in °C in the README. Don't mix them up.
- 8 rooms are modelled; the others have no sensors or were deliberately excluded (master_bath, laundry_room — see README). The AC controllers' own sensors sit in *different rooms* than the zones they cool — that mismatch is the entire reason this project exists.
- Outdoor temperature comes from the pool soffit sensor ("Pool Temp" column) — it is air temperature, not water.
- AC hardware switches on whole-°F thresholds with no hysteresis; setpoints written to HA must be integers.
- Sensor data quality: −58°F sentinel = offline sensor; ≥8 h of identical readings = dead battery (stale). Both are filtered in `preprocess/`.
- Dates in this project run through 2026; the training overlap window is May 2025 – Jun 2026.
- `thermal_control/CODE_REVIEW.md` is a point-in-time review of `scheduler.py`/`shadow_run.py` — check it before re-reviewing that code.
- Large generated files live at the root (`homeassistant_*.csv`, `shadow.csv`, notebooks) — avoid reading them whole; sample with pandas instead.
