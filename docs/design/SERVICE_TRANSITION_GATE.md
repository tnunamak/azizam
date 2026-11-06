# Service transition gate (slot T1), revision 3

Status: design, revision 3, 2026-11-05. Step T1.1 (domain leases) is built; see "Revision 3.1 notes".
Revisions 1 and 2 were reviewed by independent reviewers. Revision 3 takes every finding of both reviews in D1 to D5
and the build gates. It also states the guarantee honestly and splits T1 into steps that each ship on their own.

## Why

A restored KV snapshot gives the same answer as a hot cache. A restored 32K continuation replies in 4.6 s; a cold one
takes 154 s. That is a reason to build this gate. It is not a safety proof.

## The guarantee, stated honestly

For a **transition-enabled residency domain** the gate promises four things:

1. After close, there is no new admission.
2. No disruptive effect (checkpoint, release, load, restore) starts until all work admitted before close is proven safe.
   "Proven safe" means the leases have drained **and** the adapter gives fresh evidence that the engine is idle.
3. No serving dispatch happens while an effect runs.
4. An effect whose outcome is unknown keeps the gate closed until it is reconciled.

It covers mediated traffic only. It does not control memory capacity: legacy capacity admission stays as it is, and
arbitration records stay **shadow**. T1 adds no memory-capacity guarantee.

## Decisions

### D1. One domain key, one lock, one lease handoff

- A **residency domain** is the canonical key. It joins every service record, alias, forwarder and custom
  `exclusive_scope` that reaches the same engine process. The domain is declared in the service config
  (`residency.domain`, default the service id). Locks, leases, gate state and operations all use this key.
- **Every mediated dispatch to an enabled domain crosses one of three gateway hops, and each hop leases there.** Each
  takes its lease under `swap_lock_for(domain)`, whatever the global `llm.swap_drain` setting says:
  1. The internal LiteLLM local route (`_forward_internal_litellm_local_upstream`, proxy.py about line 30711). This is
     the existing generic hop for `gateway_lifecycle` services: LiteLLM's `api_base` points back at the gateway
     (litellm_substrate.py, the `_gateway_lifecycle_api_base` deployment branch).
  2. The current-loaded path (proxy.py about line 29481).
  3. Protocol dispatch (`_try_protocol_request`, `runtime.lease`).
- **LiteLLM direct is refused for enabled domains.** A service with `transition.enabled` must be `gateway_lifecycle`
  routed. If it is not, config load refuses the service with a precise error (D1 config check). LiteLLM therefore never
  calls an enabled engine at its own address. The LiteLLM outer forward activates with `take_lease=False` and holds no
  lease, so the inner hop's lease is the only one. There is no nesting and no duplicate.
- **Unowned processes.** Strata's process is owned by its own Control, not by a gateway driver. T1.1 must show that the
  `gateway_lifecycle` hop forwards to such a service with a pass-through activation: no start, no swap, only lease and
  forward. If it cannot, T1.1 adds that pass-through as a declared activation mode. Moving Strata to this hop changes
  its live routing, so it is a coordinated cutover with the operator lane, not part of the code merge.
- **Direct callers.** Traffic that reaches the engine without the gateway is unmediated and outside the guarantee. That
  covers a client calling LiteLLM's or the engine's own port. The engine port should bind to localhost or a private
  socket; the operator lane verifies this at cutover (D6).
- Each path has one bounded admission deadline (`admission_wait_s`). It takes precedence over a legacy `queue_wait_s`.
  A request cancelled while it waits mints no lease.
- **Under the lock**, at every one of the three hops, before minting a lease, the request re-reads the gate state (D2),
  the manual availability override and the current config. The current-loaded path does not re-check today; T1.1 adds
  it. Only an `open` gate, with no manual restriction, admits. A queued request that passed a
  check before a failed transition therefore fails closed once it gets the lock.
