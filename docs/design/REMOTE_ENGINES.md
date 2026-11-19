# Remote protocol engines

Status: implemented for explicitly opted-in, non-billed services; see implementation receipts below.
Review pin: `44659c2fcd74a70d34f7c3a9974f7d1d4f9906f6`.
The design-review material through Revisions is historical. The separate release
change enables admission only after the implementation gates below pass.
This revision inspected dispatch, egress and the two named test modules.
Earlier registry observations below are retained historical evidence; no tests ran.
Prior-art claims supplied with the assignment are background, not independently
verified evidence that this gateway already enforces any control.

## Boundary and owner journey

1. An authenticated owner edits one service record, not a request-body URL.
   Proposed `connection.remote_dispatch.enabled` defaults to false.
   It is separate from existing `connection.dispatch = protocol`.
   The record binds exact origin, approved network exceptions, service-specific
   credential reference and finite budgets. These are generic DATA, not engine names.
2. Registration validates the complete effective record; dispatch validates again.
   A regular API caller cannot set or override the policy, endpoint or credentials.
   Unknown policy fields/versions fail closed rather than silently enable remote use.
3. The owner selects a self-hosted, non-billed service and explicitly allows any
   necessary private CIDR. The first job goes through the same generic job lifecycle
   and lease controls, plus the new remote transport controls.
4. Removing opt-in disables new remote dispatch and pooled connection reuse.
   Policy generation changes invalidate connections bound to the previous policy.

Owner authentication and generic job lifecycle enforcement are **not established
by the scoped files**. They must be inspected and tested before enabling this feature.
Stage 0 below owns this verification and blocks stage 1 until it passes.
Do not label a paid provider “self-hosted” to bypass its billing/access controls.

## Existing enforcement, and what it does not prove

| Input | Observed function and boundary |
| --- | --- |
| `gateway/service_registry.py` | `validate_protocol_dispatch_registry` checks new/changed/re-enabled records; unchanged legacy rows may remain or be disabled. `validate_protocol_dispatch_local_only` refuses paid/remote classifications across record, config, connections and offerings, and checks inference/control addresses as local. `_unsafe_address` rejects userinfo, backslashes, whitespace and control characters. |
| `gateway/engine_protocol/dispatch.py` | `opted_in` requires protocol dispatch. `resolve` repeats local-only validation, rejects disabled/ambiguous targets and verifies declared generation tasks. `Runtime.connection_for` checks credential origin, fails mismatched operations, and omits mismatched probe credentials. Some credential-construction text was redacted; it was not reconstructed. |
| `gateway/engine_egress.py` | `check_request` checks host:port serving leases; it returns for unmapped destinations and declared GET probes. `async_client`/`client` attach request hooks. This is not resolved-address validation or socket pinning. |
| `gateway/engine_protocol/dispatch.py` | `Runtime.invoke` and control invocation in `Runtime.lease` call `require_egress`. The callback is optional and is not proof that every network operation has a remote policy. `poll_timing` exposes configured timing, not an enforced end-to-end deadline. |

The existing local-only refusal stays until the complete remote path is proven.
A serving lease and a probe exemption never waive remote network policy.

## Decisions and threat-to-test contract

The “red test” names below are proposed acceptance cases, **not existing pytest
node IDs or passing evidence**. No scoped test file exists from which to verify IDs.

### P0: SSRF through resolution, redirects or derived URLs

Canonicalize and validate scheme, host, port and path before use; reject ambiguous
URLs, embedded credentials and fragments. Require verified HTTPS for remote use.
Validate every returned address, not merely a hostname string or its first answer.

Default-deny all non-global destinations: loopback, unspecified, link-local,
private/ULA, shared-address space, multicast and reserved/special-use ranges.
Normalize embedded IPv4 (mapped, translated, compatible and NAT64) before
classification; explicitly reject metadata
destinations even where a platform uses an address outside link-local space.
The owner may list exact private/ULA CIDRs per service for a LAN engine.
IPv4 exceptions must have prefix length >=24 (/24 or narrower); IPv6 exceptions
must have prefix length >=64 (/64 or narrower). Registration rejects broader
CIDRs, including /0; dispatch repeats this validation on the effective policy.
Do not allow a broad exception to authorize loopback, link-local or metadata:
those remain forbidden on the remote path. Existing local behavior is separate.

A permitted private CIDR can contain the gateway host's own LAN, VPN or bridge
address; policy validation cannot infer which machine owns every address in it.
At connection time, the remote connector refuses every address identified as
this computer by `engine_check.is_this_computer`, regardless of port or private
CIDR consent. This includes the gateway's own ports and any other listener on
that host, including custom ports. Same-host engines must use the local path.
A CIDR exception authorizes other hosts in that range, not host-local services.
The check uses the normalized resolved address and precedes the socket dial.

