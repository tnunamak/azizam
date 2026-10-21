# Retention slice 3: LiteLLM 1.86.2

Base: `52408b268215585dab40299f2ebc2bd920ab6bff`. Implementation is in
`feat/retention-s3`. No live service or database was changed. Package source
was read from `~/applications/azizam/venv/lib/python3.12/site-packages/litellm/`.
The package and Compose image stay pinned to 1.86.2.

## Settings verified against installed source

Paths below are relative to that installed `litellm/` directory.

| Setting | Value | Installed source |
| --- | --- | --- |
| `general_settings.store_prompts_in_spend_logs` | `false` | `proxy/spend_tracking/spend_tracking_utils.py:1079` |
| `general_settings.disable_spend_logs` | `true` | `proxy/proxy_server.py:4312`; `proxy/db/db_spend_update_writer.py:160` |
| `general_settings.disable_error_logs` | `true` | `proxy/hooks/proxy_track_cost_callback.py:407` (returns before constructing error text or traceback) |
| `general_settings.store_model_in_db` | `false` | `proxy/proxy_server.py:7409` (environment and database can force it on) |
| `litellm_settings.callbacks`, `success_callback`, `failure_callback` | `[]` | `proxy/proxy_server.py:3935,3978,4010` initializes callbacks |
| `litellm_settings.turn_off_message_logging` | `true` | `__init__.py:196`; `litellm_core_utils/redact_messages.py:156` |
| `litellm_settings.redact_messages_in_exceptions` | `true` | `__init__.py:201` |
| `litellm_settings.redact_user_api_key_info` | `true` | `__init__.py:202` |
| `litellm_settings.cache` | `false`; no `cache_params` | `proxy/proxy_server.py:3813,3892` handles cache configuration |
| `router_settings.cache_responses` | `false` | `router.py:251` |
| `LITELLM_LOG` | `CRITICAL` | `_logging.py:86`; `proxy/proxy_server.py:6528` |
| `LITELLM_MODE` | `PRODUCTION` | `__init__.py:19`; `proxy/proxy_cli.py:28` (prevents dotenv loading) |
| `DISABLE_ADMIN_UI` | `true` | `proxy/management_endpoints/ui_sso.py:598` |
| `NO_DOCS`, `NO_REDOC` | `true` | `proxy/utils.py:5761,5744` |
| `maximum_spend_logs_retention_period` | owner opt-in recommendation `7d` | `proxy/proxy_server.py:7479`; `proxy/db/db_transaction_queue/spend_log_cleanup.py:44` |

`registry/litellm_no_content.yml` supplies the values to the config compiler
and launcher. The child inherits runtime basics, declared credential references and the
operational cost-map, HTTP(S) proxy, NO_PROXY and REQUESTS_CA_BUNDLE variables. It does not inherit arbitrary logging, telemetry or debug settings.
Compose supplies the same five fixed environment values and no database URL.

## Metadata decision

Azizam already records cost and usage facts in its own receipts and paid-spend
ledger. No gateway consumer of LiteLLM spend rows was found. Per-request
LiteLLM spend rows, IP metadata, error text and tracebacks add no required
capability, so new spend and error rows are disabled.

LiteLLM's numeric key/user/team spend updates remain necessary for virtual-key
budgets. Its daily accounting keeps the User-Agent tag, model, endpoint and a per-day
request_id. The request_id is the most recently aggregated value for that
day/tag/key/model/provider/endpoint combination, not a list of every request.
A client-supplied `user` field also reaches daily end-user accounting. The
canary checks the real success logger, queued cost increments and budget guard.
It does not disable all spend updates to obtain an empty database.

Tool inventory is suppressed by `gateway/litellm_launcher.py`. LiteLLM is
Azizam's execution substrate, not an engine under the knowledge boundary.
The launcher is the only shim implementation. It checks package version
`1.86.2` and the SHA-256 of
`DBSpendUpdateWriter._enqueue_tool_registry_upsert` before replacing that
method with a no-op. It leaves the surrounding spend writer, numeric counters
and cost accounting unchanged. A version, missing-symbol or implementation
mismatch prints a fixed startup diagnostic and exits 78. The launcher never
modifies installed files. The managed sidecar and the shipped Compose service
both invoke it; Compose mounts this standalone source file read-only.

