# Remote protocol engines

Status: proposed design, not a shipped capability.
Source pin: `50688daf80c6e264293669720daacaa30cddcd51`.
This change is documentation only; remote dispatch remains refused.
Only the three code inputs named below were inspected. No test source or
transport implementation was in scope; no tests were run.
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

## Verification and readiness

Expect it to work because the owner-selected identity, network and billing policy
is bound to the record and enforced again at the actual connection boundary,
while existing leases and strict paid routing remain independent requirements.
The likeliest-wrong assumption is that today's egress hook pins DNS: its source
shows host:port matching and exemptions, not a resolved peer check.
Confidence: **0.85 in design direction; implementation remains unverified**.
No code, ratchet baseline or test-count budget changes are part of this patch.
Exact pytest node IDs are unavailable, not guessed. Before implementation, the
owner must scope the authentication/transport evidence and one concrete test module
for each row below; bind these acceptance cases to real node IDs and run them.

## Build order (red test first)

Each row budgets at most five files; “test module” is an explicit unbound dependency,
not permission to invent or edit an out-of-scope path. Never enable remotely before
all four rows' negative and positive cases pass through the real connection path.

| Order | Red first | File budget and implementation target |
| --- | --- | --- |
| 1 | Owner-only opt-in, defaults, nested paid flags, unchanged local behavior | 3 files: one owner-scoped test module, `gateway/service_registry.py`, `gateway/engine_protocol/dispatch.py`; authentication evidence must be scoped first. |
| 2 | DNS rebinding, forbidden ranges, zero redirect/proxy escape, TLS mismatch | Up to 3 files: one owner-scoped test module, `gateway/engine_egress.py`, one transport file only if inspection proves it necessary and owner scopes it. |
| 3 | Credential canaries, derived URLs, deadlines/bytes/polling/cancellation | Up to 4 files: one owner-scoped test module, `gateway/engine_protocol/dispatch.py`, `gateway/engine_egress.py`, the same scoped transport file if needed. |
| 4 | End-to-end approved remote job and all denials; local/paid regressions | 2 files: one owner-scoped end-to-end test module, `docs/design/REMOTE_ENGINES.md` updated with exact receipts and verified limitations. |
