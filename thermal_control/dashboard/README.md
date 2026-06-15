# dashboard/

Read-only Flask web dashboard for the MPC. Visualises the schedule the MPC
sees, any active HA override, and the live decision log. **Files only** — reads
config yaml + the MPC logs, never touches Home Assistant, needs no credentials.

See [DASHBOARD.md](DASHBOARD.md) for what each section shows.

## Run locally

```bash
pip install -r thermal_control/requirements.txt
python thermal_control/dashboard/app.py          # → http://localhost:5111
```

By default it reads `remote_logs/` and `thermal_control/config/`. Override the
locations with env vars (this is how the container points at the live logs):

```bash
MPC_LOGS_DIR=/path/to/logs MPC_CONFIG_DIR=/path/to/config \
  python thermal_control/dashboard/app.py
```

## Run in a container (on the server)

Built from [`Dockerfile.dashboard`](../../Dockerfile.dashboard) as its own image
(`deadentropy/mpc-dashboard`), separate from the MPC image. Served by gunicorn.

```bash
# build (mirrors BUILD_HELP.bat conventions)
docker build -f Dockerfile.dashboard --build-arg GIT_SHA=$(git describe --always --dirty) \
  -t deadentropy/mpc-dashboard:latest .

# run on the server — its own profile, safe to run alongside prod
docker compose --profile dashboard up -d dashboard      # → http://server:8050
```

The `dashboard` compose service mounts `thermal_control/logs` and
`thermal_control/config` **read-only** and sets `MPC_LOGS_DIR` /
`MPC_CONFIG_DIR` so it reads exactly what the prod scheduler writes. It writes
nothing and is profile-gated, so a bare `docker compose up` never starts it.

## Env vars
| Var | Default | Purpose |
|-----|---------|---------|
| `MPC_LOGS_DIR`   | `remote_logs/` (repo root) | where `mpc_decision_log.csv` / `user_inputs.log` live |
| `MPC_CONFIG_DIR` | `thermal_control/config/`  | `control.yaml` + `house.yaml` |
