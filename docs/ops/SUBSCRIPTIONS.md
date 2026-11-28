# Subscription connections

Connect the subscription once in the owner controls. In the access plans for
both the person and the app, select the exact subscription, for example
`chatgpt-owner`. Granting paid API providers or “all models” does not grant it.

The model list comes from cached discovery. `/v1/models` shows ordinary logical
ids such as `gpt-6.1-terra`, plus separately named aliases. A name such as
`chatgpt-plan/gpt-6.1-terra` is a route pin: it selects only that plan route and
never falls back. It stays callable on Responses and Chat Completions and can be
retrieved with `GET /v1/models/{id}`, but is omitted from the ordinary listing.
Providers shows the connection status and its route pins. Disconnecting retains
the cached model rows and marks that route unavailable. An authorized later
route can remain available.

Owner-authored model rows and exact aliases take precedence. A colliding
discovery stays an execution-only target with its own plan pin; discovery does
not join it to an owner route group or infer equivalence with a paid model.

Kinds are data: `providers/subscriptions/*.yml`. A new provider is a new row, not new code.

## Use your ChatGPT or Claude plan through Azizam

1. In the owner web app, open "Providers". Find "Subscriptions". Choose
   "Connect" in the ChatGPT or Claude row. If Azizam says the sign-in ended,
   choose "Reconnect" instead.
2. If Claude Code is missing, choose "Install Claude Code". Azizam downloads
   and verifies it. When the install finishes, continue in the same dialog.
   Choose "Open ChatGPT sign-in" or "Open Claude sign-in" and sign in using
   your browser. Follow the Azizam dialog to finish. For Claude, sign in with
   your Claude plan. Claude shows a code after you sign in. Paste it here,
   then choose "Connect". If the sign-in finishes by itself, Azizam continues
   without a paste. An existing Claude sign-in connects at once.
3. Check the row. "Ready" means the plan is connected with no current error.
   "Off" means it is not connected or was disconnected. "Down" means it needs a
   new sign-in. "Degraded" means Azizam saw a recent limit or another failure;
   read the line next to it. A healthy row can show usage, the model count
   and when Azizam checked the list, if those details are available. If the
   row offers "Check now", use it to check the model list again.
4. Choose "Open" when the row offers it. The details show the models, their
   pinned names and the route order for the plain model name. A pinned name
   uses only that subscription and never falls back. ChatGPT and Claude do
   not send plan usage to Azizam today, so the connected row's "Plan usage"
   details link to the provider's own Usage page. If a plan sends a reading,
   the details show the percent used and reset time for each limit, and a
   reading that is "out of date" has no current percentage. Azizam always
   shows its own request and token counts for the plan.
5. Open "People" and choose "Access plans". Choose "Edit plan" next to the
   plan you want to change. In the "Subscriptions" fieldset, select the
   connected subscription and save the plan. On the person's row,
   "Access plan" shows which plan applies. The app's access plan must also
   include the same subscription, and the plans must allow the model.
6. If either access plan leaves out the subscription, the person cannot use
   its pinned name. A model with no other allowed route is not available to
   that person. A plain model name can still work through another allowed
   route. Paid fallback occurs only when both the person's and the app's
   access plans allow it. It does not apply to pinned names.
7. Open the row's actions menu and choose "Disconnect". Review who uses the
   plan if shown, then follow the confirmation dialog. Azizam stops using the
   subscription right away and keeps its model list and grants for
   reconnection. For ChatGPT, Azizam also asks ChatGPT to end the sign-in.
   If Azizam shows "Disconnected here", end that sign-in in ChatGPT's
   settings too. For Claude, disconnecting in Azizam leaves Claude signed in
   on that computer. To end that sign-in, use Claude's own sign-out there.

## Owner endpoints (`/admin/api/subscriptions`, owner role only)

| Call | What it does |
|---|---|
| `GET /` | Status of each kind. Never a token, and the read never waits on the network: the stored list answers, and a stale model list is renewed in the background (one task per kind; after a failed renewal, list reads do not try again for 5 minutes). Each connected row carries `models` (slugs), `model_details` (`[{slug, efforts}]`), `models_checked_at`, `status_changed_at`, `last_ok_at`, `last_error` (`{code, at}`, absent when none; the next success clears it; a refusal the client caused, such as a bad request, is not recorded) and, when `status` is `needs_reconnect`, `reconnect_reason` (`refresh_refused` or `revoked`). |
| `POST /{kind}/connect` | Start a sign-in; returns `authorize_url` and `attempt_id`. `{"listen": true}` also opens the optional loopback listener. |
| `POST /{kind}/finish` | `{attempt_id, callback}`: the pasted callback address finishes the attempt (state-checked). This is the normal path. |
| `POST /{kind}/import` | `{path, remove_source?}`: one-time import of an existing registration (see below). |
| `PATCH /{kind}/reasoning` | `{"top_effort": <level or null>}`: the highest reasoning level a request on this subscription may use. A request above it is lowered to the model's highest level at or below it, and the receipt and `x-azizam-plan-effort-lowered-from` say so. `null` means no limit. An unknown level is 400 `invalid_top_effort`. |
| `POST /{kind}/install-runner` | Start a verified stable runner download, or join the install already in progress. Returns the status row with `runner.state` (`missing`, `installing`, `ready`, `failed`), optional `version` and named `error`. |
| `POST /{kind}/refresh-models` | Check the model list now. Returns `{"subscription": <status row>}`, or the named error (for example `plan_not_connected`). |
| `DELETE /{kind}` | Revoke the refresh token, delete the tokens. |

