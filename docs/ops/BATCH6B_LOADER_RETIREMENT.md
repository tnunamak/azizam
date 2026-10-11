# Old executor and loader retirement

Round 1 audited from `5f7ddc4b` on `legacy/batch6b-old-executor`.
Round 2 repeated the audit after rebasing onto `35a57055`, which includes
the requested `044904cb` base and the subsequent route-inventory tests.

The requested base already includes `0efb7051`, which closed the unmatched
route and deleted the old executor and loader. This follow-up removes only
two fields left behind by that deletion. It does not migrate a public route.

## Caller evidence

The resumed audit parsed all 579 tracked Python files with `ast`, recording
definitions, imports, name and attribute references, assignment contexts and
string references. It also searched tracked gateway, tests, scripts and docs
for the retired names, and read the remaining dynamic lookup sites. The only
Python references to retired symbols are strings in absence and guard tests.

| Symbol | Evidence at the audited base | Disposition |
| --- | --- | --- |
| `proxy_request` | No definition, call, alias or runtime string lookup in tracked Python. Deleted in `0efb7051`. | Already retired. |
| `load_model` | No definition, call, alias or runtime string lookup in tracked Python. Deleted in `0efb7051`. | Already retired. |
| `TABBY_LOAD_TIMEOUT` | No runtime reference. Two mentions remain in the historical lifecycle design. | Already retired; preserve design history. |
| `MAX_LOAD_FAILURES`, `LOAD_FAILURE_BACKOFF` | No runtime reference. | Already retired. |
| `backend_for_host`, `MODEL_REQUIRED_PATHS`, `_should_return_not_found_before_backend` | No runtime reference. Historical audit mentions are not callers. | Already retired. |
| `load_event`, `load_failure_count`, `last_load_failure_time`, `last_load_error`, `reset_load_failures`, `record_load_failure`, `can_attempt_load` | No runtime reference; only absence-test strings. | Already retired. |
| `ProxyState.model_loading` | Only assignment is `False` in `__init__`. Only reads are guards in `unload_model` and `maintenance_loop`; no setter remains. | Remove field and inert guards. |
| `ProxyState.unload_event` | Initialized to `None`; allocated, signalled and cleared only by `unload_model`. No waiter, external reader or dynamic lookup remains. | Remove field and unused event effects. |

Dynamic lookup review: `reload_runtime_config` assigns only names returned by
`_derive_backend_constants`; none are retired symbols. The lifecycle driver's
lazy `import proxy` reads `config`, not loader functions or state fields. No
`getattr`/`setattr`/`vars`/`__dict__` access to these state fields exists. Test
monkeypatches and route decorators are covered by the name/attribute/string
scan; a new runtime absence test also checks the imported module and state.
Computed test lookups use `_derive_backend_constants`, fixed discovery and
forwarder name lists, media-job names or video dictionaries; none resolve to
retired symbols. The metadata test's `vars(proxy)` copies the namespace only
to run catalog filtering from its fixture.

The registered `/{path:path}` endpoint is `unmatched_route(request, path)`.
It authenticates, strips a recognized path token from the error message, and
returns 404. It declares no query parameter and performs no backend dispatch.
The existing HTTP tests exercise truthy, repeated false/true and invalid
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

The old executor and loader's direct-helper dependencies were compared with
the remaining proxy definitions. Every surviving helper has other callers:
canonical forwarding, authentication, body parsing, header preparation,
availability, health and config readers remain live.

## Tests and limitations

No existing test is removed in this follow-up. The earlier `0efb7051` removed
`TestBackendForHost` (three helper tests), the two hot-reload tests
`test_backend_for_host_uses_new_host_after_reload` and
`test_backend_for_host_old_url_no_longer_resolves`, and the discovery helper
test `test_backend_model_passthrough_is_classified_as_unmapped`. It also
removed four receipt cancellation tests that invoked the retired executor:

- `test_direct_native_begins_before_health_effect_and_finishes_failure`
- `test_direct_native_health_cancellation_finalizes_cancelled_and_reraises`
- `test_direct_native_autoload_cancellation_finalizes_cancelled_and_reraises`
- `test_direct_native_dispatch_cancellation_finalizes_once_before_later_effects`

Their retained route contracts are tested by HTTP routing, canonical lifecycle,
cancellation and receipt-owner tests.

`test_loader_retirement.py` verifies absent symbols and checks idle unload's
POST URL, method, credential header, empty body, timeout, return value,
duplicate rejection, failure and cancellation cleanup, and maintenance
conditions. Before removal, its state-absence tests fail for exactly
`model_loading` and `unload_event`; all eleven behavior cases pass.

The static guard in `test_engine_knowledge_boundary.py` rejects the five
retired executor, loader and retry names in `proxy.py` or any `gateway/*.py`
module, including nested packages. It matches whole names in calls, definitions,
imports and literal dynamic lookups, while live `unload_model` and
`_forward_litellm_proxy_request` remain allowed. A synthetic self-test verifies
all five matches and those live-name exclusions. Deliberately constructed
strings can evade this lexical guard; the caller audit separately checks the
current computed lookup domains.

No live engine, operator state or external client is used. Hermetic tests
prove fixture behavior and route selection; they do not qualify a real engine
deployment or discover external Python consumers outside this repository.

## Verification record