The verified source is `proxy/db/db_spend_update_writer.py:208-310`, SHA-256
`0f760b1f54019de072223ac6799427e39f4291c6a29dfc20cbaad50063977c43`.
Without the shim it queues names independently of `disable_spend_logs` and
flushes them at `:1086-1097` through
`proxy/db/tool_registry_writer.py:70-130`.
The version-bump and symbol-change tests require deliberate re-verification
on every dependency upgrade. The private launch proof is written only after
the shim is applied; an unpatched or failed launcher cannot authorize content.

The `litellm-spend-cost-metadata` fact in `registry/retention.yml` discloses the
remaining accounting data in the ledger and its assembled "never save" text.
No tool-name exception remains. Old ToolTable records are not removed by this
slice; an owner can purge them separately with their database tools.

Legacy spend cleanup is a separate owner decision. The data file proposes
`maximum_spend_logs_retention_period: 7d` under `legacy_spend_cleanup_proposal`.
That proposal is not applied. An owner can move the value into the file's
`general_settings` to enable the upstream cleanup. An unset period performs
no cleanup. This slice does not delete the existing 58,719 rows or claim that
old rows, backups or WAL are erased.

## Precondition and rehearsal

Managed execution checks the generated launch profile, a private launch proof,
and the process PID/start-time/group and command line. It rejects stale proof
or a different config. The database check uses a read-only transaction with a five-second statement timeout and
returns three booleans, never rows or credentials. It checks config overrides,
key `metadata.logging`, team `metadata.logging` and team
`metadata.callback_settings` (`proxy/litellm_pre_call_utils.py:476-547`).

The check runs at startup, on a process-generation change, after gateway key
writes and hourly while healthy. Refusals retry after 30 seconds, then every
60 seconds until healthy; successful checks restore the hourly interval.
Every problem-state change logs only a fixed-vocabulary slug. Status and the
operator-facing refusal show the slug and the setting-specific fix. Both local chat through LiteLLM and paid execution refuse
with `503 litellm_content_unverified` until it passes. Key listing/deletion
remain available. Direct engine traffic and LiteLLM's internal gateway hop do
not cross this execution gate.

An owner-run endpoint is `unmanaged`, disclosed and usable. Compose checks
`/health/readiness/details` (`proxy/health_endpoints/_health_endpoints.py:1593`)
to distinguish no database from a database the gateway cannot inspect. The
latter is also `unmanaged`. This does not claim that a settings API proves all
content switches. The gateway still checks database rows where it has the URL.

Run the offline rehearsal against a copy of the newly generated YAML:

```sh
./venv/bin/python scripts/rehearse-litellm-content \
  --config /path/to/copied-generated.yaml \
  --database-check /path/to/preflight-flags.json
```

`preflight-flags.json` has exactly `config_unsafe`, `key_unsafe` and
`team_unsafe`, each `false`. Use `--no-database` only for a deployment with no
database. Without either option the command exits 2 and reports
`config_valid_database_unchecked`. These two offline modes open no socket. The opt-in live check below opens a
database connection, runs the same DB_CHECK_SQL inside a read-only transaction
with a five-second statement timeout, and has a ten-second outer timeout. It
reports only a fixed problem slug (or profile_valid), never a URL, row or
exception. A missing URL refuses as database_url_missing. Connection, schema
and permission failures refuse as database_check_unavailable.

After deployment, the lead will run this exact command from the deployed
checkout, in a shell where DATABASE_URL has already been securely supplied:

```sh
./venv/bin/python scripts/rehearse-litellm-content \
  --config data/litellm.config.yaml --database-url-env DATABASE_URL
```

Do not put the URL in argv or print it. This implementation pass did not run
that command against the live database. Deployment still performs the process
generation and database checks.

The existing read-only audit says `LiteLLM_Config`, active virtual-key and team
rows were absent. That supports deployment readiness at the audit time. This
work did not recheck the live database and cannot guarantee it stayed unchanged.

## Verification

The pinned canary runs the real LiteLLM proxy app under an in-process HTTP
client in a fresh process with a private working directory and environment.
Only the outbound transport and Prisma sinks are fake. It drains the real
tool writer and spend/index jobs, captures table writes and numeric queues,
and scans every scratch file plus stdout and stderr in raw, JSON-escaped,
URL-encoded and base64 forms.

Separate markers cover message text, tool arguments, descriptions, schemas and
tool results. They must reach the fake engine and appear in no database sink,
index table or output line. Tool names must also be absent from
`LiteLLM_ToolTable`. Five subprocess cases isolate the controls:

- The unchanged profile keeps all content markers out of every sink.
- Disabling only the tool shim puts the name in ToolTable.
- Removing only LITELLM_LOG puts the prompt in stderr.
- Enabling spend rows alone keeps tool content out of those rows.
- Compared with that spend-row case, enabling only store_prompts_in_spend_logs
  exposes tool names, arguments, descriptions and schemas, even with
  turn_off_message_logging still true.

Each case runs buffered chat, streaming chat, embeddings, 400 and 500 prompt
echoes. The initial budget requests use a synthetic auth dependency; the
separate end-user probe clears that override, rejects an unauthenticated
embedding request and authenticates through the configured X-Litellm-Key
header with a synthetic master key. Its client `user` reaches the real daily
end-user write queue. Daily tag queues contain the User-Agent tag, model,
endpoint and request_id. Prisma sinks remain recording fakes; no real DB is
used by these canaries.

The pinned failure-hook source returns False when disable_error_logs is true
and otherwise falls through to None. A single-setting error-row mutation did
not create rows in this build, so it is not presented as a positive control.
The unchanged profile still asserts zero error rows for both HTTP error cases.

The launcher and sidecar reject NUM_WORKERS/--num_workers values other than 1
and reject --reload before shim imports or database publication. Uvicorn child
interpreters would otherwise start without the process-local shim.

The disposable Compose journey exposed two readiness details: LITELLM_LOG
sets the output handler threshold but leaves the logger level at WARNING,
and the router installs sync_deployment_callback_on_success automatically.
The launcher now sets the three LiteLLM logger levels to CRITICAL as well.
The checker accepts only an empty success-callback list or that one pinned
internal callback, which increments a per-minute in-memory success counter
(router.py:6674); arbitrary callbacks remain refused.

Measured ceilings on 2026-10-21: pytest 15,358 (full hermetic collection),
Vitest 3,051 (`vitest list --json`), Playwright 492 (471 + 3 + 18 across the
three gate configurations), Node 87 (82 Pi cases and 5 compatibility cases).
The Node count run had one unrelated Pi read-only-directory failure: the Pi
CLI tried to create `trust.json.lock` inside that read-only directory. This
slice changes no Node code. Both the installed and project-local Pi CLI
reproduce it; it does not affect the Python retention gate.

The focused profile/canary/management run passed 44 cases. Endpoint/compiler
and authenticated-ledger checks passed 72 cases. The rehearsal returned
`profile_valid` with synthetic clean flags. It was not a live database check.

The final full hermetic run used committed code `76456ccf`:

```sh
scripts/run-hermetic-pytest.sh --junit-xml-out /path/to/full-pass.xml \
  -n 12 --dist worksteal --order-seed=20261020 -q --tb=short
```

Result: **15,354 passed, 3 skipped, 1 xfailed**, plus **3,583 passing subtests**,
in 261.21 seconds. The runner exited 0. JUnit contains zero failures and errors.
The adapter differential family (108 cases), route freeze (5), headless access
(339), retention ledger headless (9), engine knowledge boundary (20), pinned
canary (2) and launcher guards (4) all passed. The 56 changed differential
fixtures were checked against the base: removing the new accounting fact
restores the original JSON exactly.

A preceding full run of the same code had one unrelated timing failure in
`test_ci_local.py::test_the_critical_path_follows_the_web_build_when_it_is_slowest`:
the fake `web-check` lane took longer than the fake browser lanes. The case
passed in isolation and the unchanged full repeat passed. No timing assertion
was weakened. Load15 was below 48 before each full launch.

`git diff --check` passed. Run logs, ceiling listings and JUnit XML are under
`~/.tmp/azizam-retention-s3/` on the implementation host. No live gateway,
LiteLLM or PostgreSQL connection was used for this verification. The pinned
Compose image was checked statically; a Compose container was not started.


## Review follow-up verification

The initial committed regression run reproduced 28 failures (34 passed). It
covered refusal diagnostics/recovery, safe operational environment variables,
read-only rehearsal, durable evidence links, legacy-row disclosure and worker
spawn refusal. The five isolated mutation canaries subsequently passed.
The separate Compose regression also failed first for effective logger levels
and the automatically installed router callback.

Rows written before this change remain until purged. This applies to spend
rows, error rows and tool inventory. No purge, live database check, gateway
restart or LiteLLM restart was performed. The lead owns the purge after
operator approval.