The binary stays in the gateway-owned bin directory (`AZIZAM_PLAN_RUNNER_BIN_DIR`
when set, otherwise `homes_root()/bin`). It does not install into the owner's
home or update itself. Installing again through the API replaces it with the
current stable version. An install failure keeps any previous binary.

## Importing an existing registration (headless users)

`POST /{kind}/import` reads a record file on the gateway host (the owner names the path in the request; nothing
reads a fixed path) with `client_id`, `ext_agent_host_id`, `access_token`, `refresh_token`, `expires_in`, `saved_at`,
`scopes`, `email`, `subject`, `issuer` (the shape `siwc_probe.py` writes). The record becomes the instance connection;
its host id is the instance host id, so the provider sees one registration and the owner signs in only once.

**Sign-in check.** The tokens and a pending sign-in's verifier are secrets in `admin.db` (`provider_secrets`). Connect, finish, import and disconnect each check that the owner's browser sign-in is still live in the same transaction as the write; a sign-out that came first stores or deletes nothing. The local callback listener has no browser request, so it checks the sign-in that started the attempt. A token refresh is started by a request, not by the owner's browser, so it checks none.

**Refresh tokens rotate.** After the import the tokens live only in the gateway's encrypted store, and only the gateway
refreshes them. The first refresh invalidates the refresh token still in the file, so the file is stale from then on:
do not use it again. Pass `"remove_source": true` to delete it after a successful import, or delete it yourself.
An import is refused while a plan is connected (disconnect first); it is allowed when the plan needs to be connected again.

## Request shape

Every plan request is sent as Codex CLI sends its own (`codex-rs/core/src/client.rs`, `build_responses_request`):
`store: false`, `stream: true`, `include: ["reasoning.encrypted_content"]` (the client's own entries stay), the whole
input every turn, `reasoning` as the client sent it, and a `prompt_cache_key`. A client's key is kept; with none, the
gateway derives one from the connection and the conversation's start (instructions and the input up to the first user
message), so every later turn of that conversation sends the same key. A reasoning effort the model does not advertise
is a named 400 `plan_unsupported_reasoning_effort`; it is never dropped.

The key also goes in the HTTP headers the kind lists in `request.cache_affinity_headers`. When the kind names a
`request.turn_state_header`, the gateway also does what Codex does with turn state: the upstream returns a value in
that response header at the start of a turn, and the gateway sends the value back on the later requests of the same
turn, never on a request of another turn. A request continues a turn when its last input item is a tool result (a
Responses `*_output` item, or a chat `tool` message), its input starts with the previous request's whole input, and
the items added since then hold no user, developer or system message. Any other request starts a new turn and sends
no value. Only the first value of a turn is kept. The gateway keeps one value per conversation (the prompt cache key
of one caller and one model on one connection; connecting the plan again starts afresh) in memory only, for at most 4,096 conversations and one idle hour; it never writes
the value to disk, a log or a receipt. The response header `x-azizam-turn-state` and the receipt metadata
`plan_turn_state` say what was done: `started`, `replayed` or `continued` (the turn has no value yet). Remove the row
field to turn this off. Two limits follow from the rule, and both only move routing, never content: two conversations
that share a cache key and the same first message are one conversation to the gateway, so the later one's value wins;
and a client that sends the same tool-result input again (a regenerate) gets the old turn's value.

Usage is the upstream's own counts. Receipts record `input_tokens`, `cached_input_tokens`, `output_tokens` and
`reasoning_tokens`, with `cost.mode = plan` and no dollar amount.

## Limits, errors and chat callers

- The plan call waits up to `upstream.read_timeout_seconds` (300) between bytes; the sign-in calls keep 30.
- A non-streamed reply uses the items the stream finished when the final event carries an empty output.
- An upstream 401 renews the token once and sends the request once more. Only a refresh the provider refuses for good
  ends the connection. A client never sees a 401 from the plan: it gets 503 `plan_connection_invalid`.
- An upstream 400 (or a failed response event with a request-fault code from `failed_codes`) is a named 400
  `plan_request_rejected` in the gateway's own words. A client never receives text an upstream wrote.
- Pause or disable the execution service, for example `chatgpt-plan`, to make
  that edge unavailable before sending. The logical model remains listed if a
  route is authorized; another ready authorized edge can keep it available.
  A pin to the paused service returns `route_unavailable` with status 503.
  Gateway-wide pauses and request admission limits remain terminal.
