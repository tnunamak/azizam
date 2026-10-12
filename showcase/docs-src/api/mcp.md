---
title: MCP for agents
summary: Connect an agent to the gateway's media tools over Streamable HTTP with an app key.
---

# MCP for agents

The gateway runs an MCP server at `/mcp`. Connect with Streamable HTTP, and send your app key in an `Authorization: Bearer` header.

A request without a key gets HTTP 401 with a protected-resource metadata challenge. The gateway has no OAuth server, so use a client that can send the key as a header.

## Tools

| Tool | What it does |
| --- | --- |
| `capabilities` | Lists the media workflows and model names you can use. Pass `target` to see one target's inputs. Filter with `query` and `intent`. |
| `run` | Starts a target with inputs. `mode` is `auto`, `sync` or `async`. For async work, `wait_seconds` defaults to 30, up to 120. |
| `job` | Reads a job by `job_id`, waits for it, or cancels it with `cancel: true`. |

Call `capabilities` before you build inputs. Do not assume that an example model exists.

Results include signed URLs in `resource_link` blocks and asset records in `structuredContent`. For image and video inputs, use an asset ID, a signed asset URL from this gateway, or a data URI. The server does not fetch other URLs.

Requests use the same access plan and media routes as other clients. A refusal comes back as a tool error. Check `isError` and `structuredContent`, not only the HTTP status.

## Claude Code

With `AZIZAM_BASE_URL` and `AZIZAM_API_KEY` set:

```sh
claude mcp add gateway "$AZIZAM_BASE_URL/mcp" --transport http \
  -H "Authorization: Bearer ${AZIZAM_API_KEY}"
```

The client stores the header in its configuration. Protect that file.

## Pi with pi-mcp-adapter

Add the server to `.mcp.json`. Replace the example address with your gateway's address, and keep the key in the environment:

```json
{
  "mcpServers": {
    "gateway": {
      "url": "https://gateway.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${AZIZAM_API_KEY}"
      },
      "protocolVersion": "auto"
    }
  }
}
```

`protocolVersion` can be `legacy`, `auto` or `2026-07-28`.

## Test with curl

This request lists capabilities. It does not make anything.

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/mcp" \
  -H "Authorization: Bearer $AZIZAM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"capabilities","arguments":{}}}'
```

## Protocol details

- Protocol revisions: `2026-07-28`, and the earlier `2025-03-26`, `2025-06-18` and `2025-11-25`.
- The server is stateless. It issues no session IDs and has no SSE streams. `GET` and `DELETE` on `/mcp` return HTTP 405.
- With the newest revision, the version, method and tool name headers must match the request body. A compatible MCP client sets them.
- The server offers tools only. Resources, prompts, sampling, elicitation, tasks and subscriptions are not supported.

MCP is not in the gateway's [OpenAPI document](/openapi.json). Use `capabilities` for each target's inputs.