All test commands use the hermetic runner and a committed checkout. The
round-1 test-only commit `9e781e36` was reset after these baseline checks:

```sh
scripts/run-hermetic-pytest.sh -q tests/test_loader_retirement.py -k loader_exclusive
```

Result: **2 failed, 7 passed, 18 deselected, 1 warning**. The failures were the
absence assertions for `model_loading` and `unload_event`.

```sh
scripts/run-hermetic-pytest.sh -q tests/test_loader_retirement.py -k 'idle_unload or maintenance'
```

Result: **11 passed, 16 deselected, 1 warning** before the source change.

At the original source commit `8f5b9ec1`, the required batch tests plus
retirement and registered catch-all tests passed:

```sh
scripts/run-hermetic-pytest.sh -q tests/test_loader_retirement.py tests/test_catchall_never_dispatches.py tests/test_discovery_metadata.py tests/test_route_registry.py tests/test_proxy_gateway_acceptance.py tests/test_runtime_route_policy.py tests/test_litellm_internal_lifecycle_route.py tests/test_litellm_cancellation_boundary.py tests/test_receipt_lifecycle_cancellation.py tests/test_receipt_owner_boundaries.py tests/test_protocol_forward_auth.py tests/test_engine_knowledge_boundary.py tests/test_route_policy_inventory.py
```

Result: **395 passed, 55 subtests passed, 3 warnings** in 620.56 seconds.

After rebasing onto `35a57055` and adding the static guard, the required batch
tests plus retirement coverage passed at `f9ae5b6a`:

```sh
scripts/run-hermetic-pytest.sh -q tests/test_discovery_metadata.py tests/test_route_registry.py tests/test_proxy_gateway_acceptance.py tests/test_runtime_route_policy.py tests/test_litellm_internal_lifecycle_route.py tests/test_litellm_cancellation_boundary.py tests/test_receipt_lifecycle_cancellation.py tests/test_receipt_owner_boundaries.py tests/test_protocol_forward_auth.py tests/test_engine_knowledge_boundary.py tests/test_route_policy_inventory.py tests/test_loader_retirement.py
```

Result: **234 passed, 55 subtests passed, 3 warnings** in 111.89 seconds.
The warnings concern deprecated dependency APIs.

The final full suite runs once against committed `f9ae5b6a`:

```sh
scripts/run-hermetic-pytest.sh -q
```

Result: **1 failed, 6,821 passed, 6 skipped, 1 xfailed, 21 warnings,
2,801 subtests passed** in 2143.15 seconds. This follow-up introduces no
skip or xfail. The runner's frozen checkout was compared with `f9ae5b6a`
for all three changed Python files; their bytes match. An earlier
round-1 full run was interrupted before its pytest summary and does not
establish completed verification.

Independent source review of `35a57055..f9ae5b6a` returned **PASS**, with no
blocking findings. It verified the caller, dynamic lookup, route, lifecycle
and guard claims without executing the gateway. Test evidence is recorded
above separately. `git diff --check` and the deterministic prose check pass.

## Full-suite failure attribution

The full-suite failure is
`tests/test_route_policy_inventory.py::test_http_category_resolved_check_inventory[categories]`.
Its decoy-grant pass records `GET /api/ps` returning 200 with an empty models
list, while backend tripwires record `health` and `network connection`.

Baseline comparisons use a separate detached worktree at committed
`35a57055`, without any retirement diff. The failing case passes in isolation:

```sh
scripts/run-hermetic-pytest.sh -q 'tests/test_route_policy_inventory.py::test_http_category_resolved_check_inventory[categories]'
```

Result: **1 passed, 1 warning** in 9.34 seconds.

Collecting the whole suite but executing only that case also passes:

```sh
scripts/run-hermetic-pytest.sh -q -k test_http_category_resolved_check_inventory
```

Result: **1 passed, 6,799 deselected, 1 warning** in 14.03 seconds.

Executing the preceding admin, reload and registry tests reproduces the
identical `/api/ps` failure on the unchanged base:

```sh
scripts/run-hermetic-pytest.sh -q tests/test_hot_reload.py tests/test_proxy_admin_integration.py tests/test_registry_migration.py 'tests/test_route_policy_inventory.py::test_http_category_resolved_check_inventory[categories]'
```

Result: **1 failed, 164 passed, 1 warning, 29 subtests passed** in 67.59 seconds.
Thus the failure predates this retirement and depends on preceding execution,
not collection alone. The full suite is not green; this report does not
qualify its remaining category-policy behavior.

The minimal baseline reproduction is two existing tests in this order:

```sh
scripts/run-hermetic-pytest.sh -q tests/test_proxy_admin_integration.py::ConfigExportTests::test_export_includes_seeded_backends 'tests/test_route_policy_inventory.py::test_http_category_resolved_check_inventory[categories]'
```

Result: **1 failed, 1 passed, 1 warning** in 11.32 seconds, with the same
status, body and two backend tripwires. The config-export test adds a chat
service through the admin API, which invokes `reload_runtime_config`; its
teardown restores its store path and authentication state but leaves the
derived backend globals in place. The later inventory fixture grants only
a decoy service. `ollama_ps` applies an early `models.running` check, then
`_ollama_loaded_model_summaries` probes the retained backend set without a
resolved-service check. This path and both existing tests are unchanged by
the retirement. Fixing that policy path or test isolation is separate work;
no assertion was weakened to make this run pass.
