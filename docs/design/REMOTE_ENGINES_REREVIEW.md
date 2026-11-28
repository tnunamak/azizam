# Remote engine re-review receipts

Eight commits address the eight findings on 1edaf03b6. All Python checks below
used the hermetic runner on committed trees; no live checkout, service restart,
external engine or real credential was used. The owner will run the next review
and release gate. The remaining scenario xfail is S1; S6 passes.

The third item needs proof rather than another client implementation: both graph
callers already enter NodeGraphAdapter.watch's explicit refusal before dialing,
and Realtime refuses before upgrading. Tests exercise the background listener
with an unmarked connection and replace the WebSocket dial with a fail sentinel.
The fourth item adds cross-process publication checks to both HTTP and WebSocket
entry points, including a connection carrying the service's former address.

For the fifth item, a fresh integer generation check precedes each read/write,
with SQLite off the loop and policy parsing cached. A child process commits an
owner opt-out through the real writer; the parent refuses I/O without a watcher
or in-memory callback. The watcher is now shared per service/database/loop, and
operation slots (default 4, maximum 16 from data) bound active work separately
from connections. Open responses retain their operation slots until closed.

Layering and workflow migrations preserve the enabled remote record's authority,
and workflow-only writers leave that record byte-for-byte unchanged. They need
no system actor with extra authority. Failed reloads and rollbacks retain legacy
remote denials until restart; subsequent I/O never relies on a watcher interval.

## Regression node IDs

### Item 1

```text
tests/test_c2d2_scenarios.py::test_s6_admin_can_disable_remote_service_but_only_owner_can_reenable[disabled]
tests/test_c2d2_scenarios.py::test_s6_admin_can_disable_remote_service_but_only_owner_can_reenable[enabled]
```

### Item 2

```text
tests/test_speech_lifecycle.py::RemoteRefusalTests::test_remote_legacy_speech_route_answers_typed_refusal
```

### Item 3

```text
tests/test_remote_security_review.py::test_remote_graph_websocket_guard_precedes_actual_dial[adapter]
tests/test_remote_security_review.py::test_remote_graph_websocket_guard_precedes_actual_dial[listener]
```

### Item 4

```text
tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload[http]
tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload[websocket]
tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection[audit]
tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection[coupled]
tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection[plain]
tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection[transaction]
tests/test_c2d2_scenarios.py::test_s6_registry_write_protects_next_legacy_send_before_hot_reload[False]
tests/test_c2d2_scenarios.py::test_s6_registry_write_protects_next_legacy_send_before_hot_reload[True]
tests/test_remote_security_review.py::test_remote_boot_map_protects_before_first_snapshot_and_send
```

### Item 5

```text
tests/test_c2d2_scenarios.py::test_s6_external_process_revoke_stops_next_socket_io_without_watcher[read]
tests/test_c2d2_scenarios.py::test_s6_external_process_revoke_stops_next_socket_io_without_watcher[write]
```

### Item 6

```text
tests/test_remote_security_review.py::test_remote_operation_slot_lasts_until_response_close
tests/test_remote_security_review.py::test_remote_service_shares_one_watcher_and_caps_active_operations
```

### Item 7

```text
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[17]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[False]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[None]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[True]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[enabled]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[raw3]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[raw6]
tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load[raw7]
```

### Item 8

```text
tests/test_c2d2_scenarios.py::test_s6_startup_migrations_preserve_remote_authority[False]
tests/test_c2d2_scenarios.py::test_s6_startup_migrations_preserve_remote_authority[True]
tests/test_workflow_registry_store.py::test_workflow_writer_without_remote_actor_preserves_remote_record
```

## Commands and observed pass lines

