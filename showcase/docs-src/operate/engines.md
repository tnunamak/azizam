---
title: Connect engines
summary: Add an engine, map model names, and read its health status correctly.
---

# Connect engines

An engine is a server that does model work: chat, pictures, speech or video. The gateway keeps a record for each engine. The record holds its address, its model names, its health probe and any changes the gateway makes to requests for it.

These records are settings. Use the values for the engines you actually run. No engine or model name in these docs is required.

## Add an engine

1. Open **Engines**, in the **Gateway** group of the menu, and select **Add engine**.
2. Choose a preset if one fits, then enter a name and the engine's base URL. The address must use `http://` and be reachable from the gateway. In Docker Compose, see [Connect to an engine on the same computer](../start/install.md#connect-to-an-engine-on-the-same-computer).
3. If the engine needs a key, put the key in an environment variable of the gateway, and enter the variable's name in **Key environment variable**. In Docker Compose, add the variable to the `.env` file in the repository, then run `docker compose up -d` again.
4. Select **Test connection**, then add the engine.
5. Select **Detect models**, and map each model name that apps will use to the engine's own model ID.

Then check the engine's status and send a real request, for example from **Try**, before you tell other people to use it.

## Change an engine safely

- Save a copy of the current settings before you change them.
- Change one engine at a time.
- After the change, check both its status and a real request.

The gateway applies a saved change without a restart.

## Read health status

A health probe can report one of three things: healthy, not healthy, or unknown. Unknown does not mean healthy.

An engine record can declare positive health evidence:

- `health_endpoint`: the address the gateway probes.
- `health_success_path`: a dot-separated path into the probe's JSON response.

For example, if a probe response contains a `system` object, set `health_success_path` to `system`. A 2xx response whose selected value is not null and not false counts as healthy. Explicit failure, starting and busy states take precedence.

Without a success path, a 2xx JSON response counts as healthy only when it says so in a common form: a non-empty model list, a `status` of `ok`, `up`, `healthy` or `ready`, or a true `ready`, `available`, `ok` or `healthy` field. Any other response stays unknown. That is why `/v1/models` works for an OpenAI-compatible engine.

## Change request and response shapes

Some engines need small changes to requests, such as a renamed or dropped parameter. Put these in the engine record as data, not in the gateway's code.

Before you change one, save an example request and response with secrets removed. After the change, compare the new request, response and errors. Test with the value left out as well as with it set. A healthy probe does not prove that the request is right.

Read [Engines and models](../concepts/engines-and-models.md) for how engines, models and workflows fit together, and [Troubleshooting](troubleshooting.md) when an engine is reachable but requests fail.
