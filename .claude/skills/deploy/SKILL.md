---
name: deploy
description: Deploy the latest MPC thermal controller image to the production server (192.168.5.206). SSHs in as nicolas, brings the running prod container down, pulls the latest image, starts it back up in prod mode, and checks the logs to confirm it's healthy. Use when the user says "deploy", "push to server", "update the server", or similar.
---

# Deploy to production

Deploy `deadentropy/mpc-thermal:latest` to `192.168.5.206` (user: `nicolas`).

## Step 1 — confirm the compose directory exists

The compose file lives at `/mnt/smarthome/thermal_controler/docker-compose.yml`.

```bash
ssh nicolas@192.168.5.206 "ls /mnt/smarthome/thermal_controler/docker-compose.yml"
```

If missing, tell the user and stop. Use `COMPOSE_DIR=/mnt/smarthome/thermal_controler` for all remaining steps.

## Step 2 — check what's currently running

```bash
ssh nicolas@192.168.5.206 "cd /mnt/smarthome/thermal_controler && docker compose ps"
```

Note which services are up. **Never start both `shadow` and `prod` at once** — if shadow is running, bring it down too.

## Step 3 — bring down the running containers

```bash
ssh nicolas@192.168.5.206 "cd /mnt/smarthome/thermal_controler && docker compose --profile prod down"
```

Wait for confirmation that the container stopped (the prod service has a 30 s grace period while it writes safe setpoints to the ACs — this is expected).

## Step 4 — pull the latest image

```bash
ssh nicolas@192.168.5.206 "docker pull deadentropy/mpc-thermal:latest"
```

Confirm the digest line (`Digest: sha256:...`) appears. If the pull fails (network, auth), stop and report.

## Step 5 — start prod

```bash
ssh nicolas@192.168.5.206 "cd /mnt/smarthome/thermal_controler && docker compose --profile prod up -d prod"
```

## Step 6 — verify it's running

Wait ~5 seconds for startup, then:

```bash
ssh nicolas@192.168.5.206 "cd /mnt/smarthome/thermal_controler && docker compose ps && docker compose logs --tail=40 prod"
```

### Healthy signs to look for
- `State: running` (or `Up`) in `docker compose ps`
- `MPC_VERSION` line — confirms which git SHA is live
- `Scheduler starting` or first control-loop tick logged
- No `ERROR`, `Traceback`, or `Exited` in the output

### Unhealthy signs — stop and report
- Container status is `Exited` or `Restarting`
- Traceback in logs
- Missing `.env` or weight file errors

## Deploying shadow mode instead

If the user says "deploy shadow" or passes `shadow` as an argument:

```bash
ssh nicolas@192.168.5.206 "cd /mnt/smarthome/thermal_controler && docker compose up shadow"
```

Shadow runs in the foreground for 24 h then exits — do **not** use `-d`. Tail the output live if the user wants to watch it.

## Safety notes

- Prod writes bang-bang setpoints (65°F = forced ON, 84°F = forced OFF) to real ACs. Always confirm the image pulled successfully before starting it.
- On a clean `down`, the scheduler writes 76°F safe setpoints to all three ACs before exiting — the 30 s grace period covers this.
- If something goes wrong mid-deploy (image pulled but container won't start), the safe state is: ACs revert to their own thermostat logic (no Claude setpoints). Report the error and leave the container down until the user decides.
