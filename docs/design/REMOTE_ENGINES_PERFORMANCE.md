# Remote engine performance checks

Legacy HTTP and WebSocket sends compare SQLite data_version inline before
using their destination map. An unchanged registry needs no worker hop.
During a registry read failure, known remote destinations remain refused and
local destinations outside that set continue serving. A child-process owner
write is detected on the next legacy send without hot reload.

Remote socket I/O uses a persistent connection per factory and serializes
snapshot refreshes per service. Eight concurrent checks after a changed
generation submit exactly one worker; the next unchanged checks submit none.
Unreadable authority still refuses the next socket read or write. The existing
child-process opt-out tests pass without a watcher or an in-process callback.

The recorder transferred 67,108,864 bytes through the real HTTP/TLS connector
in 1,024 body reads of 65,536 bytes. A separate SQLite connection committed a
new generation halfway through. The measured counts were 5 worker hops, 2
snapshot queries and 4,127 inline PRAGMA data_version queries. The test bounds
worker hops at 6; inline comparisons scale with I/O checks. It generates body
chunks on demand rather than allocating a 64 MiB fixture.

## Commands and results

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-perf-legacy.xml -n 4 tests/test_remote_performance.py tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload tests/test_engine_egress_choke_point.py tests/test_realtime_proxy.py
```

Observed output: `68 passed in 55.53s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-perf-remote-fixed.xml -n 4 tests/test_remote_performance.py tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py
```

Observed output: `172 passed, 1 xfailed in 27.67s`.
The remaining xfail is S1; S6 passes. The first attempt had four fixture setup
errors from a missing fixture import; that import was fixed before this run.

## Regression node IDs

```text
tests/test_remote_performance.py::test_legacy_unchanged_registry_needs_no_worker_and_retains_denials[False-False]
tests/test_remote_performance.py::test_legacy_unchanged_registry_needs_no_worker_and_retains_denials[False-True]
tests/test_remote_performance.py::test_legacy_unchanged_registry_needs_no_worker_and_retains_denials[True-False]
tests/test_remote_performance.py::test_legacy_unchanged_registry_needs_no_worker_and_retains_denials[True-True]
tests/test_remote_performance.py::test_remote_64_mib_download_bounds_worker_and_snapshot_reads
tests/test_remote_performance.py::test_remote_changed_version_refreshes_once_for_concurrent_checks
tests/test_remote_performance.py::test_remote_unreadable_version_refuses_next_socket_io[read]
tests/test_remote_performance.py::test_remote_unreadable_version_refuses_next_socket_io[write]
tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload[http]
tests/test_c2d2_scenarios.py::test_s6_cli_opt_in_refuses_next_legacy_send_without_reload[websocket]
tests/test_c2d2_scenarios.py::test_s6_external_process_revoke_stops_next_socket_io_without_watcher[read]
tests/test_c2d2_scenarios.py::test_s6_external_process_revoke_stops_next_socket_io_without_watcher[write]
tests/test_remote_security_review.py::test_remote_service_shares_one_watcher_and_caps_active_operations
```

Eight new cases bring the branch's proposed case-count increase to 108 over
train 66. Proposed owner edit at tests/budgets.toml:678: pytest = 29830 to
pytest = 29938. That file is unchanged; the release owner runs full collection.

Expect it to work because the tests use the real writer, separate-process
commits, the real connector and socket guards. They measure the worker cost
and test immediate refusal on changed or unreadable authority. Confidence: 0.9
pending the owner's release gate; no live gateway or sustained load test was run.

## Final targeted gate

The combined run covers S6, cross-process revocation, legacy callers, startup
migrations, byte parity, the frozen route graph and both boundary guards. The
closing amendment adds this receipt only; production and test code are unchanged.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-perf-release.xml -n 4 tests/test_remote_performance.py tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py tests/test_engine_egress_choke_point.py tests/test_engine_control_address.py tests/test_lifecycle_drivers.py tests/test_lifecycle_behavior_gaps.py tests/test_node_graph_adapter.py tests/test_inline_tts_stream_cleanup.py tests/test_engine_credential_exposure.py tests/test_projection_registry.py tests/test_service_registry.py tests/test_media_jobs_contract.py tests/test_config_c10_sections.py tests/test_operation_runner.py tests/test_route_graph_freeze.py tests/test_realtime_proxy.py tests/test_speech_lifecycle.py tests/test_workflow_registry_store.py tests/test_workflow_schema_migration.py tests/test_registry_migration.py tests/test_registry_layers.py
```

Observed output: `1223 passed, 1 xfailed in 99.01s (0:01:39)`.