These gates overlap; their counts must not be added. Diagnostic failures were
fixed before push, and only passing receipts are quoted here. Later publication
checks were folded into item 4 to retain one commit per finding.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-1.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_admin_can_disable_remote_service_but_only_owner_can_reenable
```

Observed output: `2 passed in 9.56s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-2-fixed.xml -n 4 tests/test_speech_lifecycle.py::RemoteRefusalTests tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py
```

Observed output: `45 passed in 13.04s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-3.xml -n 4 tests/test_remote_security_review.py::test_remote_graph_websocket_guard_precedes_actual_dial tests/test_realtime_proxy.py::GateRefusalTests::test_remote_enabled_service_refuses_before_readiness_or_websocket
```

Observed output: `3 passed in 7.88s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-4-corrected.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_registry_write_protects_next_legacy_send_before_hot_reload tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection tests/test_remote_security_review.py::test_remote_boot_map_protects_before_first_snapshot_and_send
```

Observed output: `7 passed in 9.04s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-5.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_external_process_revoke_stops_next_socket_io_without_watcher tests/test_remote_security_review.py::test_remote_generation_change_stops_next_socket_io tests/test_remote_security_review.py::test_remote_authority_cache_runs_off_loop_and_ignores_unrelated_revision
```

Observed output: `4 passed in 10.72s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-6.xml -n 4 tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py
```

Observed output: `152 passed, 1 xfailed in 22.56s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-7-fixed.xml -n 4 tests/test_remote_security_review.py::test_remote_malformed_policy_is_typed_and_excluded_on_load tests/test_service_registry.py
```

Observed output: `146 passed in 8.26s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-8.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_startup_migrations_preserve_remote_authority tests/test_workflow_registry_store.py tests/test_workflow_schema_migration.py tests/test_registry_migration.py tests/test_registry_layers.py
```

Observed output: `200 passed in 41.85s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-4-cli.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload tests/test_c2d2_scenarios.py::test_s6_registry_write_protects_next_legacy_send_before_hot_reload tests/test_c2d2_scenarios.py::test_s6_every_registry_writer_refreshes_legacy_remote_protection tests/test_engine_egress_choke_point.py
```

Observed output: `41 passed in 60.70s (0:01:00)`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-cli-all-wires.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload tests/test_remote_security_review.py::test_remote_graph_websocket_guard_precedes_actual_dial tests/test_realtime_proxy.py
```

Observed output: `32 passed in 13.89s`.

## Owner oracle proposal and limits

This re-review adds 29 collected cases in the targeted files, on top of the 71
cases already added by this branch relative to train 66. The owner-only proposal
at tests/budgets.toml:678 is `pytest = 29830` to `pytest = 29930`, subject to the
owner's complete release collection. No oracle, reviewed list, parity golden,
shipped manifest or frozen route graph was edited.

Operation and connection ceilings are per serving event loop. Sleeping or blocked
work is checked by the shared watcher; scheduler/database latency affects that
observation. The next I/O always checks the committed generation. No full-suite,
live deployment or sustained-load claim is made.

Expect it to work because real owner/admin routes, child-process writes, the
checked connector and startup migrations exercise the authority boundaries
without replacing them with product-function mocks. Confidence: 0.87 pending the
owner's independent re-review and release gate.

## Final combined gate

The final production/test source tree is 7c88d183124325e327374fa9753c52630b1e9f24;
the closing amendment adds documentation receipts only. All 29 new cases pass
in this gate, together with S6, the boundary guards, legacy callers, byte parity
and the unchanged frozen route graph.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-rereview-release.xml -n 4 tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py tests/test_engine_egress_choke_point.py tests/test_engine_control_address.py tests/test_lifecycle_drivers.py tests/test_lifecycle_behavior_gaps.py tests/test_node_graph_adapter.py tests/test_inline_tts_stream_cleanup.py tests/test_engine_credential_exposure.py tests/test_projection_registry.py tests/test_service_registry.py tests/test_media_jobs_contract.py tests/test_config_c10_sections.py tests/test_operation_runner.py tests/test_route_graph_freeze.py tests/test_realtime_proxy.py tests/test_speech_lifecycle.py tests/test_workflow_registry_store.py tests/test_workflow_schema_migration.py tests/test_registry_migration.py tests/test_registry_layers.py
```

Observed output: `1215 passed, 1 xfailed in 111.17s (0:01:51)`.