- An unleased forward to a transition-enabled domain is refused, not sent.
- The step functions (select, load, release) are shared and non-locking. `transition` takes the lock once.
  `SwitchController.select` is rebuilt on the same steps, so it is never nested.
- A successful select or wake mints the trigger's lease before it releases the lock, as `select` does today. No
  transition can run in that gap.

### D2. One durable operation record is the gate's authority

A small table holds one current operation per domain. That record is the authority for the gate; the event log is a
view of it.

```
transition_op(op_id, domain, kind, idempotency_key, incarnation, config_revision,
              state, step, step_intent, step_outcome, evidence_ref, snapshot_handle, created, updated)
gate(domain, state: open | closed | failed_closed | reconciling, op_id)
```

- **Serialization.** The transition first takes `swap_lock_for(domain)`. Then one transaction runs a compare-and-set:
  it inserts the operation only if the domain has no unfinished operation (a unique partial index on `domain` where
  `state` is not finished), and it sets the gate to `closed`. A second contender fails the insert and gets the existing
  operation (`409` with its id), or the same operation if its idempotency key matches. The lock is released only after
  the gate is set to `open`, `failed_closed` or `reconciling`, so the closed state is durable before the lock goes.
- **Admission reads the durable gate.** The hops read a per-domain gate cache. Each gate write invalidates it, in the
  same process. At startup the cache is loaded from the table before the first request is admitted.
- **Write-ahead.** Each effect has two records:
  1. Before the call, one transaction writes `step_intent` (effect, effect key, expected incarnation).
  2. After the call, a second transaction writes the outcome: `success`, `no_change`, `partial` or `unknown`, with its
     evidence reference.
- **Startup.** The gateway reconciles every operation that has not finished before it admits anything to that domain.
  A domain with an `unknown` step starts `reconciling`, which answers 503 with Retry-After until an owner or a recovery
  query resolves it.
- **Retries.** `POST` with the same idempotency key returns the same operation, so a lost client response does not
  repeat effects.
- **Cancellation.** Once an effect has been dispatched, a cancelled caller does not reopen the gate. The executor runs
  on a task that the caller does not own, and its deadlines are its own.
- **Arbitration.** Arbitration records are written from this operation as shadow facts. They are labelled shadow and are
  never called a grant.

### D3. Effects are declared operations with explicit outcomes

The declared operation shape is an extension of the http-v1 grammar. A reviewed adapter can take an operation that
does not fit that grammar.

```yaml
residency:
  domain: <key>
  transition: {enabled: true, admission_wait_s: 30, drain_deadline_s: 60, retry_after_s: 30}
  idle_evidence: {observation: <name>, fresh_within_s: 2}     # e.g. no slot processing
  operations:
    checkpoint: {op, args, result: {handle: <selection>, bytes: <selection>}, verify: <observation or result predicate>,
                 failure_semantics: no_change | unknown, deadline_s, query: <op>}
    release:    {op, args, verify: <observation>, releases: [<resource ref>], residual: <observation or bound>,
                 attested: true|false, deadline_s}
    load:       {op, args, target: <offering>, verify: {selected: <obs>, ready: <obs>}, deadline_s}
    restore:    {op, args, verify: <predicate>, failure_semantics: no_change | unknown, reset: <op>, deadline_s, query: <op>}
    delete_snapshot: {op, args, verify: <predicate>}
  checkpoint_policy: best_effort | required
```

- **Attestation is not owner data.** `failure_semantics`, `attested`, the idle-evidence observation and the snapshot
  completion and integrity predicates live only in a **reviewed transition profile**. A profile is either a versioned
  file under `registry/` that changes only through a reviewed commit, or a reviewed adapter. The owner-editable service
  config can only name a profile id and fill in its plain arguments (paths, slot ids, deadlines). It cannot set or raise
  an attestation. A config that names an unknown profile, or tries to set an attested field, is refused at load. This
  is the rule "data extensibility is not authority extensibility" (CODE_TO_DATA.md).