**Redirect decision: never follow.** A 3xx ends the operation with a bounded error.
This is easier to prove than per-hop validation and prevents credential forwarding.
Poll/asset URLs are new requests, not an exception: require the same approved
origin and network policy. An unapproved absolute URL is refused.

Enforcement target: retain registry URL validation and runtime revalidation;
extend the transport owned through `engine_egress.async_client/client`.
The inspected request hook alone cannot enforce the actual peer address.
Red tests: denied IPv4/IPv6 and mapped forms; mixed good/bad DNS answers;
private-CIDR exception versus metadata exclusion; redirect target receives zero
connections; hostile poll/asset URL receives zero connections; probes obey policy.

### P1: DNS rebinding and connection reuse

**Resolve once and pin the socket for each new connection attempt.**
Validate the complete answer set, select an approved address, and connect directly
to that address without a second implicit resolution. Preserve original hostname
for TLS SNI/certificate verification and HTTP Host; never disable TLS verification.
Every fresh connection/retry resolves and validates anew; a reused connection must
retain approved origin, peer and current policy generation.

A separate DNS “recheck” followed by a normal hostname connect leaves a race.
Disable environment-proxy inheritance for this path; an approved proxy would need
equivalent peer enforcement and is outside the first implementation.
Enforcement target: transport construction through `engine_egress.async_client/client`,
not just `check_request`; implementation location must be verified before coding.
Red tests: resolver changes after validation but socket stays pinned; reconnect
to a now-forbidden address fails; proxy environment cannot bypass validation;
policy revocation prevents reuse; TLS mismatch fails.

### P0: gateway credentials or owner tokens leak

Construct outbound headers from a minimal allowlist; never copy inbound headers.
Inbound Authorization, Proxy-Authorization, Cookie, X-API-Key, owner/session tokens
and forwarding headers never leave. Do not reflect them in errors or logs.
A credential deliberately bound to this service and origin may be injected as its
own outbound authentication header; this is not forwarding the caller's credential.
Reject conflicting caller-supplied authentication fields.

Enforcement starting point: `dispatch.Runtime.connection_for` provides origin checks;
the actual outbound construction and all auxiliary requests still need verification.
Red tests: unique inbound secret canaries absent from captured requests/logs;
only the configured service credential appears; mismatched control origin fails;
probe omission is preserved; redirects never transmit a second request.
Stage 2 cannot pass until separate poll, asset, control and probe production-path
tests assert unique inbound secret canaries absent from captured outbound requests,
errors and logs. Also assert that only the credential bound to the approved service
and origin can appear. Unavailable credential-construction evidence is a failed
gate, not permission to assume these auxiliary paths are safe.

### P1: paid-provider bypass

Keep paid-provider access/spend routing out of scope. Remote network location and
billing class are separate concepts. Replace only the locality restriction for
explicitly opted-in non-billed self-hosted services; do not delete the nested paid
checks in `service_registry.validate_protocol_dispatch_local_only`.
Keep equivalent enforcement in registry mutation and `dispatch.resolve`.
Unknown billing classifications remain refused.
Red tests: paid/cost markers at every nesting level fail despite remote opt-in;
unopted legacy rows remain refused; permitted non-billed remote record succeeds.

### P2: unbounded response bytes and elapsed time

Require finite per-service ceilings for DNS/connect/TLS, first byte, idle read,
total operation deadline, wire bytes, decompressed bytes, headers and poll count.
The following are proposed policy defaults and non-overridable hard maxima, not
measured performance claims. Omitted values take the default; registration rejects
non-finite, non-positive, wrong-unit or above-maximum values. Byte/count values must
be integers. Runtime validates the effective limits again; owners may only lower
the maxima, never enlarge them.

| Ceiling | Default | Hard maximum |
| --- | ---: | ---: |
| DNS resolution per attempt | 5 s | 15 s |
| TCP connect per attempt | 5 s | 15 s |
| TLS handshake per attempt | 5 s | 15 s |
| First response byte per request | 30 s | 60 s |
| Idle read between received chunks | 15 s | 30 s |
| Total operation, including queue time | 120 s | 600 s |
| Response wire bytes, aggregate operation | 64 MiB | 256 MiB |
| Decompressed response bytes, aggregate operation | 128 MiB | 512 MiB |
| Response headers per response, encoded bytes | 32 KiB | 64 KiB |
| Response header fields per response | 100 | 256 |
| Poll requests per operation | 60 | 300 |

