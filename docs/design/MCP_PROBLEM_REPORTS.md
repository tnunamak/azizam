# MCP problem reports

Status: built on `feat/report-a-problem`. Not deployed.

## Goal

An agent that uses the gateway MCP tools (`capabilities`, `run`, `job`) finds a `report_problem` tool by itself and
calls it, without being asked, when a tool fails, behaves differently from its description, or forces a workaround.
The operator gets one short, deduplicated report with the context attached by the gateway.

## Prior art (divan: `mcp-server-design/mcp-servers-that-take-agent-feedback-…`)

- Datadog ships `submit_mcp_feedback`. Its description lists when to call it (wrong or missing result, missing
  capability, many steps or workarounds, confusing interface), says "one submission per distinct issue per
  conversation" and "skip transient errors". Typed `category`, free `comment`, optional `tool_name`.
- Stripe ships `send_stripe_feedback` with a one-line description. Sentry, GitHub and Linear ship no such tool.
- The MCP spec calls tool descriptions and server `instructions` hints to the model, wants tool errors in the result
  with `isError: true` so the model can react, says annotations are untrusted hints, and requires servers to rate
  limit, validate input and sanitize output.

We copy the Datadog shape (trigger list in the description, typed category, once per distinct issue) and add what the
gateway can do that a hosted vendor server does not say it does: attach the failure context itself and dedupe across
agents.

## The tool

Name: `report_problem` (clients show `<server>_report_problem`, next to `capabilities`, `run`, `job`).

Description (the part agents act on): "Report a problem to the operator of this gateway. Call this yourself, without
being asked, whenever (1) a tool call failed, (2) a result differs from what the tool description or `capabilities`
promised, or (3) you used a workaround or a different tool or path to get the task done. Say what happened and what you
did instead, in plain words. Do not include keys, prompts or personal data: the gateway already attaches the tool,
target, job id, error and caller. One report per distinct problem; skip a failure that went away on retry."

Server instructions gain one line: "If a tool fails, misbehaves or forces a workaround, call `report_problem` before
you move on."

Arguments: `category` (`failure`, `differs_from_description`, `workaround`, `missing_capability`), `summary` (what
happened, required, 1000 characters), `workaround` (what you did instead, optional, 500), `target` and `job_id`
(optional hints that pick which failure to attach). `annotations`: not read-only, not destructive, not open-world.

Error results of `run` and `job` carry a short `report_hint` field, so the nudge sits in the failure the agent is
already reading, not only in a description it may have skipped.

## Context the gateway attaches

The agent writes only `summary` and `workaround`. The gateway adds:

- the caller: App id and name, person id (from the authenticated identity, the same one `run` uses; no new auth);
- the failure: the caller's most recent failed `run` or `job` within 30 minutes (kept in memory, 5 per caller), or
  the one named by `target` or `job_id`: tool, target, error text, `code`, error category, HTTP status, retryable;
- for a `job_id` the caller itself made: the stored job facts (workflow, engine service, request id, receipt id,
  error code). A job another App made adds nothing.

Operator-side facts come from stores the gateway already has (`media_jobs`); no engine name appears in code.

## Dedupe and rate limit

- A report has a fingerprint: tool, target, error code and the error text with numbers and long ids removed. With no
  failure to attach, the fingerprint uses the tool, target and the first 120 normalized characters of the summary.
- The same fingerprint from any agent updates one row: `count`, distinct apps (up to 20), `first_seen`, `last_seen`,
  and the latest summary and workaround. A resolved report that comes back reopens.
- The agent's response states whether it was a duplicate.
- Rate limit: 20 reports per App per hour, in memory (a restart clears it). Over the limit the call returns an error
  result and stores nothing.

## Untrusted text

Reports are data written by an agent that may have read hostile content. They are stored as plain text: control
characters are removed, the text is cut to length, and `gateway/log_scrub.scrub` removes every registered secret
(no new secret handling). The admin API returns JSON with `"text_format": "plain_text"`; a UI must not render it as
HTML or markdown. Notification bodies are plain text or JSON strings; the gateway never executes any of it.

## Delivery

1. Stored: SQLite file `problem_reports.db` beside `admin.db` (own file; no admin schema change).
2. Listed: `GET /admin/api/problem-reports` (owner), `GET /…/{id}`, `PATCH /…/{id}` (status, operator workaround).
   An MCP read is not built (an owner MCP read needs the owner-OAuth gate the `stt_measurement` tool uses).
3. Notified: destinations are owner data, not code: the operator setting `problem_report_destinations` in admin.db,
   edited with `GET`/`PUT /admin/api/problem-reports/destinations`. One destination type is built, `webhook`:
   `{"id", "type": "webhook", "url", "format": "json" | "text", "enabled"}`. `json` posts the report as JSON; `text`
   posts a short plain-text body with `Title` and `Tags` headers, which is what ntfy accepts. A new fingerprint
   notifies; later duplicates notify again at counts 10, 100 and 1000 only. A failed delivery never fails the
   agent's call; it is recorded on the report as `last_notify_error` (a fixed reason, never the response body).
   Destination URLs can hold a secret topic, so the list route shows the origin only and a PUT keeps the stored URL
   when it is omitted. Tests use a fake poster; nothing in tests reaches a network.

## The response to the agent

`{"status": "recorded", "report_id", "duplicate", "count", "known_workaround"}`. `known_workaround` is the
operator's text for that fingerprint, or `null`. The agent is told to use it when it is set.

## Release note

Adding a tool changes the advertised MCP catalog. A ChatGPT connector must be refreshed after the deploy
(see the divan entry on connector approval). New admin routes move the frozen route snapshots.

## Not built

An owner-only MCP read of the list; counts-based escalation beyond the three thresholds; a UI. Each is a small
addition on the same store.