- **Evidence is fresh and correlated.** Each observation a profile names returns a value with `observed_at`, and with
  the operation correlation id where the engine echoes one. Evidence older than `fresh_within_s`, or for another
  incarnation, counts as unknown.
- **Results.** `invoke` returns a bounded, selected result: a handle, bytes and a correlation id. It no longer discards
  the reply. An operation that times out is `unknown`, unless its declared `query` resolves it.
- **Release.** A release counts as stock released only if the adapter declares `attested: true` for that verify
  predicate. Otherwise it is recorded as `observed_not_resident` and stays charged in shadow. A residual bound stays
  charged.
- **Load.** Load verifies the **target** identity and readiness, not just "something is resident".

### D4. Order, with each failure's outcome

The table below gives the ordered steps. The success path ends with **open**, which releases the lock.

| Step | Action | On failure |
|---|---|---|
| 1. Close | Take the lock, then the compare-and-set transaction (D2): insert the operation and set the gate to `closed`. | Another unfinished operation exists: release the lock and answer `409` with its id. |
| 2. Drain | Wait for the leases to drain, up to `drain_deadline_s`. Then require fresh idle evidence. | Deadline passed with no effect started: reopen; nothing changed. Each caller keeps its own admission deadline. Idle evidence unknown: stay closed and retry until the deadline, then reopen with no effect. |
| 3. Checkpoint | Only if declared and the source is resident. | `best_effort`: a verified `no_change` failure is logged and skipped, and release goes ahead. The receipt records that the next start is cold. `required`: abort, and reopen only after a re-check that the old state is still servable. `unknown`: stay closed and reconcile. |
| 4. Release | For `park`, and for `select` when the target needs it. | `unknown` or verify failed: `failed_closed`. |
| 5. Load | Load the target, then verify target identity and readiness. | Partial or unknown: `failed_closed`. Recovery to the previous offering is a **new compensating transition**, with the same checks; it never calls the old rollback helper. |
| 6. Restore | Only if a compatible published snapshot exists (D5). | `no_change`: serve cold. Otherwise run the declared `reset`, then verify a clean target and serve cold. `unknown`, or no reset: stay closed. |
| 7. Open | For `select` and `wake`, mint the trigger's lease under the lock, then set the gate to `open`. | — |

**After a failed checkpoint.** Any `pending` snapshot from that checkpoint is marked `abandoned` before release. It
can never be published, so the next wake starts cold.

**After a park.** The domain is not resident. Its gate is `open`, but serving it needs a `wake` first: the next request
triggers `transition(wake)` through the same gate, or the request is refused if policy says so. Missing is never ready.
Requests that arrive during the wake wait on the lock up to `admission_wait_s`. The trigger's lease is minted under the
lock before it is released. Before restoring, the wake reconciles `pending` snapshots and restores only a `published`
one.

### D5. Snapshot contract

- **Compatibility.** The adapter produces an opaque **compatibility token** for the resolved effective state: the
  engine and cache-format versions, the model artifact identity, the tokenizer identity, the relevant effective
  settings, and the slot or session identity. The adapter attests the token at save and again just before restore. Core
  compares tokens only. Unknown or a mismatch means cold, with no restore attempt. The engine still validates before it
  changes any state.
- **Lifecycle.** A snapshot moves through `pending`, then `published`, then `abandoned`, then `deleted`.
  - The generation number is allocated once, durably, and never reused.
  - The latest-snapshot pointer is published only after the checkpoint completion and its integrity check are both
    verified and correlated.
  - On restart, any snapshot still `pending` is reconciled: the gateway queries it, or deletes it.
  - The profile defines completion and integrity evidence: for example, the save reply's byte count and a checksum the
    engine returns or the adapter computes. A snapshot with no such evidence is never published.
  - If the profile has no query or delete operation, a `pending` snapshot can never be published. Disk use is bounded
    by the retention limits, and an over-limit domain refuses new checkpoints (best-effort: skip; required: abort).