KiB = 1024 bytes; MiB = 1024 KiB. Header bytes include names, values and framing.
Counters include retries, polls, observations, control and asset requests belonging
to the operation; standalone auxiliary operations get the same policy. Enforce the
smaller of each phase budget and the remaining total deadline; an exhausted budget
refuses the next action before DNS or send. Streaming cannot reset byte counters.
Apply the total deadline across retries and polling, not separately to each hop.
Close streaming bodies/sockets on overflow, cancellation and deadline; bound
concurrency and avoid buffering an entire body before checking its size.
Enforcement targets: remote transport via `engine_egress.async_client/client`,
and deadline propagation through `dispatch.Runtime.invoke/poll_timing`.
Those controls are proposed, not proven by the inspected code.
Red tests: oversized compressed/uncompressed body; slow trickle; endless polling;
cancelled stream; limits apply to probes/control as well as generation.

## Mandatory remote capability and revocation contract

Proposed capability: immutable (service_id, exact_origin, policy_generation,
address_exceptions, credential_ref, finite_budgets), minted only from the current
owner-controlled registry. Never build it from a request URL or caller headers.
One mandatory client factory owns this capability and the enforcing transport.
Main dispatch, fresh HTTP observations, inventory, control, polling and asset
fetches must all obtain their clients from it; absence or mismatch fails closed
before DNS or socket creation. AdapterRegistry must receive it on every path.
No adapter may fall back to a default client. Keep existing local-only behavior.
Remote security validation runs independently of lease authorization: probes,
unmapped destinations and lease exemptions cannot skip it. The optional
Runtime.require_egress callback is not the remote security boundary.

Check service id, exact origin and current generation at dispatch, dequeue,
every poll/observation and immediately before each socket write or pooled reuse.
Pool keys include the complete binding, never origin alone; validate the retained
peer against that binding. Services sharing an origin never union exceptions.
Registry updates publish a new generation atomically with admission revocation.
Opt-out refuses new work immediately, invalidates idle pools and cancels admitted
running work, queued polls and observations; close active sockets and mark the job
cancelled with a bounded reason. Do not finish remote work under revoked authority.
Check generation before and after each I/O and cancel active work on revocation.
A generation change stops the next read or write; bytes already handed to the
network cannot be recalled. No database writer lock is held across network I/O.
No cleanup/cancel request to the
remote service is exempt. A policy change requires fresh admission, not silent
rebinding of an old job. Persist the binding with queued work; reload must recheck.

Evidence at 44659c2: dispatch.py creates the main adapter with transport (96-108),
but its fresh observation adapter (206) omits it and invokes at 218 without
require_egress; that callback is optional (154-157). engine_egress.py (296-330)
exempts probes/unmapped hosts and attaches lease hooks, not socket policy.

## Stage 0 prerequisites and four gated build stages

These are specified NEW node IDs, not existing or passing tests. Prefix R means
tests/test_engine_protocol_round3.py:: and S means tests/test_c2d2_scenarios.py::.
Implement these top-level functions; run `python -m pytest <full node IDs>`.

