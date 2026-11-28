# Remote engine security review corrections

The owner requested corrections to the rejected e1b49bb73 implementation. Ten
commits address the ten review items. Items 3 and 6 include the final caller
sweep: connections that omit the service marker, the Realtime WebSocket, and the
raw ordinary-setting helper are refused too. The test source and production
source are unchanged by folding those corrections into their item commits.

The smaller change for legacy clients is typed refusal before client creation.
They do not receive remote credentials or acquire an independent remote network
capability. Only the generic HTTP protocol executor can use remote authority.
The expanded path inventory is in REMOTE_ENGINES.md.

Network I/O holds no SQLite writer lock. Checks run in worker threads, and
policies/hashes are cached per registry generation. A per-service authority digest
excludes unrelated edits. Revocation cancels active tasks and stops the next
read/write; bytes already handed to an in-progress operation cannot be recalled.

Chunked transfer remains refused because trailer ceilings are unproved; the
design lists the excluded endpoint configurations. Valid private ranges can
contain a gateway-host address, but dialing that host is refused on every port.

## Required regression node IDs

The 42 cases below all passed in the completeness gate. Parameter suffixes are
copied from its JUnit report. Existing S6, parity, boundary and frozen-graph
regressions are also included in final validation.

### Item 1: Legacy switch and residency control

```text
tests/test_remote_security_review.py::test_remote_legacy_switch_refuses_before_client_or_credential
```

### Item 2: Health, readiness and observations

```text
tests/test_remote_security_review.py::test_remote_legacy_health_refuses_before_client_or_credential[False]
tests/test_remote_security_review.py::test_remote_legacy_health_refuses_before_client_or_credential[True]
```

### Item 3: Independent clients and WebSocket paths

```text
tests/test_realtime_proxy.py::GateRefusalTests::test_remote_enabled_service_refuses_before_readiness_or_websocket
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[chunked_stt-False]
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[chunked_stt-True]
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[inline_tts-False]
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[inline_tts-True]
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[node_graph-False]
tests/test_remote_security_review.py::test_remote_unbound_adapters_refused[node_graph-True]
tests/test_remote_security_review.py::test_remote_unbound_egress_hook_refuses_probe_and_inference
```

### Item 4: Admin writes during network I/O

```text
tests/test_remote_security_review.py::test_remote_blackholed_connect_does_not_block_admin_write
tests/test_remote_security_review.py::test_remote_generation_change_stops_next_socket_io
```

### Item 5: Authority cache and unrelated edits

```text
tests/test_c2d2_scenarios.py::test_s6_unrelated_registry_generation_leaves_running_remote_job
tests/test_remote_security_review.py::test_remote_authority_cache_runs_off_loop_and_ignores_unrelated_revision
```

### Item 6: Every registry writer requires owner proof

```text
tests/test_c2d2_scenarios.py::test_s6_non_owner_cannot_write_remote_policy_through_any_api[config_import]
tests/test_c2d2_scenarios.py::test_s6_non_owner_cannot_write_remote_policy_through_any_api[config_patch]
tests/test_c2d2_scenarios.py::test_s6_non_owner_cannot_write_remote_policy_through_any_api[registry_put]
tests/test_c2d2_scenarios.py::test_s6_non_owner_cannot_write_remote_policy_through_any_api[service_add]
tests/test_c2d2_scenarios.py::test_s6_non_owner_cannot_write_remote_policy_through_any_api[service_patch]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[None-audit]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[None-ordinary_in]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[None-plain]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[None-transaction]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[None-with_setting]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[roles1-audit]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[roles1-ordinary_in]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[roles1-plain]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[roles1-transaction]
tests/test_remote_security_review.py::test_remote_registry_writers_require_owner_actor[roles1-with_setting]
```

### Item 7: Public job metadata

```text
tests/test_c2d2_scenarios.py::test_s6_public_job_get_strips_remote_binding_for_non_internal_audiences
```

### Item 8: Embedded IPv4 addresses

```text
tests/test_remote_security_review.py::test_remote_embedded_ipv4_addresses_checked_before_dial[64:ff9b::7f00:1]
tests/test_remote_security_review.py::test_remote_embedded_ipv4_addresses_checked_before_dial[64:ff9b::a00:1]
tests/test_remote_security_review.py::test_remote_embedded_ipv4_addresses_checked_before_dial[64:ff9b::a9fe:a9fe]
tests/test_remote_security_review.py::test_remote_embedded_ipv4_addresses_checked_before_dial[::127.0.0.1]
tests/test_remote_security_review.py::test_remote_embedded_ipv4_addresses_checked_before_dial[::ffff:0:7f00:1]
```

### Item 9: Chunked response refusal

