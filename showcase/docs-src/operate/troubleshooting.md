---
title: Troubleshooting
summary: Find where a request stopped, then take the next step for its error code.
---

# Troubleshooting

Start with the error code and message in the response, not the HTTP status alone. For example, HTTP 503 can mean that the gateway is draining (`gateway_draining`) or that the engines are busy (`resource_admission_denied`). The [error reference](../reference/errors.md) lists the codes and the next step for each.

## Find where the request stopped

Errors from the browser interface's routes include a layer in `error.details.layer`:

| Layer | Where the request stopped |
| --- | --- |
| `request` | The request itself is invalid or incomplete. |
| `account` | Sign-in or account permissions. |
| `library` | A creation, file or job in Library. |
| `gateway` | The gateway is draining, busy or misconfigured. |
| `engine` | The engine cannot serve the model. |
| `model` | The model ran but did not produce a result. |
| `workflow` | The workflow's input mapping. |
| `unknown` | The gateway cannot tell where it stopped. |

## The gateway does not start

- Run `docker compose ps` and check that the containers are running.
- Run `docker compose logs gateway` and read the last lines.
- Check that nothing else uses the port. To use another port, see [Install Azizam](../start/install.md#use-a-different-port).

## An app cannot connect

- Check the base URL. Do not add a second `/v1` or `/api`.
- Check that the key is an app key, not an admin key or a provider key.
- A `127.0.0.1` address points to the device or container that sends the request. Use an address that reaches the gateway from there.

## A request is refused

- Sign in again if the code is `not_authenticated` or `session_user_not_found`.
- Check the app's access plan if a model or protocol is missing. Changing the plan does not need a new key.

## A model is unavailable

- Open **Models** and use a model name that your key can see.
- Check the engine's status. If the status is unknown, check its health settings in [Connect engines](engines.md#read-health-status).

## A job or restart fails

- Do not assume that an interrupted job resumes. Follow the error for that job.
- If a safe restart reports busy, wait for the work to finish. If it reports that the drain endpoint is unavailable, check the local address and the admin key. Do not use `--force`. See [Upgrade and back up](upgrade-and-backup.md#restart-a-gateway-that-runs-as-a-systemd-service).
- If local health passes but the public address fails, check the reverse proxy's route to the gateway.

## Ask for help

Collect the error code, the layer and a short description of the request. Remove keys, cookies, private addresses, prompts and private media first.
