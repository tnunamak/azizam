---
title: Install Azizam
summary: Start the gateway with Docker Compose and connect it to an engine on the same computer.
---

# Install Azizam

This page installs the gateway with Docker Compose on one computer. At the end, the gateway runs on that computer and its setup page opens in a browser.

## Before you start

You need:

- Docker with a Compose plugin that supports optional env_file entries (required: false).
- A copy of the Azizam repository.
- Network access to download container images and build dependencies.
- An engine that serves an OpenAI-compatible chat API, with a model loaded. You connect it in [First run](first-run.md).

Compose starts two containers: the gateway and its execution sidecar. Neither one downloads a model.

## Start the gateway

Open a terminal in the root of the repository and run:

```sh
docker compose up -d
docker compose ps
```

The first build takes the longest. It installs the web dependencies and builds the browser interface.

When the gateway is running, open this address in a browser on the same computer:

```text
http://127.0.0.1:5000/setup
```

You do not need a `.env` file for a local engine that does not require credentials. For a host-based engine or one that requires a key, complete the connection setup below before continuing with [First run](first-run.md).

The gateway health check probes `/health`; a running or healthy gateway does not prove that an engine can answer. The `litellm` sidecar waits for `/app/data/litellm.config.yaml` before starting its server, so a running sidecar container alone is not a readiness check. Finish engine setup and send the real request in First run.

## Use a different port

The gateway publishes port 5000 on the computer's loopback address only. Other devices cannot reach it yet. See [Security](../operate/security.md) before you open it to other devices.

To use another port, set `AZIZAM_PORT` when you start Compose. This example uses 5001:

```sh
AZIZAM_PORT=5001 docker compose up -d
```

Then open `http://127.0.0.1:5001/setup`. Use the same setting every time you run a Compose command that recreates the gateway.

## Connect to an engine on the same computer

The gateway and execution sidecar run in separate containers, so `127.0.0.1` inside either container is that container itself, not your computer. Both need to reach the engine. The supplied Compose file adds `host.docker.internal` only to the gateway; add the same alias to the sidecar instead of relying on Docker to provide it automatically.

Create `compose.override.yaml` beside `docker-compose.yml` with the following content, or merge this service setting into an existing override:

```yaml
services:
  litellm:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Apply it from the repository root:

```sh
docker compose up -d
```

Compose automatically reads this override alongside `docker-compose.yml`. Preserve your `AZIZAM_PORT` and network settings when recreating containers.

For example, an engine that listens on port 8080 of your computer has this address:

```text
http://host.docker.internal:8080/v1
```

An engine that listens only on the computer's loopback address is not reachable from the container. Make the engine listen on an address the container can reach, and use your firewall to restrict who else can reach it.

If the Compose network conflicts with another network on your computer, set `AZIZAM_DOCKER_SUBNET` and `AZIZAM_DOCKER_GATEWAY` together. See [Configuration](../reference/configuration.md#listener-and-network).

## Configure engine credentials

Skip this section if your engine does not require a key. Otherwise, choose an environment variable name, such as `MY_ENGINE_API_KEY`, and put its value in the repository's local `.env` file. Keep that file private; do not commit it or paste the value into the browser's **Key environment variable** field.

The supplied Compose file loads `.env` into the gateway, but not into the `litellm` sidecar. Pass the engine credential to the sidecar too. Add this setting under `services.litellm` in `compose.override.yaml`, alongside `extra_hosts` if you added it above:

```yaml
services:
  litellm:
    environment:
      MY_ENGINE_API_KEY: ${MY_ENGINE_API_KEY:?Set MY_ENGINE_API_KEY in .env}
```

Use your chosen variable name in both places. This forwards only that variable to the sidecar rather than every value in `.env`. Then recreate the containers so both processes receive the new environment:

```sh
docker compose up -d
```

In First run, enter `MY_ENGINE_API_KEY` (the name, not its value) as the engine's key environment variable. Repeat the recreation after changing its value. `AZIZAM_LITELLM_API_KEY` is the gateway-to-sidecar credential; it is not your engine provider key.

## Where your data lives

| Data | Where | Kept when you replace the container |
| --- | --- | --- |
| Accounts, apps, keys, settings and receipts | Named volume `gateway-data`, at `/app/data` | Yes |
| Creations (pictures, audio, video) and the key that signs download links | Named volume `gateway-cache`, at `/app/cache` | Yes |

Keep both volumes when you rebuild or replace containers. `docker compose up -d --build` keeps them. `docker compose down --volumes` deletes them, and so does `docker volume rm`. If you installed an earlier version that kept creations inside the container, download the creations you want to keep before your first upgrade. They are not in the new volume.

Read [Upgrade and back up](../operate/upgrade-and-backup.md) before you replace an installation.

## Next step

[First run](first-run.md): create the owner account, connect your engine and get a first reply.
