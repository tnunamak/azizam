---
title: Install Azizam
summary: Start the gateway with Docker Compose and connect it to an engine on the same computer.
---

# Install Azizam

This page installs the gateway with Docker Compose on one computer. At the end, the gateway runs on that computer and its setup page opens in a browser.

## Before you start

You need:

- Docker with the Compose plugin.
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

You do not need a `.env` file for this local setup. Continue with [First run](first-run.md).

## Use a different port

The gateway publishes port 5000 on the computer's loopback address only. Other devices cannot reach it yet. See [Security](../operate/security.md) before you open it to other devices.

To use another port, set `AZIZAM_PORT` when you start Compose. This example uses 5001:

```sh
AZIZAM_PORT=5001 docker compose up -d
```

Then open `http://127.0.0.1:5001/setup`. Use the same setting every time you run a Compose command that recreates the gateway.

## Connect to an engine on the same computer

The gateway runs in a container, so `127.0.0.1` inside the container is the container itself, not your computer. Use `host.docker.internal` to reach your computer from the gateway.

For example, an engine that listens on port 8080 of your computer has this address:

```text
http://host.docker.internal:8080/v1
```

An engine that listens only on the computer's loopback address is not reachable from the container. Make the engine listen on an address the container can reach, and use your firewall to restrict who else can reach it.

If the Compose network conflicts with another network on your computer, set `AZIZAM_DOCKER_SUBNET` and `AZIZAM_DOCKER_GATEWAY` together. See [Configuration](../reference/configuration.md#listener-and-network).

## Where your data lives

| Data | Where | Kept when you replace the container |
| --- | --- | --- |
| Accounts, apps, keys, settings and receipts | Named volume `gateway-data`, at `/app/data` | Yes |
| Creations (pictures, audio, video) and the key that signs download links | `/app/cache` inside the gateway container | No |

Keep the `gateway-data` volume when you rebuild or replace containers. Creations are not in the volume, so replacing the gateway container deletes them. Download the creations you want to keep before you upgrade.

Read [Upgrade and back up](../operate/upgrade-and-backup.md) before you replace an installation.

## Next step

[First run](first-run.md): create the owner account, connect your engine and get a first reply.
