---
title: Client setup examples
summary: Connect common chat apps, editors and image tools to your gateway.
---

# Client setup examples

Every client needs three things: the gateway address with the right base path, an app key, and a model name that the key can use. Menus and file formats change between client versions, so treat these as examples.

For most chat apps and editors, use the OpenAI-compatible base URL. Use the Anthropic or Ollama routes only when a client requires them.

## Check the connection first

Run this on the same computer, or in the same container, as the client:

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/openai/v1/models" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

`127.0.0.1` reaches the computer or container that sends the request. If the gateway runs somewhere else, use its address.

## Open WebUI

In **Settings → Connections → OpenAI**:

- Base URL: your gateway address followed by `/openai/v1`
- API key: your app key

Refresh the model list. Model file management through Ollama panels does not work through the gateway. Picture making needs an image engine on the gateway.

## LibreChat

Add a custom endpoint to `librechat.yaml`. Replace `my-chat` with a model name from your gateway.

```yaml
endpoints:
  custom:
    - name: "Azizam"
      apiKey: "${AZIZAM_API_KEY}"
      baseURL: "http://127.0.0.1:5000/openai/v1"
      models:
        default: ["my-chat"]
        fetch: true
```

Set `AZIZAM_API_KEY` in LibreChat's environment. Do not write the key into the file. Features that need the OpenAI Assistants API do not work.

## Editors with an OpenAI Compatible provider

For editor extensions such as Cline:

- API provider: OpenAI Compatible
- Base URL: your gateway address followed by `/openai/v1`
- API key: your app key
- Model ID: a chat model from the gateway's model list

Tool calls use the OpenAI tool format. The model must support tool calls.

## Continue

Use an OpenAI provider with the `/openai/v1` base URL. Choose a chat model for chat and autocomplete, and a model that supports embeddings for embeddings. One model may not do both. Set context size and other runtime options on the engine, not in the editor.

## SillyTavern pictures

Choose **Stable Diffusion Web UI (AUTOMATIC1111)** as the image source.

- SD WebUI URL: the gateway address, without `/v1` or `/sdapi/v1`
- Authentication: `gateway:` followed by your app key

Connect and choose an image model. Turn off Hires fix, Restore faces and scripts. Read the [A1111 limits](a1111.md#limits) before you turn on other settings.

## When setup fails

- Check the base URL. Do not add a second `/v1` or `/api`.
- Check that the key is an app key.
- Refresh the model list, and choose a name that this gateway returns.
- If a protocol or model is missing, ask the owner to check the app's access plan.
- Keep keys and signed URLs out of anything you share.

For an agent, see [MCP for agents](mcp.md).