0. Owner prerequisites (owner accountable; no stage 1 work before this gate passes):
   - Stest_s6_remote_policy_owner_authentication_production_path
   - Stest_s6_remote_generic_job_lifecycle_and_leases_production_path
   Verify actual policy mutation rejects unauthenticated and ordinary API callers
   while permitting the authenticated owner. Verify admission, execution, terminal
   receipts and serving leases use the generic production job path, not a bypass.
   The owner must bind each path below to its exact production file and function
   at a pinned commit and assign one production-path test per row for stage 1.
   Stage 1 cannot pass until all six tests pass against its implementation. These
   are required owner tasks, not claims that an existing factory already covers them.

   | Factory path | Stage 0 owner binding task | Required test |
   | --- | --- | --- |
   | Main dispatch | Record exact file + function | Rtest_remote_factory_main_dispatch |
   | Fresh observation | Record exact file + function | Rtest_remote_factory_fresh_observation |
   | Inventory | Record exact file + function | Rtest_remote_factory_inventory |
   | Control | Record exact file + function | Rtest_remote_factory_control |
   | Polling | Record exact file + function | Rtest_remote_factory_polling |
   | Assets | Record exact file + function | Rtest_remote_factory_assets |

   These six rows cover the generic protocol path only. Every other engine
   client must refuse remote-enabled records before sending. The control, probe
   and marked-adapter entry points refuse before credential lookup or clients:

   | Additional engine path | Enforcement | Regression |
   | --- | --- | --- |
   | Legacy switch/residency control | engine_control.EngineControlRuntime observe/invoke/select/load/unload/sleep/wake refuse | test_remote_legacy_switch_refuses_before_client_or_credential |
   | Lifecycle readiness/health and standalone observation | lifecycle_drivers driver selection, ensure_ready and _poll_health; engine_observations.ObservationReader.read refuse | test_remote_legacy_health_refuses_before_client_or_credential |
   | Buffered/chunked transcription and speech | chunked_stt/inline_tts invoke refuse a marked Connection that holds no authority; with authority (slice S7) every request is one BoundTransport.send through bound_wire | test_remote_unbound_adapters_refused, tests/test_remote_adapter_binding.py |
   | Node graph HTTP sessions, attached sessions and WebSocket events | node_graph open/_use/GraphSession/watch refuse marked Connection | test_remote_unbound_adapters_refused |
   | Realtime WebSocket | proxy._handle_realtime refuses the record before readiness or upgrade | GateRefusalTests.test_remote_enabled_service_refuses_before_readiness_or_websocket |
   | Other legacy proxy inference, metadata, health, capacity and transition HTTP reads/writes | engine_egress project records remote destinations even without transition policy; sync/async send hooks require bound authority, including probes | test_remote_unbound_egress_hook_refuses_probe_and_inference |

   AdapterRegistry also refuses every reviewed custom adapter for remote
   capabilities: only the generic HTTP executor accepts the bound transport.
   Slice S7 adds one reviewed path beside it: the speech and transcription
   adapters (``reviewed.sends_through_remote_guard``) take the bound transport
   from ``Connection.remote_transport`` and build every client through
   ``bound_wire.client``. The proxy binds it per call (``_remote_binding``), so
   an owner change or a revoke applies to the next call and cancels one in flight.
   Connection conversion retains the remote-enabled marker for direct adapter
   callers. Destination checks also cover connections constructed without the
   service marker, including graph event subscriptions. A default HTTP client
   has no remote authority. A destination shared
   with a remote record is conservatively denied to an unbound caller as well.
   This inventory supersedes any claim that the six generic factory tests alone
   cover every engine egress path.

   Each test traces the real caller into the mandatory factory, verifies service/
   origin/generation binding and refusal without the capability before any socket.
   Missing bindings or missing owner/lifecycle/lease evidence keep stage 1 blocked.
1. Registry/admission binding (registry + dispatch + the two named test modules):
   - Rtest_remote_binding_required_on_every_adapter_path
   - Rtest_remote_same_origin_services_do_not_union_exceptions
   - Rtest_remote_policy_generation_rechecked_on_dispatch_and_reuse
   - Stest_s6_only_owner_can_enable_remote_dispatch
   Cover default-off, unknown policy/billing refusal, paid flags at every nesting
   level, request-body overrides, origin changes and unchanged local behavior.
   Exercise all six factory paths, including the freshly compiled observation.
2. Socket boundary (egress + scoped transport implementation + R module):
   - Rtest_remote_observation_denied_dns_zero_connections
   - Rtest_remote_observation_redirect_zero_forbidden_connections
   - Rtest_remote_observation_proxy_env_zero_forbidden_connections
   - Rtest_remote_observation_credential_canary_zero_forbidden_connections
   - Rtest_remote_dns_pinning_reconnect_and_tls_identity
   - Rtest_remote_poll_secret_canary_absent
   - Rtest_remote_asset_secret_canary_absent
   - Rtest_remote_control_secret_canary_absent
   - Rtest_remote_probe_secret_canary_absent
   Use the real transport connector with a controlled resolver/socket recorder,
   not a mocked policy predicate. Count zero connections to each forbidden peer,
   including redirect/proxy targets; assert canaries absent from all wire/logs.
   Exercise mixed DNS answers, mapped IPv6, metadata, CIDR limits and TLS mismatch.
3. Lifecycle/resource bounds (dispatch + egress + scoped transport + R module):
   - Rtest_remote_opt_out_closes_pool_and_cancels_queued_poll
   - Rtest_remote_aggregate_deadline_spans_retries_and_polling
   - Rtest_remote_wire_and_decompressed_byte_caps
   - Rtest_remote_header_limits_and_cancellation_close_socket
   Use a single monotonic deadline from admission (queue time included); retries,
   observations and polls consume remaining budget, never restart it. Limit both
   wire and decoded bytes while streaming, header count/bytes and poll count.
   Assert deadline/overflow/cancellation closes sockets, returns bounded errors,
   performs no extra request and cannot reuse a revoked pooled connection.