- Chat callers may send `max_tokens`, `max_completion_tokens`, `temperature`,
  `top_p`, `top_k`, `stop` and `user`. The plan route drops the fields declared
  in the kind's `dropped_fields` before sending, and discloses the sorted names
  in `x-azizam-dropped-params`, separated by commas. No header is added when
  nothing was adjusted. A dropped token cap is not enforced by this route.
  This keeps ordinary chat clients and the Anthropic Messages route working.
- A chat `reasoning_effort: minimal` becomes the lowest advertised effort if
  the model has no `minimal`, as it did on the old path. The original value is
  not sent; the disclosure header also names `reasoning_effort`. Responses
  does not make that normalization.
- Streaming and non-streaming responses carry the same disclosure, including
  the Anthropic route. The receipt records the same names in `ignored_inputs`.
  Only the final served attempt contributes: if a quota refusal advances from
  a plan to a paid route that sends the fields, neither the final header nor
  the receipt claims that the paid route dropped them. Dropping does not
  reorder routes or prefer a later paid route.
- Another unadvertised reasoning effort is a terminal 400
  `plan_unsupported_reasoning_effort`. The message names the requested effort
  and supported efforts, and `param` is `reasoning.effort`. It never silently
  moves the request to a paid route. Local request validation details are safe
  gateway-written text; upstream error text is never echoed.
- Responses fields that the plan does not support, such as `temperature` or
  `max_output_tokens`, get 400 `plan_unsupported_parameter` on a plan-only route:
  “The requested model route could not complete this request.” The Responses
  `unsupported_fields` list takes precedence over `dropped_fields`; for
  example, `temperature`, `top_p` and `user` still refuse. A Responses
  `max_tokens` field is dropped and disclosed instead.

## Tools on the Claude plan

A request to a Claude plan model may carry function tools (Chat `tools`, Responses `tools`, or Messages `tools`).
The `claude` program gets only those tools, through a loopback MCP server that the gateway runs for that request; its
built-in tools stay off. When the model calls tools, the answer ends with the calls (Chat `finish_reason`
`tool_calls`, Messages `stop_reason` `tool_use`, Responses `function_call` items), several at once when the model
calls them in parallel. The run waits for the results. The next request that sends exactly those results continues
the same run, so the prompt cache stays warm.

- `tool_choice` may be `auto` or `none`; `required` or a named tool is refused (`plan_tool_choice_unsupported`).
  `parallel_tool_calls: false` is refused (`plan_parallel_tools_required`), and so are tools that are not functions
  (`plan_tool_type_unsupported`) and `strict` schemas (`plan_strict_tools_unsupported`). Responses `store` must be
  false or absent (`plan_store_unsupported`); `include` may name only `reasoning.encrypted_content`.
- A request that sends some, but not all, results is refused with `plan_tool_results_missing`, which names the
  missing call ids.
- A tool result may hold images: Chat `image_url` parts in a tool message, or Messages image blocks in a
  `tool_result`. The Claude plan hands them to the tool call as images. The ChatGPT plan sends them as `input_image`
  parts of the tool output.
- A waiting run ends after `runner.park_ttl_seconds` (30 minutes by default), after a gateway restart, or when its
  caller has more than `runner.park_limit_per_principal` (4) waiting runs, or its connection more than
  `runner.park_limit_per_connection` (8). A run also ends when the next request changes the history, the tools, the
  model or the effort. The next request then starts a new run with the whole conversation as a transcript, tool calls
  and results included as text. The gateway never runs the caller's tools itself. A Responses answer shows how the
  turn started in `metadata.plan_turn_start` (`new`, `continued` or `rebuilt`).
- Send the same `prompt_cache_key` on every request of one conversation. It keeps that conversation's runs and
  cache apart from the caller's other conversations.
- A waiting run does not count toward `runner.max_concurrent`; `runner.timeout_seconds` bounds each request, not the
  whole tool loop.

The `X-Azizam-Provider` response header names the plan that answered, on Chat, Messages and Ollama alike.

## Claude plan transcripts

The Claude plan runs the local `claude` program, which keeps each conversation as a session file in the plan's config
home (`projects/<folder>/<session>.jsonl`). The gateway resumes that file on the conversation's next turn so the plan's
prompt cache is read. Conversation transcripts therefore stay on the gateway computer until the session expires
(`runner.session_ttl_seconds` in the kind row, one hour by default). The gateway then deletes the file, and also deletes
the file of a run that failed or was cancelled. After a gateway restart it forgets the sessions it holds, so files from
before the restart stay until they are removed by hand.

## Known limit

Cache reads on this route are partial. Before the cache-affinity headers, the
upstream reported almost no cached input. With them, 19 of 24 follow-on turns
hit in one measurement (2026-11-23); the other turns reported zero. The effect of
turn state on those misses is not yet measured: run
`scripts/plan_parity_check.py --tool-loop 5` before and after a change.

## Known gaps

Reviewed and left for later:

- The local callback listener is not hardened beyond state matching and a short life.
- `disconnect` does not take the refresh lock, so a refresh in flight may save tokens just after a disconnect.
- A model slug is not checked against a pattern before it goes into the upstream request body.
- A refusal that arrives mid-stream is sent as one error event, not as deltas.
- The event splitter turns CRLF into LF over the whole buffer, which copies large chunks.
