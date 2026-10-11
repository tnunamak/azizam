# Old executor and loader retirement

Audited from `5f7ddc4b` on `legacy/batch6b-old-executor`.

The requested base already includes `0efb7051`, which closed the unmatched
route and deleted the old executor and loader. This follow-up removes only
two fields left behind by that deletion. It does not migrate a public route.

## Caller evidence

The audit parsed all 578 tracked Python files with `ast`, recording definitions,
name and attribute references, assignment contexts, string references and each
enclosing function. It also searched tracked gateway, tests, scripts and docs
for the retired names, and read the remaining dynamic lookup sites.

| Symbol | Evidence at the audited base | Disposition |
| --- | --- | --- |
| `proxy_request` | No definition, call, alias or string lookup in tracked Python. Deleted in `0efb7051`. | Already retired. |
| `load_model` | No definition, call, alias or string lookup in tracked Python. Deleted in `0efb7051`. | Already retired. |
| `TABBY_LOAD_TIMEOUT` | No runtime reference. Two mentions remain in the historical lifecycle design. | Already retired; preserve design history. |
| `MAX_LOAD_FAILURES`, `LOAD_FAILURE_BACKOFF` | No runtime reference. | Already retired. |
| `backend_for_host`, `MODEL_REQUIRED_PATHS` | No runtime reference. Historical audit mentions are not callers. | Already retired. |
| `load_event`, `load_failure_count`, `last_load_failure_time`, `last_load_error`, `reset_load_failures`, `record_load_failure`, `can_attempt_load` | No tracked Python reference. | Already retired. |
| `ProxyState.model_loading` | Only assignment is `False` in `__init__`. Only reads are guards in `unload_model` and `maintenance_loop`; no setter remains. | Remove field and inert guards. |
| `ProxyState.unload_event` | Initialized to `None`; allocated, signalled and cleared only by `unload_model`. No waiter, external reader or dynamic lookup remains. | Remove field and unused event effects. |

Dynamic lookup review: `reload_runtime_config` assigns only names returned by
`_derive_backend_constants`; none are retired symbols. The lifecycle driver's
lazy `import proxy` reads `config`, not loader functions or state fields. No
`getattr`/`setattr`/`vars`/`__dict__` access to these state fields exists. Test
monkeypatches and route decorators are covered by the name/attribute/string
scan; a new runtime absence test also checks the imported module and state.

The registered `/{path:path}` endpoint is `unmatched_route(request, path)`.
It authenticates, strips a recognized path token from the error message, and
returns 404. It declares no query parameter and performs no backend dispatch.
The existing HTTP tests exercise true, false, repeated and invalid
`mapped_route` query values through the actual FastAPI router.

## Live paths retained

Public OpenAI routes enter `_handle_openai_litellm_request`, which authenticates
and applies route policy before `_forward_litellm_proxy_request`. The loaded
model branch uses `_forward_current_loaded_openai_request`. Anthropic and
Ollama facades retain their translations and registered routes. Namespaced
native requests retain `provider_native_request` and its policy and receipt
handling. None call the retired executor or loader.

Local activation still uses `ensure_target` / `_ensure_target_leased` /
`_ensure_target` and the configured lifecycle driver's `ensure_ready` or
`swap`. Its service data and `gateway/engine_quarantine.py` are unchanged.

`lifespan` creates `maintenance_loop`, which calls `unload_model` after the
idle timeout and successful health and loaded-model probes. This is live
maintenance, so `unload_model`, `model_unloading`, `load_lock`,
`check_tabby_health`, `is_model_loaded`, and `get_tabby_model_config` stay.
The config reader also supplies the live lifecycle params provider and model
metadata. `_highest_priority_chat_backend` and `NO_ACTIVITY_PATHS` also have
live discovery and canonical dispatch callers.

## Tests and limitations

No existing test is removed in this follow-up. The earlier `0efb7051` removed
`TestBackendForHost`, the discovery helper-only test
`test_backend_model_passthrough_is_classified_as_unmapped`, and four
`test_direct_native_*` receipt cancellation tests that invoked the retired
executor directly. Their retained route contracts are tested by HTTP routing,
canonical lifecycle, cancellation and receipt-owner tests.

`test_loader_retirement.py` verifies absent symbols and checks idle unload's
POST URL, method, credential header, empty body, timeout, return value,
duplicate rejection, failure and cancellation cleanup, and maintenance
conditions. Before removal, its state-absence tests fail for exactly
`model_loading` and `unload_event`; all eleven behavior cases pass.

No live engine, operator state or external client is used. Hermetic tests
prove fixture behavior and route selection; they do not qualify a real engine
deployment or discover external Python consumers outside this repository.