4. Real generic-job journey (S module + this receipt document):
   - Stest_s6_remote_job_observation_poll_asset_security
   - Stest_s6_remote_opt_out_cancels_running_job
   - Existing: Stest_s6_owner_opted_in_remote_engine_serves_a_job
   Align its provisional allow_remote input with the canonical owner policy field;
   the current in-process fixture/strict capability-gap xfail is not socket proof.
   Add controlled connection-level positive and denial evidence through the real
   job path; retain local and paid regressions, asset bytes and one terminal receipt.

Until stage 0 and all four build stage results pass, retain production local-only refusal. Exercise
candidate remote admission only in an isolated test harness, not an owner-visible
bypass. The final separate release change permits remote admission LAST and cites
each stage's exact commit, full node IDs, output and limitations. A missing test,
xfail, skipped security path or mocked socket assertion does not satisfy a gate.

## Design-review readiness (historical)

Expect it to work because one service-bound transport carries the same immutable
authority across every main and auxiliary request; socket checks cannot inherit
the lease/probe exemptions. Generation revocation closes pools and queued work.
Confidence: **0.88 in design; implementation and runtime security unverified**.
No tests run. Only visible document regions changed; redacted text was preserved.

## Revisions

- Added explicit default and hard-maximum transport/resource budgets with registration refusal.
- Added blocking stage 0 owner-authentication, generic lifecycle and lease verification.
- Named six factory paths with owner file/function bindings and one test per path.
- Required separate poll, asset, control and probe secret-canary evidence before stage 2 passes.
- Bounded private CIDR exceptions to IPv4 /24 and IPv6 /64 or narrower.
- Most uncertain assumption remains production-path completeness: prior scoped evidence
  did not establish owner authentication, lifecycle enforcement or all credential
  construction. Stage 0 and auxiliary canary gates require evidence, not inference.

## Implementation receipts (W3b / S6)

The design above is retained as its reviewed contract. Stage 0 was accepted by
the owner at `2107d91098a619951fd3bcd71ad2ccf6620e2d97`. Production admission
remains refused in the stage 1–4 commits. Only the separate release commit may
change `gateway.remote_engine.PRODUCTION_READY` from false to true.

### Owner configuration and conservative refusals

An owner writes `connection.remote_dispatch` through the existing service or
registry API. Its fields are `enabled` (boolean), `version` (1, default 1),
`private_cidrs` (default empty) and `limits` (default empty). No request-body
policy, endpoint, authentication header or credential override grants authority.
The service must explicitly declare `cost_class: free-local`; opt-in alone does
not establish non-billed use. Existing paid/unknown classifications at every
nesting level still refuse admission. Native pass-through is refused on these
records because it does not carry the declared-operation capability.

Defaults and hard maxima live in `registry/remote_egress.yml`. In addition to
the reviewed table, `connections` defaults to 1 and has maximum 4, shared across
operations for a service/database on each serving event loop;
`authority_check_s` defaults to 0.05 s and has maximum 1 s. This watcher observes
cross-process revocation while a job sleeps. Generation checks independently
refuse the next I/O and success publication under revoked authority.

The first implementation refuses encoded/compressed responses before decoding,
transfer encodings (including chunked bodies and trailers),
all redirects, informational responses, query-bearing URLs, non-HTTPS origins,
and unlisted outbound headers. Control addresses must share the approved exact
origin. An unavailable probe credential may still be omitted; a non-probe
operation requiring it fails before send. Credentials use only the existing
service-reference resolver. None are persisted in capability/job metadata.

Chunked transfer remains unsupported in this release. The socket guard caps
all wire bytes and the first header block, but does not parse chunk extensions
and trailers to apply the separate header byte/field ceilings. Accepting chunked
bodies without that parser would leave trailer limits unproved. Refusal is
intentional until those limits have socket-boundary tests; both a valid chunked
JSON response and a response with trailers are regression-tested as refusals.

This excludes every engine endpoint that requires HTTP/1.1 chunked transfer:
streaming SSE/NDJSON endpoints, unknown-length generated audio/video or file
responses, and even buffered JSON health, inventory, submit or poll responses
when their server chooses chunking. Such an engine is eligible only when every
required endpoint supplies identity content with Content-Length (or bounded
connection-close framing). Engine brands alone cannot determine eligibility:
the operator's server and proxy configuration determine framing. A compatible
buffered submit/poll API can qualify while that same engine's streaming endpoint
cannot. No claim of general remote streaming support is made.