- **Handles.** The adapter owns the physical paths. Core holds an opaque handle that is checked against the adapter's
  pattern, never an unchecked path.
- **Retention.** Each domain has a byte limit and a count limit. Old and orphan snapshots are deleted through the
  declared delete operation, with a verify. A snapshot that is pinned or being restored is never deleted.
- **Privacy.** A snapshot holds private session cache. It stays in the engine's private directory and is never served.
  An erasure of a person's data deletes the snapshots that their sessions reached, where the adapter can say which those
  are; where it cannot, the snapshot is treated as owner-private operational state, like the execution spool.

### D6. One effector

Every gateway path that changes state in a transition-enabled domain goes through `transition`, or is refused. That
covers:
- native and raw control forwarding;
- process stop and restart;
- `EngineControlRuntime.load`, `unload`, `sleep` and `wake`;
- implicit-load paths;
- the gateway's idle managers.

The external policy process gets a key that can call only the transition endpoint, where that is practical. Its direct
engine control is removed.

Cutover:
1. Stop the old idle unload.
2. Reconcile any outstanding unload.
3. Record a new domain incarnation.
4. Enable automatic restore.

The external process could keep direct engine access. If it does, the boundary is stated as such, and the gate treats
any out-of-band change it observes (an incarnation or residency it did not cause) as `reconciling`.

### D7. Receipts and log

A transition receipt carries:
- the operation id, domain, incarnation and config revision;
- the outcome of each step, with its evidence reference and any uncertainty;
- the fallback reason (for example "checkpoint failed, cold next start");
- the snapshot disposition;
- the final serving state, recorded separately from the residency state.

The private admission evidence gains `service_id`. That ships as its own small change, outside this gate's acceptance.
(Correction: revision 1 called `_service_availability_response` dead. It is not; ten routes call it through
`asyncio.to_thread`, which an earlier grep missed. It stays.)

## Revision 3.1 notes (from building T1.1, 2026-11-05)

- **Four hops, not three.** The per-engine native lane (`/native/{engine}/...` and `/gateway/v1/providers/{p}/...`,
  `_forward_provider_native`) also dispatches to the engine. For an enabled domain it now leases under the domain lock,
  with the same re-check, and holds the lease to the reply's final byte. A native call made while the gate is closed is
  refused, reads included. The transition executor therefore calls the engine directly, not through the native lane.
- **The re-check is in the lease mint.** `SwapDrain` takes an `admit` hook that runs at every mint, under the lock. So
  the switch path (`SwitchController.select`) and every other mint get the gate, override and config check, not only
  the call sites edited here.
- **Declared keys.** `residency.domain`; `residency.transition.enabled`, `.admission_wait_s` (default 30 s),
  `.activation: pass_through`. Transition config sits inside `residency` so the registry schema gains no new top-level
  field. Any member service enabling transition enables its whole domain; the domain's bound is the shortest declared.
- **Pass-through is a declared mode.** Live Strata (`flash-next-strata-gpu1`) has a top-level `process` block with
  driver `command` (`systemctl --user start strata-flash-next.service`). Without the mode, the lifecycle hop would start
  that unit when its health check fails. With `activation: pass_through` a request never starts, stops or swaps the
  process; it only leases and forwards.
- **Projection fix.** The runtime backend projection dropped a schema-2 record's top-level `residency`. It now carries a
  non-empty one. No live record declares one, so live behaviour does not change.
- **Gate store.** `data/transition.db`, table `transition_gate`. T1.1 only reads it.

## Build order (each step ships alone)

Every step is opt-in by `transition.enabled`. Each step's tests include a legacy-path suite: a service that is not
enabled behaves exactly as before. That suite checks swap-drain on and off, all three hops, and the current queue and
drain deadlines.