```text
tests/test_remote_security_review.py::test_remote_chunked_responses_are_explicitly_unsupported[False]
tests/test_remote_security_review.py::test_remote_chunked_responses_are_explicitly_unsupported[True]
```

### Item 10: Host-local addresses and ports

```text
tests/test_remote_security_review.py::test_remote_private_cidr_never_authorizes_gateway_host_ports[15000]
tests/test_remote_security_review.py::test_remote_private_cidr_never_authorizes_gateway_host_ports[17777]
tests/test_remote_security_review.py::test_remote_private_cidr_never_authorizes_gateway_host_ports[443]
tests/test_remote_security_review.py::test_remote_private_cidr_never_authorizes_gateway_host_ports[5000]
```

## Committed-tree hermetic commands

The remaining xfail in these review gates is S1. S6 passes without an override.
Each quoted output below came from the hermetic runner. Failed diagnostic runs
were corrected before push and are not counted as passing evidence.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-1-fixed.xml -n 4 tests/test_remote_security_review.py
```

Output: `1 passed in 7.77s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-2.xml -n 4 tests/test_remote_security_review.py
```

Output: `3 passed in 7.16s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-3-fixed.xml -n 4 tests/test_remote_security_review.py tests/test_engine_egress_choke_point.py tests/test_engine_protocol_round3.py
```

Output: `121 passed in 51.20s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-4.xml -n 4 tests/test_remote_security_review.py tests/test_engine_protocol_round3.py::test_remote_opt_out_closes_pool_and_cancels_queued_poll
```

Output: `10 passed in 7.22s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-5.xml -n 4 tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py
```

Output: `107 passed, 1 xfailed in 15.09s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-6-valid.xml -n 4 tests/test_remote_security_review.py tests/test_c2d2_scenarios.py tests/test_config_c10_sections.py
```

Output: `50 passed, 1 xfailed in 15.12s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-7.xml -n 4 tests/test_c2d2_scenarios.py::test_s6_public_job_get_strips_remote_binding_for_non_internal_audiences tests/test_media_jobs_contract.py
```

Output: `48 passed in 11.41s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-8.xml -n 4 tests/test_remote_security_review.py
```

Output: `23 passed in 8.50s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-9.xml -n 4 tests/test_remote_security_review.py::test_remote_chunked_responses_are_explicitly_unsupported
```

Output: `2 passed in 7.22s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-10.xml -n 4 tests/test_remote_security_review.py
```

Output: `29 passed in 8.27s`.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-completeness.xml -n 4 tests/test_remote_security_review.py tests/test_realtime_proxy.py tests/test_engine_egress_choke_point.py tests/test_c2d2_scenarios.py
```

Output: `119 passed, 1 xfailed in 55.36s`.

## Evidence limits and owner oracle proposal

No full suite, deployment, restart, live checkout, real service credential or
external engine was used. The tests use synthetic records, credentials and peer
addresses. The connection ceiling remains per serving event loop.

The owner oracles and parity fixtures are unchanged. The 42 additional cases in
this correction bring the branch increase relative to train 66 to 71 cases.
Proposed owner change at tests/budgets.toml:678: `pytest = 29830` to
`pytest = 29901`, subject to the release collection count. No reviewed list or
frozen route graph change is proposed.

Expect it to work because both declared and legacy callers now reach an explicit
authority check or refusal, and the real connector tests cover the dial boundary,
resource ceilings and revocation. Confidence: 0.88 pending the owner's next
independent review and release gate.

## Final combined validation

The final code/test tree is 7a2c8ccff2ca8f1866d2a60700107a23cadd63c9; the
closing amendment adds receipts and derives the synthetic host-local peer from
its declared fixture CIDR, to satisfy the public leak scan. All 42 new cases pass in this
run, alongside S6, legacy callers, authority, credential, parity and route-graph
regressions. The full suite remains for the owner release gate.

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-validated.xml -n 4 tests/test_remote_security_review.py tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py tests/test_engine_egress_choke_point.py tests/test_engine_control_address.py tests/test_lifecycle_drivers.py tests/test_lifecycle_behavior_gaps.py tests/test_node_graph_adapter.py tests/test_inline_tts_stream_cleanup.py tests/test_engine_credential_exposure.py tests/test_projection_registry.py tests/test_service_registry.py tests/test_media_jobs_contract.py tests/test_config_c10_sections.py tests/test_operation_runner.py tests/test_route_graph_freeze.py tests/test_realtime_proxy.py
```

Output: `974 passed, 1 xfailed in 84.82s (0:01:24)`.

The synthetic-peer fixture amendment was then checked on its committed tree:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-review-10-public-fixture.xml -n 4 tests/test_remote_security_review.py::test_remote_private_cidr_never_authorizes_gateway_host_ports
```

Output: `4 passed in 9.98s`.