Each connection is freshly resolved and pinned; no idle pool is reused between
requests. The complete answer set must be permitted, and only the selected first
address is dialed. The original hostname supplies TLS SNI, certificate checking
and Host. A new request/retry resolves again. Raw TCP is closed on revocation;
revocation does not authorize a TLS shutdown or backend cleanup request.

Authority-changing registry mutations revoke existing in-process authority before commit, through
`admin_store.write_service_registry_in_transaction`, including caller-owned
transactions. A rollback conservatively requires fresh admission too. Network
fences check authority without holding SQLite transactions during connect, TLS
or writes; an in-progress I/O is cancelled and the next I/O must recheck. A
service-specific digest of connection, billing, enabled state and declared
operations/selection pins queued work. Display metadata and unrelated registry
revisions do not revoke it. Policies and hashes are cached per registry generation;
snapshot reloads and validation run in workers; unchanged authority uses an
inline SQLite data_version comparison on a persistent read-only connection.
The normal
restart behavior fails interrupted declared jobs rather than rebinding them.

### Stage 1 — capability binding

Commit: `2243e02f1a87fab268f4a9dd090bde74c18e87b8`.
Gate output: `124 passed, 2 xfailed in 17.55s` (the xfails were other scenarios).
Required full node IDs:

```text
tests/test_engine_protocol_round3.py::test_remote_factory_main_dispatch
tests/test_engine_protocol_round3.py::test_remote_factory_fresh_observation
tests/test_engine_protocol_round3.py::test_remote_factory_inventory
tests/test_engine_protocol_round3.py::test_remote_factory_control
tests/test_engine_protocol_round3.py::test_remote_factory_polling
tests/test_engine_protocol_round3.py::test_remote_factory_assets
tests/test_engine_protocol_round3.py::test_remote_binding_required_on_every_adapter_path
tests/test_engine_protocol_round3.py::test_remote_same_origin_services_do_not_union_exceptions
tests/test_engine_protocol_round3.py::test_remote_policy_generation_rechecked_on_dispatch_and_reuse
tests/test_c2d2_scenarios.py::test_s6_only_owner_can_enable_remote_dispatch
```

The six production bindings remain the ones recorded at stage 0. Runtime passes
one bound transport into both the main and freshly compiled observation adapter.
The HTTP executor requires the matching capability and refuses remote redirects.
Inventory enters through Runtime; control, polls and assets use the same adapter
and connector. Missing or mismatched authority cannot fall back to a default.

### Stage 2 — socket boundary

Commit: `5296b53e248d8aa4ec27092595cafc54aa3cc0f7`.
Gate outputs: `120 passed in 14.29s`, followed by the strengthened fresh-observation
checks, `2 passed in 7.52s`. A fresh observation failure now stops selection; it
cannot become an unknown value that triggers a fallback control request.
Required full node IDs:

```text
tests/test_engine_protocol_round3.py::test_remote_observation_denied_dns_zero_connections
tests/test_engine_protocol_round3.py::test_remote_observation_redirect_zero_forbidden_connections
tests/test_engine_protocol_round3.py::test_remote_observation_proxy_env_zero_forbidden_connections
tests/test_engine_protocol_round3.py::test_remote_observation_credential_canary_zero_forbidden_connections
tests/test_engine_protocol_round3.py::test_remote_dns_pinning_reconnect_and_tls_identity
tests/test_engine_protocol_round3.py::test_remote_poll_secret_canary_absent
tests/test_engine_protocol_round3.py::test_remote_asset_secret_canary_absent
tests/test_engine_protocol_round3.py::test_remote_control_secret_canary_absent
tests/test_engine_protocol_round3.py::test_remote_probe_secret_canary_absent
```

These use the real HTTP transport/connector with a controlled resolver and an OS
socket recorder. Certificate identity also runs against a real local TLS server
with synthetic certificates: the matching identity works and a trusted wrong
hostname receives no HTTP request. Later journey qualification adds private/ULA
positive cases, mapped IPv4, metadata exclusion inside an allowed ULA exception,
rebinding at the dial boundary, and missing-probe-credential omission.

### Stage 3 — resources and revocation

Commit: `a4e29bbd07ef1c1e3775fb979490aea82d034a7a`.
Gate output: `161 passed, 2 xfailed in 21.92s`.
Required full node IDs:

```text
tests/test_engine_protocol_round3.py::test_remote_opt_out_closes_pool_and_cancels_queued_poll
tests/test_engine_protocol_round3.py::test_remote_aggregate_deadline_spans_retries_and_polling
tests/test_engine_protocol_round3.py::test_remote_wire_and_decompressed_byte_caps
tests/test_engine_protocol_round3.py::test_remote_header_limits_and_cancellation_close_socket
```

