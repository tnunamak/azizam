---
title: Upgrade and back up
summary: Back up the gateway's state, upgrade it, restart it safely, and check the result.
---

# Upgrade and back up

Treat an upgrade as maintenance. Make a backup first, and write down the version you are replacing.

## What to back up

- **The data volume.** In Docker Compose, accounts, apps, keys, settings and receipts are in the `gateway-data` volume, mounted at `/app/data`. The gateway and the execution sidecar both use it.
- **The cache volume.** Creations and the key that signs download links are in the `gateway-cache` volume, mounted at `/app/cache`. Rebuilding the container keeps them. If you lose the key, old download links stop working.
- **Your start-up settings.** Keep your Compose changes, `first-run.yml` changes and your `.env` file, if you use one.

Store backups privately. A container image is not a backup of its data.

To copy both volumes while the containers are stopped, run these commands from the root of the repository. Compose adds the project name to volume names, so find the exact names with `docker volume ls`. This example assumes the project name `azizam`:

```sh
for volume in gateway-data gateway-cache; do
  docker run --rm -v azizam_$volume:/volume:ro -v "$PWD":/backup alpine \
    tar czf /backup/$volume.tar.gz -C /volume .
done
```

The backup files hold your keys and creations. Keep them private.

If you replace an installation that kept creations inside the container (an earlier version), download the creations you want to keep before the first upgrade. They are not in the new volume.

## Before you upgrade

1. Write down the current version and your deployment settings.
2. Choose a time when few people use the gateway.
3. Let replies and media jobs that are running finish.
4. Stop the containers, so nothing writes to the data while you copy it.
5. Back up both volumes and your settings.
6. Check that you can read the backup.

## Upgrade a Docker Compose installation

Get the new version of the repository. Then, from the root of the repository:

```sh
docker compose up -d --build
docker compose ps
```

Use the same settings as before. For example, set the same `AZIZAM_PORT` if you changed it. This command rebuilds the gateway image, including the browser interface, and can recreate the containers. Work that is running when the gateway stops is cut off.

## Check the result

1. Check the local health endpoint:

   ```sh
   curl --fail --silent --show-error http://127.0.0.1:5000/health
   ```

2. If you use a reverse proxy, check the public address too.
3. Sign in as the owner, and check your engines and model names.
4. On **Home**, select **Check connection**.
5. Send a request from an app with an app key.

A healthy endpoint alone does not prove that an app's engine, model and key work. Keep the backup until you have checked the new version. If a check fails, keep the logs and the backup. Do not overwrite your only copy of the old data.

## Restart a gateway that runs as a systemd service

If you run the gateway outside Docker, as a user systemd service, restart it with `scripts/safe-restart` from the root of the repository. A plain service restart can cut off streamed replies and media jobs.

The script needs:

- Python 3, and `systemctl --user` with a unit for the gateway. The default unit name is `azizam`; change it with `--unit`.
- Node.js and npm, when the browser interface needs a rebuild.
- An admin key in `AZIZAM_ADMIN_KEY`, or in the file named by `AZIZAM_ADMIN_KEY_FILE`. The default key file is `~/.config/azizam/admin-key`. Allow only your user to read it.
- A loopback `--url`: `127.0.0.1`, `::1` or `localhost`.

First, see what is running without restarting:

```sh
scripts/safe-restart --dry-run --url http://127.0.0.1:5000 --public-url ''
```

Then restart:

```sh
scripts/safe-restart --url http://127.0.0.1:5000 --public-url https://gateway.example.com
```

Replace the example public address with your own, or pass `--public-url ''` if the gateway has no public address. Always set it: the script's built-in default does not match your installation.

The script:

1. Builds the browser interface if it is missing or out of date. If the build fails, it stops and does not restart anything.
2. Waits until chat has been quiet for 300 seconds. Use `--quiet-seconds` to change this.
3. Drains the gateway: new `POST`, `PUT` and `PATCH` requests get HTTP 503 with the code `gateway_draining` and a `Retry-After` header. Reads, job checks, cancels, sign-in, administration and health checks still work. It waits for running work to finish.
4. Restarts the service and checks local health and, if you set it, the public address.

Read every warning, even when the script exits with 0. Do not use `--force` for a routine restart: it skips the waits and can cut off running work.

| Exit code | Meaning |
| --- | --- |
| 0 | Restarted. Read the printed health and drain results. |
| 1 | Local health, or the public address you set, was not confirmed. |
| 2 | The command options are wrong. |
| 3 | Work was still running at the deadline. Nothing restarted. |
| 4 | The drain endpoint was unavailable or refused. Nothing restarted. |
| 5 | The service restart failed. |
| 6 | Another safe restart is running. |
| 7 | The browser interface build or publication failed. |
| 130 | Interrupted. Read the drain cleanup message. |
