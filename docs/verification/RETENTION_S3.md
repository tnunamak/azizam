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
| `litellm_settings.callbacks`, `success_callback`, `failure_callback` | `[]` | `proxy/proxy_server.py:3818` onward loads the module settings and callbacks |
| `litellm_settings.turn_off_message_logging` | `true` | `__init__.py:196`; `litellm_core_utils/redact_messages.py:156` |
| `litellm_settings.redact_messages_in_exceptions` | `true` | `__init__.py:201` |
| `litellm_settings.redact_user_api_key_info` | `true` | `__init__.py:202` |
| `litellm_settings.cache` | `false`; no `cache_params` | `proxy/proxy_server.py:3818` onward handles cache configuration |
| `router_settings.cache_responses` | `false` | `router.py` constructor's `cache_responses` argument |
| `LITELLM_LOG` | `CRITICAL` | `_logging.py:86`; `proxy/proxy_server.py:6528` |
| `LITELLM_MODE` | `PRODUCTION` | `__init__.py:19`; `proxy/proxy_cli.py:28` (prevents dotenv loading) |
| `DISABLE_ADMIN_UI` | `true` | `proxy/management_endpoints/ui_sso.py:598` |
| `NO_DOCS`, `NO_REDOC` | `true` | `proxy/utils.py:5761,5744` |
| `maximum_spend_logs_retention_period` | owner opt-in recommendation `7d` | `proxy/proxy_server.py:7479`; `proxy/db/db_transaction_queue/spend_log_cleanup.py:31` onward |

`registry/litellm_no_content.yml` supplies the values to the config compiler
and launcher. The child inherits only runtime basics and declared credential
references. It does not inherit arbitrary logging, telemetry or debug settings.
Compose supplies the same five fixed environment values and no database URL.

## Metadata decision

Azizam already records cost and usage facts in its own receipts and paid-spend
ledger. No gateway consumer of LiteLLM spend rows was found. Per-request
LiteLLM spend rows, IP metadata, error text and tracebacks add no required
capability, so new spend and error rows are disabled.

LiteLLM's numeric key/user/team spend updates remain necessary for virtual-key
budgets. Its aggregate daily accounting remains operational metadata. The
canary checks the real success logger, queued cost increments and budget guard.
It does not disable all spend updates to obtain an empty database.

Tool names remain operational metadata, as the owner directed on 2026-10-20.
LiteLLM 1.86.2 auto-discovers them for its registry and tool-policy lookup;
Azizam does not read that inventory. The write is independent of
`disable_spend_logs`: `proxy/db/db_spend_update_writer.py:210-309` enqueues the
name and attribution; `:1086-1097` flushes it; `proxy/db/tool_registry_writer.py:70-130`
upserts `LiteLLM_ToolTable`. `proxy/schema.prisma:1177-1199` lists its columns.
No disable switch was found. There is no startup patch or shim.

The `litellm-tool-names` fact in `registry/retention.yml` appears in the current
ledger and its assembled "never save" text. It names what is kept, why it is
kept, who can read it, and how to purge it. To purge unwanted names, the owner
stops chat, backs up their database and deletes the selected rows from
`LiteLLM_ToolTable` with their database tools. Later tool calls can recreate
the names. This procedure was documented, not executed.

Legacy spend cleanup is a separate owner decision. The data file proposes
`maximum_spend_logs_retention_period: 7d` under `legacy_spend_cleanup_proposal`.
That proposal is not applied. An owner can move the value into the file's
`general_settings` to enable the upstream cleanup. An unset period performs
no cleanup. This slice does not delete the existing 58,719 rows or claim that
old rows, backups or WAL are erased.

## Precondition and rehearsal

Managed execution checks the generated launch profile, a private launch proof,
and the process PID/start-time/group and command line. It rejects stale proof
or a different config. The database check uses a read-only transaction and
returns three booleans, never rows or credentials. It checks config overrides,
key `metadata.logging`, team `metadata.logging` and team
`metadata.callback_settings` (`proxy/litellm_pre_call_utils.py:476-547`).

The check runs at startup, on a process-generation change, after gateway key
writes and hourly. Both local chat through LiteLLM and paid execution refuse
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
`config_valid_database_unchecked`. The script starts nothing, opens no socket
and never connects to PostgreSQL. It prints no config values or credentials.
Deployment still performs the real generation and database check.

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
index table or output line. Tool names must appear in `LiteLLM_ToolTable`,
which proves that the allowed metadata exception is exercised. The leaky
positive control enables spend/error logging and uses the default log level;
it must expose content to both the database scanner and output scanner.

Final full-suite results and measured ceilings are recorded below after the run.