One admission deadline and counters survive control, observations, retries,
polls and assets. Raw HTTP bytes and headers are limited before parser buffering;
identity body bytes have an independent aggregate cap. The tests include closed
sockets on cancellation/overflow, a slow trickle and admin-write progress while a
socket connect is blocked. Later journey qualification shares connection limits
across independent factories on a serving loop and fences final job publication.

### Stage 4 — public generic jobs

The stage 4 commit is pinned in the release receipt below after its tests pass.
Required full node IDs:

```text
tests/test_c2d2_scenarios.py::test_s6_remote_job_observation_poll_asset_security
tests/test_c2d2_scenarios.py::test_s6_remote_opt_out_cancels_running_job
tests/test_c2d2_scenarios.py::test_s6_owner_opted_in_remote_engine_serves_a_job
```

The owner installs the description and writes canonical record data. The public
route admits and queues the job, holds the serving lease where configured,
observes, submits, polls and stores the fetched bytes, then publishes one terminal
receipt. Opt-out cancels admitted work and refuses subsequent submissions without
another upstream connection. S6's strict xfail is removed from the test and
`tests/suites.toml`; the unrelated S1 xfail remains.

Qualification uses synthetic request/service canaries, never real credentials.
The public models and OpenAPI comparisons hold display time and cached host facts
fixed, then compare response bytes with readiness disabled and enabled. Existing
OpenAPI/workflow and model metadata goldens also pass without fixture changes.

### Commands, limits of evidence and owner oracle proposal

Every pytest run uses a committed tree and the hermetic runner. The stage gates
used the following target lists, always with `nice -n 15`,
`--junit-xml-out ../<unique-name>.xml` and `-n 4`:

- Stage 1: `tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py`.
- Stage 2: `tests/test_engine_protocol_round3.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py`, then the two named fresh-observation cases.
- Stage 3: the stage 1 targets plus `tests/test_operation_runner.py tests/test_smell_infer_offering.py`.
- Stage 4: the stage 3 targets plus `tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py`.

No full suite, live gateway, deployment restart, paid provider, external engine,
or sustained load test was used. The connection ceiling is per serving event
loop; generation checks observe commits across processes. Cross-process active-work
cancellation is observed by the bounded watcher. There is no idle-connection
reuse feature to qualify. Both compressed responses and unsafe ancillary paths
are refused instead of being repaired or followed.

Owner oracles were not edited. `tests/budgets.toml:678` currently has
`pytest = 29830`. The initial stages added 29 cases; the security and performance
corrections bring the branch increase to 108 over train 66. Proposed owner
change, subject to release collection: `pytest = 29938`. No reviewed literal list
or frozen graph change is proposed.

Expect it to work because all six production paths enter the same bound
transport; the real connector records approved peers and TLS identity, and
registry changes are checked around network I/O and completion. Confidence:
0.86 in the tested implementation, pending the separate reviewer and release gate.

### Release admission receipt

Stage 4 commit: `ec56a499e68a896cc02e2f41cc4cc70b7d173909`.
Its final named gates and credential/projection/registry regressions reported
`213 passed in 15.53s`. The final DEBUG-level canaries and all three S6 journeys
reported `8 passed in 11.16s`. The broader targeted parity/runner/security run
reported `471 passed, 1 xfailed in 29.20s`; the remaining xfail is S1.
The public job journey also rejects an asset redirect to metadata, publishes no
asset for that failed job, and opens no connection to the redirect target.

All four stage commits were pushed after their named gates passed. This separate
release commit enables production readiness and preserves the versioned runner's
ownership of already-committed terminal bundles and replays. An opted-in
record must still satisfy owner authorization, non-billed classification, exact
origin, credentials, address policy and all finite ceilings. No service record
is opted in by the release. The final hermetic target command is:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-release-final.xml -n 4 tests/test_engine_protocol_round3.py tests/test_c2d2_scenarios.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py tests/test_c2d_model_metadata_parity.py tests/test_operation_runner.py tests/test_smell_infer_offering.py tests/test_engine_credential_exposure.py tests/test_projection_registry.py tests/test_service_registry.py tests/test_result_bundle.py tests/test_result_bundle_release.py
```

Output: `722 passed, 1 xfailed in 41.60s`. The remaining xfail is S1.
The unchanged frozen route graph also passed with
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w3b-route-graph-final.xml -n 4 tests/test_route_graph_freeze.py`: `5 passed in 8.30s`.

The S6 harness enables candidate admission only when production readiness is
false. With this release's readiness true, the journey runs without a readiness
override. The owner's third security review passed; the release gate remains.

