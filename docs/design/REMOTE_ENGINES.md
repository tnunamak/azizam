# Remote protocol engines

Status: proposed design, not a shipped capability.
Review pin: `44659c2fcd74a70d34f7c3a9974f7d1d4f9906f6`.
This change is documentation only; remote dispatch remains refused.
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
Normalize IPv4-mapped IPv6 before classification; explicitly reject metadata
destinations even where a platform uses an address outside link-local space.
The owner may list exact private/ULA CIDRs per service for a LAN engine.
Do not allow a broad exception to authorize loopback, link-local or metadata:
those remain forbidden on the remote path. Existing local behavior is separate.

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
Serialize send authorization with revocation so no send begins after revocation
commits; bytes already sent cannot be recalled. No cleanup/cancel request to the
remote service is exempt. A policy change requires fresh admission, not silent
rebinding of an old job. Persist the binding with queued work; reload must recheck.

Evidence at 44659c2: dispatch.py creates the main adapter with transport (96-108),
but its fresh observation adapter (206) omits it and invokes at 218 without
require_egress; that callback is optional (154-157). engine_egress.py (296-330)
exempts probes/unmapped hosts and attaches lease hooks, not socket policy.

## Four gated stages and concrete test bindings

These are specified NEW node IDs, not existing or passing tests. Prefix R means
tests/test_engine_protocol_round3.py:: and S means tests/test_c2d2_scenarios.py::.
Implement these top-level functions; run `python -m pytest <full node IDs>`.

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

Until all four stage results pass, retain production local-only refusal. Exercise
candidate remote admission only in an isolated test harness, not an owner-visible
bypass. The final separate release change permits remote admission LAST and cites
each stage's exact commit, full node IDs, output and limitations. A missing test,
xfail, skipped security path or mocked socket assertion does not satisfy a gate.

## Verification and readiness

Expect it to work because one service-bound transport carries the same immutable
authority across every main and auxiliary request; socket checks cannot inherit
the lease/probe exemptions. Generation revocation closes pools and queued work.
Confidence: **0.87 in design; implementation and runtime security unverified**.
No tests run. Only visible document regions changed; redacted text was preserved.