| Step | Content | Ships when |
|---|---|---|
| T1.0 | Receipt `service_id` in the private admission evidence | The receipt test shows it; the caller view does not |
| T1.1 | D1: domain key; leases at the three hops for enabled domains; bounded admission; gate re-check under the lock; refusal of LiteLLM-direct config for enabled domains; the gate table and cache, read-only (always `open` until T1.2) | Oracle items 1, 3, 4, 5 and 10 pass on fakes; a real Strata chat stream holds a lease |
| T1.2 | D2: operation record, write-ahead, startup reconcile, idempotent endpoint; release and load only (no checkpoint) | Oracle items 2, 6, 7 and 8 (load part) pass |
| T1.3 | D3 and D4 with checkpoint and restore; outcome semantics; idle evidence | Oracle items 8 (restore part) and 9 pass |
| T1.4 | D5 snapshot lifecycle and retention | Oracle item 9 (storage part) passes |
| T1.5 | D6 effector inventory and refusal; cutover with the operator lane; then one real park and wake on Strata | Real run: pinned identity, exact tokens against a cold baseline, a forced restore failure, verified release |

## The oracle

These are the fake-engine tests. A second, differently shaped fake adapter must pass the same tests.

1. A request leased before close, paused just before its upstream dispatch, then close and drain. No effect starts until
   it is finished and the engine reports idle.
2. Cancellation at each of these points: before the lock, during every await, and after the engine accepted the request
   but its reply was lost. Every unknown outcome stays closed.
3. Streams: normal completion, early disconnect, stream error, a body that never started, and a repeated close.
   Finalizers protect against leaks; they are not proof of engine idleness.
4. A queued request that passed an availability check before a failing transition, then takes the lock. It re-checks and
   fails closed.
5. Every forwarding family: aliases on one domain; buffered and streaming; nested activation with no duplicate lease.
   Positive control: remove one lease; the test must fail.
6. Concurrent `select`, `park` and `wake`; repeated POSTs with the same key; a gateway restart in each step; stale
   executor messages; an out-of-band unload.
7. A release with a stale `resident=false`, a wrong incarnation, an unknown probe, a residual allocation, or a delayed
   completion. None of these settles stock.
8. Failed or partial load; failed or partial restore; a restore that succeeds after its timeout; a reset that fails; a
   compensating transition whose new grant is refused.
9. The same declared arguments with a changed artifact, tokenizer, engine or defaults; snapshots that are corrupt,
   missing, orphaned, half-written or out of storage; a crash before and after the pointer is published.
10. Same-offering concurrency and progress in independent domains still work. Timing and Retry-After values are checked,
    not just status codes.

The existing TLA model does not cover this gate. T1.2 adds a state-machine test of the operation record against these
linearization points. Extending the model is optional; the test is required.

## Out of scope

Cross-mode cache handoff, more than one slot per operation, idle policy in core, and memory-capacity enforcement
(arbitration slices).

## Operation interface (written before T1.2 and T1.3 are built)

`gateway/transition_ops.py` holds the types and protocols that T1.2 and T1.3 build against at the same time. It has
no behaviour.

- **T1.2** implements `OperationStore` (the `transition_op` and `transition_gate` tables in `data/transition.db`),
  `TransitionExecutor`, the steps close, drain, release, load and open, startup reconcile, and the owner HTTP surface
  listed at the end of the module.
- **T1.3** implements `SnapshotStore` and the checkpoint and restore steps (with `reset`). It registers them in the
  step table at their places in `STEP_ORDER`; it does not edit the executor.
- **Adapters** implement `TransitionAdapter`. An adapter that cannot checkpoint sets `supports_checkpoint = False`,
  and the executor skips checkpoint and restore. Engine-specific adapters stay outside the gate core.
- **The proof harness** drives only the HTTP surface: park, then wake, then replays frozen request bodies and checks
  the cached prefix. It names no engine; the domain and the bodies are its arguments.