### Independent security review corrections

The owner rejected e1b49bb73 for merge. The ten corrections and their exact
regression node IDs and test commands are recorded in
[REMOTE_ENGINES_REVIEW.md](REMOTE_ENGINES_REVIEW.md). The owner's third security
review passed these corrections. The release gate remains required.

### Re-review: cross-process I/O checks

GuardedStream reads and writes call check_current before touching the socket and
check again after I/O. Each production factory shares one persistent read-only
SQLite connection. It compares PRAGMA data_version inline; an unchanged version
reuses parsed registry data, policy validation and hashes without a worker hop.
After a change, one check per service refreshes the snapshot in a worker while
other checks wait. The snapshot's version is captured before reading, so a
commit during refresh cannot be hidden. I/O is refused if refresh fails or is
overtaken by another commit. The in-process revoked flag still stops I/O
immediately. These checks do not depend on a watcher tick. The external-process test
commits an owner opt-out through the real registry writer with no parent callback
or watcher running, then proves both the next read and the next write refuse.

The watcher interval schedules checks while an operation is sleeping or blocked
inside a previously authorized I/O; actual detection also includes scheduler and
database latency. It is not permission for another I/O under a stale generation. Bytes already handed to the network cannot be recalled.

### Re-review: service concurrency and watcher scope

The `operations` ceiling in registry/remote_egress.yml defaults to 4 with maximum
16. It limits active logical operations per service/database on a serving event
loop, across factories. Excess work is refused without a second wait queue.
Nested main/observation transports for one capability share one slot; an open
response retains its slot until closed, and cancellation releases it.

There is no per-caller quota yet: one caller can occupy every operation slot
for a service and cause other callers' operations to be refused.

One watcher task serves all active transports for that service/database/loop.
Each tick uses the same version cache, at the smallest configured
`authority_check_s` interval among those operations. Unchanged authority submits
no worker. Independent I/O checks compare the committed version before sending
or consuming more bytes. Operation slots and connections retain separate caps.

### Re-review: legacy publication, disable and migration boundaries

The active registry writer publishes conservative remote destination denials
before committing, covering all four registry transaction callers. Boot and hot
reload install the egress projection before publishing their runtime snapshot.
Legacy send hooks compare data_version inline on one persistent read-only
connection for the active registry path. They reload destinations only after a
commit, so a CLI opt-in is refused on the next send without a reload callback.
If a read fails, they retain the last known denial set; local destinations
outside that set keep serving, including gateways with no remote records.
Once marked remote, a destination stays refused for unbound clients in that
process until restart, including after a failed reload or transaction rollback.
This is fail-closed for known remote destinations: a stale concurrent reload,
failed write or later opt-out cannot restore unbound access. Restart rebuilds
the set from the current registry.

An administrator can disable a remote service without changing other authority
inputs. Only an owner can re-enable it or change its connection policy. Schema
loading rejects malformed remote_dispatch shapes before runtime code can treat
them as local records; runtime guards return a typed protocol refusal.

The actorless layering and workflow migrations and workflow registry writes were
tested with an enabled remote record present. They preserve its authority inputs,
so no system actor with extra permissions is needed. Layered and pre-layering
startup paths both succeed; the second boot leaves the registry generation alone.

The two callers of _is_local_swap_backend are _build_role_catalog_entries (which
has no callers) and _first_servable_loaded_llm_target. The latter's only
local_swap_only caller is decide_image_background_removal; current-model paths
also call it without that flag. Classification treats a refused remote driver
as non-local; legacy HTTP boundaries return sanitized typed refusals. The speech
route exercises the latter end to end. Graph adapter/watch, the background
listener and Realtime tests separately prove refusal before a WebSocket dial.

The direct callers of _first_servable_loaded_llm_target are
_prepare_litellm_local_llm_request, _current_loaded_llm_target,
decide_image_background_removal and _model_list_served_declarations. Only the
background-removal caller supplies local_swap_only=True. This caller inventory
was checked against Python AST call sites rather than comments or string matches.
The legacy map also retains each service's former published destinations when it
becomes remote, so stale legacy connections and WebSocket listeners are refused
on their old address as well as on the new remote origin.

The eight re-review corrections, full node IDs, commands and observed pass lines
are recorded in [REMOTE_ENGINES_REREVIEW.md](REMOTE_ENGINES_REREVIEW.md).

Performance test commands and measurements are recorded in
[REMOTE_ENGINES_PERFORMANCE.md](REMOTE_ENGINES_PERFORMANCE.md).
