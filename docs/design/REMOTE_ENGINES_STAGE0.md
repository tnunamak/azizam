# Remote engine prerequisite evidence

Status: stage 0 evidence only. Remote admission remains refused. Build stages
1–4 are unimplemented; this branch does not satisfy S6 or qualify remote security.
The r3 design already exists byte-for-byte in the starting commit fd30cbfca
(SHA-256 4bab80ef421e18c5f9f23a7de8ebcff97253e87a1d7d0237703bbcb7a9caee42).
The attempted cherry-pick of 9691630c was empty and skipped.

## Production path bindings

Bindings inspected at fd30cbfca. The stage 1 tests named below are assignments,
not tests implemented or passed by this change.

| Path | Production callers | Planned stage 1 test in test_engine_protocol_round3.py |
| --- | --- | --- |
| Main dispatch | dispatch.Runtime.__init__/invoke → AdapterRegistry.adapter_for → HttpV1Executor.invoke | test_remote_factory_main_dispatch |
| Fresh observation | dispatch.Runtime.lease's observe closure → new AdapterRegistry → HttpV1Executor.invoke | test_remote_factory_fresh_observation |
| Inventory | inventory.models's one closure → dispatch.Runtime → Runtime.invoke | test_remote_factory_inventory |
| Control | dispatch.Runtime.lease's invoke closure → self.adapter.invoke | test_remote_factory_control |
| Polling | jobs.run → HttpV1Adapter.poll → HttpV1Executor.poll | test_remote_factory_polling |
| Assets | jobs.run → HttpV1Adapter.fetch_assets → HttpV1Executor.fetch_assets | test_remote_factory_assets |

File prefixes above are gateway/engine_protocol/. Each HTTP executor calls
HttpV1Executor._exchange/_send → transport.HttpxTransport.send →
engine_check.check_destination/checked_transport → engine_egress.async_client.
This is an existing connector, not the mandatory remote capability factory.
The observation closure builds a fresh registry without the injected transport.
AdapterRegistry accepts an absent transport and the executor constructs a default.
Neither behavior meets the remote capability contract.

The transport disables automatic redirects and environment proxies. The executor
still follows same-origin redirects. Remote policy must refuse these too.
engine_egress.check_request's serving-lease checks exempt probes and unmapped
hosts; they do not establish remote network authority.

## Stage 0 checks

New production-route tests in tests/test_c2d2_scenarios.py:

- test_s6_remote_policy_owner_authentication_production_path: unauthenticated,
  ordinary API and administrator callers cannot mutate connection policy; the
  owner can persist it on a local record. The assertion reads stored data since
  unknown connection fields are hidden in the owner projection. Persisted policy
  data alone does not grant remote execution or validate the future policy schema.
- test_s6_remote_generic_job_lifecycle_and_leases_production_path: the owner
  declares the custom wire protocol and pass-through residency policy; the public
  job route runs submit, poll and fetch under one serving lease, preserves asset
  bytes, releases the lease and produces one successful receipt. A later job
  admitted with the serving gate closed terminates failed with no upstream send.
- test_s6_remote_without_owner_opt_in_keeps_existing_refusal: registration returns
  the existing HTTP 400 and exact error message and stores no refused record.

The lease scenario must explicitly set config.protocol to its declared custom
protocol: the add-engine definition otherwise also advertises an OpenAI-compatible
model-router path, and registration correctly refuses that unleased path.
A closed gate rejects execution after the public job route has queued the job;
the route returns 202, then the job terminates failed. A forced in-memory gate
hold survives reload; the test runs the success before imposing the hold.

The scenario helper now prints complete terminal error data on failure. The S6
receipt assertion uses the submit response id: terminal public records expose
job_id. Its strict xfail remains in the test and tests/suites.toml.

## Verification

Committed test tree 8b14d94c4, hermetic runner:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-stage0-checked.xml -n 4 tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py
```

Output: `363 passed, 2 xfailed in 22.18s`.

Earlier stage-0 diagnostic runs failed on test assumptions about response
wrappers, projection visibility, duplicate fake endpoints, synchronous admission,
reopening a forced hold, and the terminal job identifier. These were corrected;
none was counted as security evidence or hidden with an xfail.

No production files, credentials, owner oracles, manifests, or parity fixtures
changed. Existing OpenAPI/workflow and model metadata goldens pass. No real
remote socket journey or /v1/models byte comparison was qualified here.

Expect it to work because the new prerequisites exercise the real owner and
public job routes; only the upstream is synthetic. Every observed submit, poll
and asset request asserts the serving lease. Confidence: 0.95 in this bounded
stage-0 evidence; remote implementation and security remain unverified.
