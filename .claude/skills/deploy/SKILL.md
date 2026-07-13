---
name: deploy
description: Deploy the latest MPC thermal controller AND dashboard images to the production server (192.168.5.206). SSHs in as nicolas, brings the running prod container down, pulls the latest images, starts prod + the read-only dashboard back up, and checks the logs to confirm both are healthy. Use when the user says "deploy", "push to server", "update the server", or similar.
---

# Deploy to production

Deploy two images to `192.168.5.206` (user: `nicolas`):

- `deadentropy/mpc-thermal:latest` — the live controller (`prod` service, **safety-critical**: writes setpoints to real ACs).
- `deadentropy/mpc-dashboard:latest` — the read-only web dashboard (`dashboard` service, port 8050; safe to (re)start any time, no side effects on the ACs).

**Prerequisite:** both images must already be on Docker Hub. The dashboard image is built from `Dockerfile.dashboard` — if it has never been built/pushed, the `docker pull` in Step 5 will fail. If the user only wants to ship one of the two, deploy just that service (skip the other's pull/up steps).

## SSH setup (read first)

`/root/.ssh/config` has world-writable permissions (777) on a read-only filesystem — SSH refuses to use it. All SSH/SCP commands must bypass it:

```
-F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no
```

The key at `/tmp/mpc_deploy` is ephemeral (lost on container restart). If it's missing, generate a new one and ask the user to add the public key to `nicolas@192.168.5.206:~/.ssh/authorized_keys`:

```bash
ssh-keygen -t ed25519 -f /tmp/mpc_deploy -C "mpc-deploy" -N ""
cat /tmp/mpc_deploy.pub   # give this to the user to add on the server
```

**Run each `ssh`/`scp` command inline** — exactly as written in the steps below,
starting with the literal `ssh `/`scp ` — do **not** wrap them in a shell
variable (`SSH="ssh …"; $SSH …`). The allow-rules in `.claude/settings.local.json`
match on the command prefix, so a command that starts with `SSH=` instead of
`ssh ` will prompt for approval every time. For the same reason, avoid prefixing
with `echo … &&`; if you need a label, put it in a separate command.

## Step 1 — confirm SSH works and compose directory exists

The compose file lives at `/mnt/smarthome/thermal_controler/docker-compose.yml`.

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 "ls /mnt/smarthome/thermal_controler/docker-compose.yml"
```

If the key is missing/rejected, follow the SSH setup above first. If the compose dir is missing, tell the user and stop. Use `COMPOSE_DIR=/mnt/smarthome/thermal_controler` for all remaining steps.

## Step 2 — copy updated files to the server

Copy the docker-compose file and config directory from the local repo to the server before touching any running containers.

```bash
scp -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no \
  /workspaces/InfluxDb/docker-compose.yml \
  nicolas@192.168.5.206:/mnt/smarthome/thermal_controler/docker-compose.yml
```

```bash
scp -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no \
  /workspaces/InfluxDb/thermal_control/config/control.yaml \
  /workspaces/InfluxDb/thermal_control/config/house.yaml \
  nicolas@192.168.5.206:/mnt/smarthome/thermal_controler/thermal_control/config/
```

(`rsync` is not available in this container — use `scp` for individual files.) If either transfer fails, stop and report — do not proceed with a stale compose file or config.

## Step 3 — check what's currently running

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "cd /mnt/smarthome/thermal_controler && docker compose ps"
```

Note which services are up. A running `dashboard` service is fine and expected — it's read-only and coexists with prod; Step 6 just recreates it from the new image.

## Step 4 — bring down the running containers

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "cd /mnt/smarthome/thermal_controler && docker compose down"
```

Wait for confirmation that the container stopped (the prod service has a 30 s grace period while it writes safe setpoints to the ACs — this is expected).

## Step 5 — pull the latest images

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "docker pull deadentropy/mpc-thermal:latest && docker pull deadentropy/mpc-dashboard:latest"
```

Confirm a digest line (`Digest: sha256:...`) for **each** image. If the controller pull fails, stop and report. If only the *dashboard* pull fails (e.g. it was never built/pushed), you may still proceed with prod — finish the controller steps and skip the dashboard `up` in Step 6, then tell the user the dashboard image is missing.

## Step 6 — start prod, then the dashboard

Start the controller first (the safety-critical service), then the dashboard.

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "cd /mnt/smarthome/thermal_controler && docker compose up -d prod"
```

Then bring the dashboard up. `up -d` recreates it from the freshly pulled image if it changed; it's read-only and profile-gated, so this never affects prod or the ACs:

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "cd /mnt/smarthome/thermal_controler && docker compose --profile dashboard up -d dashboard"
```

## Step 7 — verify both are running

Wait ~5 seconds for startup, then:

```bash
ssh -F /dev/null -i /tmp/mpc_deploy -o StrictHostKeyChecking=no nicolas@192.168.5.206 \
  "cd /mnt/smarthome/thermal_controler && docker compose ps && docker compose logs --tail=40 prod && docker compose logs --tail=20 dashboard"
```

### Healthy signs to look for
**prod:**
- `State: running` (or `Up`) in `docker compose ps`
- `MPC_VERSION` line — confirms which git SHA is live
- `Scheduler starting` or first control-loop tick logged
- No `ERROR`, `Traceback`, or `Exited` in the output

**dashboard:**
- `State: running` (or `Up`), port `8050->5111` mapped in `docker compose ps`
- gunicorn `Booting worker` / `Listening at: http://0.0.0.0:5111` in the logs
- No `Traceback` (e.g. missing config/logs mount)
- Optionally confirm it serves: `curl -sf -o /dev/null -w "%{http_code}\n" http://192.168.5.206:8050/` → `200`

### Unhealthy signs — stop and report
- Container status is `Exited` or `Restarting`
- Traceback in logs
- prod: missing `.env` or weight file errors
- dashboard: missing config/logs mount, or a port-8050 conflict

The dashboard is non-critical: if **only** the dashboard is unhealthy, leave prod running, report the dashboard error, and don't roll prod back for it.

## Safety notes

- Prod writes bang-bang setpoints (65°F = forced ON, 84°F = forced OFF) to real ACs. Always confirm the image pulled successfully before starting it.
- On a clean `down`, the scheduler writes 76°F safe setpoints to all three ACs before exiting — the 30 s grace period covers this.
- If something goes wrong mid-deploy (image pulled but container won't start), the safe state is: ACs revert to their own thermostat logic (no Claude setpoints). Report the error and leave the container down until the user decides.
