---
title: First run
summary: Create the owner account, connect a chat engine, and get a first reply in your app.
---

# First run

This tutorial takes a new gateway to its first reply. You create the owner account, connect one chat engine, create an app key and send a message.

Before you start, [install Azizam](install.md) and make sure your chat engine is running with a model loaded.

The examples below use the default port, 5000. If you changed AZIZAM_PORT during installation, replace 5000 in every URL on this page with that port. Run curl on the gateway computer. The app URL also assumes the app runs directly on that computer. For an app on another device or in another container, first configure a reachable gateway address using the Security guide.

## 1. Create the owner account

On the computer that runs the gateway, open:

```text
http://127.0.0.1:5000/setup
```

Create the owner account. The owner can configure the gateway and invite other people.

## 2. Connect your engine

1. Open **Engines**, in the **Gateway** group of the menu, and select **Add engine**.
2. Enter a name and your engine's OpenAI-compatible base URL. Use an `http://` address that the gateway can reach. For an engine on the same computer, see [Connect to an engine on the same computer](install.md#connect-to-an-engine-on-the-same-computer).
3. If the engine needs a key, first [configure engine credentials](install.md#configure-engine-credentials) for both the gateway and execution sidecar. Enter that variable's name in **Key environment variable**. Do not paste the key itself.
4. Select **Test connection**, then add the engine.
5. On the engine, select **Detect models** to read the engine's models. Map the model name apps will send to the engine's actual model ID. For example, map the public name `my-chat` to the exact ID returned by your engine; do not substitute `my-chat` for that ID unless it really is the engine's ID.

This tutorial uses `my-chat` as the model name. It is an example, not a built-in model. Use your own name in the steps below if you chose a different one.

Check the engine's health settings as well. For an OpenAI-compatible engine, a models endpoint such as `http://host.docker.internal:8080/v1/models` can provide positive health evidence when it returns a non-empty model list. Use your engine's real address and port, and avoid adding `/v1` twice. An unknown health status is not a successful connection; see [Read health status](../operate/engines.md#read-health-status). A real chat reply is still the end-to-end test.

The engine and its model names are settings. You can change or replace them later without changing the gateway.

## 3. Create an app key

Open **Home**. Choose the app you want to connect, then select **Create key for** that app.

Save the key when it appears. It is shown only once.

Use app keys in apps. Do not put an admin key in an app.

## 4. Get a first reply

In your app, use these settings:

- Base URL: `http://127.0.0.1:5000/openai/v1`
- API key: the app key you saved
- Model: `my-chat`

Send a short message from your app, or use the curl request below. A reply means that the app key, gateway, model mapping and engine work together; a gateway health check alone does not establish this.

### Send the same request with curl

Run this example in Bash on the gateway computer. If your terminal uses another shell, run bash first. It reads the key without echoing it and asks for a streamed reply. Your curl must support --fail-with-body.

```bash
read -r -s -p 'App key: ' AZIZAM_API_KEY && printf '\n' &&
curl --no-buffer --fail-with-body --silent --show-error \
  http://127.0.0.1:5000/openai/v1/chat/completions \
  -H "Authorization: Bearer $AZIZAM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"model":"my-chat","stream":true,"messages":[{"role":"user","content":"Say hello in one sentence."}]}'
unset AZIZAM_API_KEY
```

The reply arrives as a stream of events. The text is in `choices[0].delta.content`. The stream ends with `data: [DONE]`.

### If there is no reply

- **The request is refused.** Check that you used an app key, not an admin key, and that you copied all of it.
- **The model is not found.** Open **Models** to see the model names your key can use.
- **The engine is busy or starting.** Check **Status**, then try again.

See [Troubleshooting](../operate/troubleshooting.md) for error codes.

## Next steps

- [Invite people](../operate/invite-people.md) to use your gateway.
- [Connect more engines](../operate/engines.md) for images, video or audio.
- Before other devices connect, read [Security](../operate/security.md).
- Before you upgrade, read [Upgrade and back up](../operate/upgrade-and-backup.md).
