# Azizam resource arbitration: the foundation (draft 7, 2026-11-05)

Status: design. Draft 5 got three independent red-team reviews on 2026-11-05:
- systemic, using Meadows' leverage points
- conformance and fit with the existing code, with 13 exotic engines
- prior art: Kubernetes DRA, Nomad, Slurm, Ray, YARN, HTCondor, Borg, Omega, Mesos/DRF, Kueue

All three approved the direction. All three found the same gaps:
- reusable memory and spent budgets are different things
- an observation must say what it measures
- a ledger grant is not a crash-safe action
- the design promised more physical safety than its mechanism gives

This draft fixes every blocker and major finding. §13 maps each finding to its fix. Draft 5 is kept as FOUNDATION-draft5.md.

Why the foundation exists: "we can't hardcode a bunch of domain or model specific stuff ... There could even be new engine categories tomorrow and ideally they could work ... the most important thing is that it's right at the foundation." "Azizam isn't about me."

## 0. Grounding in prior research

Each design claim rests on research already done (internal research notes).

- **Rounds 1 and 2 of the GPU-sharing research** (2026-11-04) say:
  - The problem is eight separate decisions.
  - Policy is owner data over tested mechanism.
  - A memory need is graded, not known.
  - The unit of release is the engine's own verified action.

  This draft keeps all of that, and generalizes it beyond GPUs.
- **No shared engine definition exists across 19 local runners:**
  - No two runners agree on what an engine or "ready" means.
  - Residency is unknown unless the operator says how to observe it.
  - Hence §3.2 (an unknown is never zero) and §3.5 (actions are declared and verified).
- **A gateway can start and stop any engine with three fixed drivers:** fixed generic drivers plus per-engine data. Hence the split between core, adapters and data in §7.
- **Owner-defined dimensions over imported evidence, and the category-policy red teams:**
  - Labels from outside are evidence, not truth.
  - Grants are positive, deny wins, and a missing label fails closed.
  - Hence §3.4 (provenance is not a bound) and §3.6 (unknown classes fail closed).
- **Household AI gateways should explain waits with observed phases and reason codes**, and **status surfaces that explain waits:**
  - No surveyed system gives per-request device time or a reliable queue ETA.
  - A wait is explained by phase and reason, not by an invented position.
  - Hence the decision records in §4.6.
- **Headless parity needs one authorized operation layer:** one operation layer, exposed through REST, CLI and MCP. The ledger and policy are operations in that layer, not UI-only features.
- **Work allocation should optimize realized value:** Meadows puts the leverage in goals, information flows and rules, not in slot use. Hence §10 level 3: useful service is the goal, and truthful outcomes are an invariant.
- **Statechart skills that check a TLA+ model**, and **model-based testing from state machines:** model state machines in TLA+, check them with TLC, then generate test paths from the graph. Hence the slice 0 formal model (§11).
- **Arguing a design is optimal needs a stated objective:** a design can be called optimal only with a stated objective and a lower-bound argument. Otherwise, the honest claim is "it satisfies the requirements and survived red teams". That is the claim made here.

**Gap now filled.** Accounting for spent and refilling budgets was the one part not grounded in research. It is now covered by a research note: concurrent spend and rate accounting needs reserve-before-dispatch (2026-11-05). See §14.

## 1. The problem

Azizam passes work from people and apps to engines it does not own. Work uses things that run out:
- memory on a device or a host
- execution slots
- a provider's tokens per minute
- a monthly money budget
- a lab instrument
- a consumable material

For each piece of work, Azizam decides one of these:
- start it now
- make room, then start it
- let it wait
- refuse it

It states the decision, the reason, and the strength of the guarantee behind it. It keeps its accounting true while engines, outside programs and people change things.

The core must do this without knowing what the scarce things are, what the engines do, or what the work means.

## 2. Paradigm, and its limits

**Azizam is a broker of declared claims against declared and observed capacity, with evidence.** It knows a small, explicit algebra of resource semantics. It knows nothing about domains.

- All engine-, device- and domain-specific facts come in as **data** or through an **adapter**. Data means registry records and owner policy. An adapter is code at the edge that speaks one engine's protocol.
- The core is generic in its semantics, not only in its names. Its vocabulary has a fixed, small set of accounting kinds (§3.1), observation meanings (§3.2) and action effects (§3.5). A new *domain* needs data plus at most one adapter. A genuinely new *semantic*, one that none of these kinds can express, is a core change. That change is deliberate and reviewed. It is not hidden in an adapter.
- **What arbitration does not do.** It does not decide process safety, business authorization or physical interlocks. Two examples:
  - A capacity-1 token prevents two holders. It does not make a robot motion safe.
  - Admission does not authorize paid fallback.
  - Adapters and external controllers keep those duties, and the broker records what it does not cover (§6).
- **When to change the paradigm.** Revise the abstraction if any of these happens:
  - A real engine needs a semantic that data and one adapter cannot express.
  - An adapter must keep its own second ledger to be correct.
  - The behavioral conformance suite (§8) cannot state a new case without a core edit.

  When one of these happens, change the paradigm. Do not force the case into the nouns.

## 3. The six nouns, with their semantics

### 3.1 Resource

- **Identity and version:**
  - `id`: opaque and stable.
  - `incarnation`: changes when the physical thing behind the id changes, for example a replaced device or a new account.
  - Holdings stay attached to the incarnation they were granted on.
  - A renamed or deleted resource keeps a tombstone until its old holdings settle.
- **Units:** `unit` and `granularity`. Quantities are exact integers in the smallest unit, with checks for overflow. Units are typed, so no arithmetic mixes them. Money is a unit with a currency identity and fixed precision.
- **`accounting`:** one of a small, closed set of kinds, each with parameters:
  - `stock`: reusable. A holding returns capacity when it is released. Examples: memory, slots, a device.
  - `window_budget`: replenishes on a declared schedule. Examples: a fixed window, a sliding window or a token bucket, with period, refill and reset authority. Use is reserved, then committed, then settled. Completion does not refund consumed use. Settlement may return the *unused* part of a reservation. Only replenishment, under the declared rule, restores consumed credit (§14).
  - `balance`: does not replenish. Examples: a monthly money budget or a consumable reagent. Reserve, commit and settle as for a window budget. An owner action may top it up.
  - `opaque_budget`: an adapter keeps an outside budget whose rule is unknown. The core reserves conservatively and accepts only settlements reported by the adapter. It never invents a refill rule.
- **Coupling:**
  - Ids are independent unless data says otherwise.
  - Data can declare `alias_of`, `parent` (the child's use also uses the parent) or linear coupling constraints.
  - The ledger checks every grant against all coupled budgets. Two names can therefore never double the capacity of one physical thing.
- **Authority mode:**
  - `exclusive`: every user of the resource goes through this ledger.
  - `partitioned`: this installation owns an enforced sub-budget.
  - `shared`: other users exist that the ledger cannot fence.
  - The mode limits the guarantee (§6).
- **Labels:** words for people, as data.

### 3.2 Observation

A typed fact from an observer adapter:
- `meaning`: one of total, free, used, remaining, cumulative-consumed, holder-usage, or health.
- `scope` and `incarnation`.
- `source` and `sequence`: monotonic per source.
- `sampled_at` and a validity horizon.
- `uncertainty`: a bound.
- `quality`: measured, declared or estimated.

Rules:
- Unavailable or stale is never zero and never "fine".
- An out-of-order sample, or one from an old budget window, never restores credit or releases a holding.
- Each accounting kind has **one balance equation** (§4.2). A free-reading observer and a used-reading observer must give the same grant result.
- Aggregate observations never prove that a particular holder ended. Only holder-scoped evidence can do that (§3.3).

### 3.3 Holder

Who holds part of a resource. Every holder has:
- a stable id
- an attribution: principal, app and class
- a state
- its current holdings and their bound

The kinds of holder:
- **Attempt.** One try at one request.
- **Residency.** What an engine instance keeps between attempts. It has an identity and a member set, and it is charged **once** however many attempts use it. A delta is computed against a specific, versioned baseline. When the last member leaves, the residency stays until a release is verified.
- **Outside.** Use that Azizam does not manage. Its upper bound is computed only from observations, through the balance equation. It is never given exactly to an engine when the breakdown cannot be known.

A holding is a **reservation**. It is not a lease that expires. Two separate things:
- **Authority expiry**: the holder lost the right to start new effects.
- **Settlement**: the evidence shows that use has ended.

A timeout is only a failure detector. It never settles a holding (§4.4).

### 3.4 Claim

What an attempt asks for, against a concrete set of resource ids:
- **Phase requirements are simultaneous vectors in a small phase graph.** Each transition declares:
  - the holdings it needs first
  - the peak extra holdings while it runs
  - the result that stays afterwards
  - any transfer of ownership, for example from a working holding to residency

  A phase cannot start before its extra reservation commits. A negative delta releases nothing before verification.
- **Growth:** `bounded` (the full upper bound is reserved up front) or `amendable`.
  - With `amendable`, the adapter stops at a safe point and asks for an atomic extra amount. If the extra amount is refused, the attempt keeps what it holds and is cancelled or waits within its deadline budget. Deadlock handling is in §4.5.
  - An engine that can neither bound nor pause its growth is **unsupported for guaranteed sharing**. It runs only inside an enforced or reserved envelope, or as an advisory attempt (§6).
- **Each quantity carries:**
  - `bound_type`: enforced limit, conservative upper bound, estimate or unknown
  - `derivation`: declared, computed, measured or learned
  - its evidence: issuer, engine version and adapter version, configuration fingerprint, the input range it applies to, the concurrency envelope, sample support, error statistic, age and invalidation rule

  The derivation is not ranked as safety. Only `bound_type` decides admissibility.
- **Claim functions** are written in a small, total, bounded expression language over typed request fields that the offering declares:
  - constant, table, linear, step and max
  - explicit unknown and error values
  - an evaluation budget
  - inputs are normalized once and used both for the estimate and for dispatch
  - NaN, negative numbers, overflow and fields outside the range are rejected

  The core does not know what the fields mean.

### 3.5 Action

An adapter operation that changes holdings or engine state. The identity is opaque, for example `release`, `load`, `calibrate` or `purge`. The *effect schema* is generic:
- preconditions, including the safe-idle predicate
- the affected-set version
- the resource demand while the action runs
- postconditions and residual bounds
- idempotency and recovery behavior
- fencing support
- irreversibility scope and whether outside authorization is needed
- the evidence that proves the semantic result, which must be newer than the action's causal boundary

Verification status is recorded against:
- the adapter version
- the engine version
- the configuration fingerprint
- the conditions under which it applies

Two kinds of completion are different things:
- **Transport completion**: an HTTP 200 or the end of a stream.
- **Resource settlement**: evidence of the effect.

Existing release hooks only *trigger* reconciliation. They do not prove that resources are free.

Physical safety semantics stay in the adapter or a certified external controller. An action that needs outside authorization gets a visible "not covered by arbitration" result unless that evidence is present.

### 3.6 Policy

Owner data in a **bounded declarative language**. It has explicit precedence, conflict rules, provenance, numbered revisions, rollout and rollback. It contains:
- **Classes:** opaque, owner-named. Today's three are default data.
- **Class resolution** from trusted labels: categories, offerings, apps and principals. An unknown class fails closed.
- **Compatibility:** a symmetric relation, which need not be transitive, enforced *directly* against active holders inside the grant transaction. There is a rule for self-compatibility. Group tokens are a compiled optimization only, and only where they are proven equivalent. Cardinality limits are explicit constraints.
- **Revisions:** say whether existing holders are kept as they are (grandfathered) or the resource enters a state where new conflicting admission waits.
- **Milestone deadlines:** admission, dispatch, first output and terminal outcome. They come from **one** monotonic remaining budget per attempt, so limits never add up.
- **Order:** first come by default. Optional extras:
  - a bypass limit for smaller claims
  - protected opportunity, defined on feasible admission progress, with a finite deadline and an explicit scope
  - principal-level admission budgets, to counter request splitting
  - bounded qualification opportunities, so that new offerings can collect evidence
- **Reserves:** per resource. Each is either extra headroom or a minimum allowance for outside use, declared, so it is never subtracted twice.
- **Release permissions:** per engine instance.
- **Gates:** on any signal. Each gate has enter and exit thresholds, a settling period, behavior when the signal is stale, and coordinated transitions. Retry-After values are spread out so that clients do not retry together.
- **Guarantee floor:** which guarantee level each class requires (§6). For example, "never admit at advisory level" or "advisory allowed with a warning".
- **Service objectives:** chosen by the owner, per class and per principal. Examples: completion within a time, or a minimum share of admission opportunity. The core measures them and does not choose them.

**Policy checking** happens before a revision is published. The checker reports:
- classes that can never be reached
- ordinary claims that can never be satisfied
- orphan resources
- contradictory pins, minimum residency, compatibility and deadlines

It runs a trace simulation with adversarial scenarios, not just a preview of changed decisions. When rules conflict, it explains which owner-controlled rule would allow the claim. It never weakens safety to restore liveness.

## 4. The authority: the ledger

### 4.1 Transactions

- There is one store per installation. Each grant is **one serializable transaction** over all affected rows and their versions.
  - Planning happens outside the transaction.
  - The commit validates versions and invariants: coupled budgets, compatibility, policy revision and evidence revision.
  - External reads and actions never happen inside a transaction.
  - Sorted lock order is an implementation detail. The acceptance criteria are conservation and serializable outcomes.
- A transaction commits all of these together:
  - ownership and reservations
  - the compatibility check
  - the drain barrier, when one is needed
  - the operation intent (§4.3)

  It does not claim that external effects are atomic.
- Quarantine is scoped to the affected resource and the closure of what depends on it. A slow observer cannot block resources that are not related to it.

### 4.2 Balance equations

- **For a stock**, all values are bounds:

  remaining = total_lower − managed − outside_upper − reserve − uncertainty

  - `managed` is visible plus not-yet-visible managed holdings, without overlap.
  - Two events move a holding from not-yet-visible to visible: holder-scoped evidence, or the adapter's materialization event.
  - Free-reading observers use a separate normalization. It subtracts only liabilities that the reading does not already include.
- **For a window budget**, current window credit = limit − committed in this window − reserved − uncertainty. Each window has an epoch. Reports from older windows cannot add credit.
- **For a balance**, the same as a window budget, but without replenishment.
- A negative residual is **drift**. The resource is quarantined, and the decision says so.

### 4.3 Operations: the effect protocol

Every external effect is a durable operation with these states:

planned → committed → dispatched → effect verified → settled

It also has the states `withdrawn`, `failed` and `unknown`.

- **`committed`:** one transaction writes the reservation, the barrier and the intent together. There is no crash window between them (model finding A1).
- **`withdrawn`:** the ledger cancels a committed operation that was never dispatched. The dispatcher checks the operation's state and fence in the same step as it sends, so a withdrawn operation can never be sent. Withdrawal is a ledger-local return, not a settlement (A9).

The protocol is model-checked (§4.7).

- **Before any external step**, one transaction does all of the following:
  - sets the drain barrier, so that new admissions to the affected domains either come before it or wait
  - checks again the idle and safety preconditions and the holder and policy versions
  - reserves the capacity that the transition needs
- **Each operation has:**
  - an idempotency key
  - the expected engine and resource generation
  - the adapter's contract for querying and recovering it

  The effecting boundary enforces a monotonic fence where the adapter supports one. For an adapter that cannot be fenced, only one controlled executor may act. Failover action is refused while the previous executor's authority is uncertain. Generation metadata alone is not a fence.
- **No blind replay.** Work that cannot be replayed becomes reconcilable through the adapter's query contract or through operator reconciliation.
- **Transport leases are separate from resource holdings.** The serving lease that protects a stream, `hand_off` included, keeps its existing behavior. Ending it starts reconciliation. It does not settle resources.

### 4.4 Holder states and recovery

`active`, `degraded` (evidence is weak), `quarantined` (drift, or no authority), `reconciling`, and `settled`.

Each adapter declares the **recovery evidence** it can give. Any of these settles a holding:
- an identifiable process exit
- an authoritative attempt status from the engine
- proof that the resource incarnation was replaced
- an **explicit operator reconciliation**, which is audited

The design states the recovery path for every resource:
- An engine that cannot give automatic evidence is marked "manual recovery only". The refusals it causes include the steps to recover.
- Control and recovery paths do not need the resources that they recover.
- The system tracks how long holdings stay unresolved, and how long resources stay blocked.

### 4.5 Waiting and growth

- A holder that waits for an extra amount keeps what it holds only within its deadline budget.
- Wait-for cycles between amending holders are found when they are requested. The younger request is refused, with the reason.
- Infeasibility is checked separately from a temporary lack of room. A claim that can never fit is refused at once, and it never blocks protected opportunity.

### 4.7 Model-checked protocol

The model is in `tla/` (Ledger.tla, written 2026-11-05). TLC checks the good protocol with no error:
- 61,240 distinct states, depth 23
- 7 safety invariants, 1 action property and 2 liveness properties hold
- the lead re-ran this independently

Three broken variants each fail as they should:
- a timeout that releases a holding breaks `SettledHasEvidence`
- an old-window report that restores credit breaks `WindowNoRefund`
- a takeover with no fence breaks `NoStaleEffect`

The model turned up twelve design gaps. This draft resolves them as follows.

| # | Gap | Resolution |
|---|---|---|
| A1 | Is intent in the grant transaction or a later step? | In the same transaction (§4.3 `committed`). |
| A2 | What does the barrier cover? | The operation's affected set (the resources and residency domains it touches), not the whole installation. |
| A3 | Which window pays for a charge that crosses a rollover? | The epoch of the grant. Dispatch is allowed only in that epoch. After a rollover, the operation is withdrawn and granted again in the new epoch. |
| A4 | When is the fence raised? | In the takeover transaction itself. A new generation raises the fence before it may act. |
| A5 | Can an executor that cannot be fenced ever prove that an in-flight message is gone? | No. Time is never safety evidence. In-flight effects from an old executor stay `unknown` until engine-side evidence (A12) or a replaced incarnation. Engines whose adapters cannot be fenced are therefore "manual recovery only" for failover. |
| A6 | Can operator reconciliation free a holding the engine still runs? | The operator must attest that the engine is not running the key. Where the adapter supports it, reconciliation also writes a tombstone for the key at the engine. Without that, the decision records the attestation as the only evidence, and the level for that resource drops to advisory until fresh evidence arrives. |
| A7 | Holder states were not mapped to operation states. | committed/dispatched → `active`; evidence stale or past its horizon → `degraded`; unknown, drift or lost authority → `quarantined`; recovery query or operator action in progress → `reconciling`; settled or withdrawn → `settled`. The model encodes this mapping. |
| A8 | A deadline promises a durable decision, but a holding can stay unresolved. | The deadline promises a *decision*: started, refused, or outcome unknown. It never promises settlement. These two are separate by design. |
| A9 | The cancel path for an operation never sent had no name. | `withdrawn` (§4.3). |
| A10 | "Release" was ambiguous. | A **release action** is an adapter operation. A **settlement** is the ledger returning capacity on evidence. Capacity from a stock comes back only on settlement. |
| A11 | Evidence freshness was undefined. | Evidence counts only if it carries the operation's idempotency key, or a source sequence after the commit of the intent. |
| A12 | Safety when evidence is absent needs the engine to reject a key after cancellation. | This is part of the adapter contract. An adapter is **fenceable** if the engine rejects stale generations or tombstoned keys. Otherwise, A5 applies. |

### 4.6 Decisions and receipts

Every grant, wait, refusal and settlement writes a structured decision. The decision records:
- the resource ids and incarnations
- the rule and policy revision
- the facts used, with meaning, quality and age
- the guarantee level (§6)
- the attempt key

A deadline guarantees a **durable decision that the client can retrieve with its attempt key**. It does not guarantee delivery over a broken connection.

There are two projections:
- **private evidence**, for the owner
- a **caller-safe projection**, which does not expose owner labels or prose unless policy publishes them

## 5. Making room

The planner is **bounded**. It works within a fixed planning budget, and its fallback is to wait or refuse, never to act partially.

1. Candidate actions are the permitted, verified actions whose preconditions hold.
2. Effects are computed against unique holdings, using the action dependency graph. Shared residency is never counted twice. Actions that contradict each other are excluded.
3. Ranking uses expected cost (reload time, collateral loss, the age of holdings), minimum-residency protection and policy pins.
4. The chosen plan is checked again when its barrier is set (§4.3). Room counts as made only after the effect is verified and the resources settle.

The planner never knows what a model, a workflow or a device is.

## 6. Guarantees, stated honestly

| Level | Holds when | What Azizam promises |
|---|---|---|
| **Ledger conservation** | Always | No new grant exceeds the current declared capacity or coupled budgets. Unknown is never zero. No blind replay. These are safety properties. A durable decision by the deadline holds only while the ledger store is available; after an outage, the overdue outcome is recorded as overdue, never backdated (§14 B3). |
| **Enforced** | The resource is `exclusive` or `partitioned`, *and* every claim's bound is an **enforced limit** (a mechanism outside the ledger constrains use) | Physical capacity is protected, within declared uncertainty and delay bounds. A qualified estimate never earns this level. |
| **Validated** | The bounds are conservative and qualified for this input range and version, but they are not enforced. Outside use is bounded. | Physical protection while the declared assumptions hold. The system watches for violations. |
| **Advisory** | Estimates, unknown bounds, `shared` resources without an outside bound, or opaque engines | Best-effort admission. The decision says "advisory". Policy decides whether a class may run at this level. |

- Every decision states its level.
- The broker does not claim physical safety from serialization or from atomic accounting alone.
- Wrong declarations can overload a machine. When evidence shows a violation, the result is drift, quarantine and requalification. It is never silent relearning.
- An out-of-memory or quota rejection is *censored* evidence that the bound was inadequate in that context. It is not an exact measurement.

**Delays are part of the guarantee.** Each observer declares a sampling interval and validity horizon. Each action declares how long its effect takes to become visible. Each resource may declare a bound on how fast outside use can grow.
- Admission pacing and headroom must agree with these bounds.
- Where no bound exists, the level drops to advisory.
- After an action, the system waits a settling period before trusting new samples.

## 7. What lives where

| Layer | Contains | Changes when |
|---|---|---|
| Core | ledger transactions, balance equations, the accounting kinds, the operation state machine, the holder recovery states, the policy language and checker, the bounded planner, decisions | a deliberate, reviewed semantic change (§2) |
| Adapters | engine protocols: invocation, observation, actions, job identity, recovery queries, fencing; one package may implement several interfaces | an engine protocol changes |
| Engine facts (data) | resources consumed, claim expressions, phase graph, residency domains, action effect schemas and their verification status | an engine is added or upgraded |
| Inventory (data) | resource ids, incarnations, units, accounting kinds, coupling, authority mode, observer bindings, delay bounds | hardware, accounts or providers change |
| Owner policy (data) | classes, resolution, compatibility, deadlines, order, reserves, release permissions, gates, guarantee floors, service objectives | the owner changes their mind |

**The purity of the core is enforced by behavior, not words.** The gate requires all of the following:
- module dependency rules: core modules import no adapter or engine code
- metamorphic tests: rename every label and id, permute dimensions, add an unrelated dimension, and change units with valid conversion; every decision must stay the same
- fixtures with invented categories, units, classes and actions

A forbidden-word list stays only as a secondary lint.

## 8. Conformance: behavioral traces, not deserialization

Scope: this suite proves **arbitration**. Dispatch is a separate seam, owned by ENGINE_CONTRACT: canonical operations, offering kinds, and the loopback rules for declarative executors. A new category also needs a dispatch path. Work on a generic, versioned operation envelope is tracked separately (§12). The suite does not quietly widen the current trust rules for paid, remote or owner-defined engines.

Each case is a **trace** with these events, written in data plus a fake adapter:
- duplicate events
- stale and reordered samples
- phase growth
- cancellation after a request was sent
- a crash before or after dispatch
- a restart
- a partition
- a window reset
- outside races

1. Smell-to-molecule model on a device: an invented category, invented units, amendable growth.
2. A remote API with a token window: two half-window calls; the third is refused until the window refills; a late header from the old window adds no credit.
3. A batch API with a daily money balance: reserve the upper-bound cost, settle the actual cost; completion refunds nothing; a day rollover keeps unsettled charges.
4. A CPU-only model: a host-memory stock plus execution slots; no device resource exists.
5. A model split across two devices: a paired atomic grant, phase peaks on both, evidence from both devices.
6. A physical device with a consumable: a slot of capacity 1 plus a material balance; start, cancel and safe-idle evidence; the irreversible action returns "not covered" when authorization is missing.
7. An opaque engine: a declared envelope held for the life of the instance, serialized requests, "manual recovery only"; disconnect, restart and unreachable all charge the holding until reconciliation.
8. An engine whose release leaves a floor: a residual in the effect schema; outside growth hides the release, so no settlement happens from aggregate data.
9. Streaming speech with a first output in 300 ms: milestone deadlines from one budget; prepared residency; "started" is not "answered".
10. An engine whose child processes leave its control boundary: incomplete attribution; outside or unresolved holdings; advisory level unless enforcement exists.
11. An engine on another machine on the LAN: host-qualified ids, generation and sequence, partition, then reconcile on reconnect; no paths on the local machine.
12. Demand known only after start: a bootstrap grant, then amend at a safe point; a refused extra amount keeps what is held; a wait-for cycle is found.
13. An engine shared with clients that bypass Azizam: `shared` authority mode; a release needs an engine-wide quiesce acknowledged by the adapter, or it is unavailable.
14. Aliases and nested partitions: capacity is never multiplied; replacing an incarnation keeps the old holdings.
15. Nontransitive compatibility, A~B and B~C but not A~C: concurrent A and C are refused.
16. Two principals, one of them splitting requests, a cold offering and an expensive recurring job, over a long trace: owner service objectives are measured; refusing everyone fails the objective.
17. Two gateway processes contending for one resource; a stale executor tries to act after failover.

## 9. Fit with today's code

The red-team inventory (§13, conformance report) is authoritative. In summary:
- `resource_claims.py`:
  - fixed `QueueClass`, fixed GPU/VRAM/CPU/RAM/disk fields, the lock and queue/profile maps
  - becomes vectors and opaque classes, behind converters
  - keep: unknown ≠ zero, absent ≠ empty, and unused dimensions block nothing
- `resource_admission.py` (missing from draft 5):
  - a second evaluator, with a GPU reservation branch and intent/app reservations
  - generalize it; keep "degraded is allowed, not denied"
  - keep the policy gates as gates
- `resource_decisions.py`:
  - GPU-specific reason codes and a closed list of resource classes
  - extend with ids, units, revision, evidence age, level and the caller-safe projection
  - keep: private evidence vs caller-safe reasons
- `resource_awareness.py`: an observer adapter. Keep its markers for uncertainty and staleness, and its cohort reset.
- `exclusive_pool.py`:
  - the first ledger consumer, in shadow mode first
  - keep its lock and observer order and its conservative MiB rounding
  - map pool identity to physical resource identity
- `swap_drain.py` and `engine_switch.py`:
  - transport leases and a single-selection strategy
  - they remain adapters or strategies
  - keep: same-selection concurrency, the drain fence, verified restore, fail-closed, idempotent release
- `engine_residency.py` and `engine_control.py`: adapters with fixed predicates, HTTP-only transport and local paths. Parameterize the predicates and affected sets. Keep their protections for origin, credentials and paths.
- `lifecycle_drivers.py` (missing from draft 5):
  - model-shaped swap and driver interfaces, and weak success checks such as substring matching
  - they stay specialized adapters
  - their success results are *triggers* for reconciliation, never proof
- **Coverage:** `RESOURCE_ADMISSION_COVERAGE.md` shows broad coverage of the **policy gates**. What is missing is **resource holdings** at dispatch. Two inventories are kept separately: gate coverage and holding acquisition. Each attempt acquires one holding, not one at each translation layer. Worker and nested exemptions are kept.
- **Coupling to ComfyUI and other engines** (`proxy.py` and about 57 files) is a separate decoupling workstream. The core must not depend on any of it.

## 10. Leverage analysis (Meadows, all twelve)

| Level | Choice in this design |
|---|---|
| 12. Parameters | Numbers are uncertain bounds with envelopes. Ledger conservation does not establish physical safety when declarations are wrong. The guarantee level says how much depends on them (§6). |
| 11. Buffers | Reserves, queues and margins are sized against arrival rate, service time and recovery delay. Capacity for control and recovery is reserved. |
| 10. Stock and flow structure | Stocks, window budgets and balances are separate. Coupling and authority boundaries are explicit. |
| 9. Delays | Sampling, visibility and growth bounds are declared. Pacing follows them, or the level drops to advisory. |
| 8. Balancing loops | Gates with hysteresis, quarantine with an *evidence-producing* recovery path, and operator reconciliation. Timeouts never release. |
| 7. Reinforcing loops | Warm residency wins more work; qualified offerings get more evidence; large clients take more opportunity; retries storm. Countered by: attribution, principal budgets, bounded qualification opportunity, protected progress, spread retries, censored-failure learning that only raises estimates, minimum residency against thrash. |
| 6. Information flows | Decisions carry the guarantee level. Longitudinal metrics per class and per principal: eligible demand, admitted, completed, refused, time to useful completion, unresolved age, opportunity denied, blocked time. Counterfactual explanations. |
| 5. Rules | Conservation, no blind replay, fencing, unknown ≠ zero, and an honest guarantee level are fixed. A policy can only make them stricter. |
| 4. Self-organization | Owners and engine authors extend through data and adapters, with versioned evidence, a composition checker, simulation, rollback, and templates that state their assumptions. |
| 3. Goals | Useful service within the owner's safety and allocation constraints. Truthful outcomes by deadline are an *invariant*, not the goal. Refusing everyone fails. |
| 2. Paradigm | A broker with an explicit, small algebra of resource semantics. It bounds what admission proves. |
| 1. Transcending the paradigm | Falsification criteria in §2. The external orchestration boundary in §2. Revise the abstraction rather than force a case into it. |

**What the design assumes about people.** It assumes that people who write engine facts and owners who write policy are *not* experts. Missing facts have a predictable onboarding state: advisory or qualification. False facts lead to quarantine and requalification. An upgrade invalidates the evidence that it affects. Templates and checker explanations do the expert work.

## 11. Build order

- **Slice 0 (narrowed in draft 7, per the round-2 review and Gall's law): a supported subset, no change in live behavior.**
  - **Done already:** the TLA+ model (§4.7). It is checked, and three broken variants fail as they should.
  - **Done means** all of the following pass, reported as "N of M".
  - **The contracts that cost the most to add later.** These are a few immutable records and tagged variants, not class hierarchies.
    1. Versioned identities: `ResourceRef(id, incarnation)`, opaque attempt, holding and operation ids, a schema version, and required capabilities. A semantic that is needed but unsupported fails closed.
    2. Typed quantities with an explicit state: `Known`, `Unknown` or `NotApplicable`. Units and granularity. `bound_type` kept apart from evidence.
    3. Generic demand vectors over explicit resource refs. Only one phase is implemented. The transition fields (peak, resulting holdings, transfer) are reserved now.
    4. Observations separate from liabilities: meaning, scope and incarnation, source generation and sequence, validity, uncertainty, and an attested included-use amount (§14 B2, §15).
    5. Advice separate from ownership: a pure evaluation is never a reservation.
    6. A versioned private decision envelope with the guarantee level. The legacy caller projections do not change.
    7. A small policy snapshot: opaque classes, a pairwise compatibility relation with a self rule, limits, and a revision reference. There is no policy language.
  - **One accounting kind runs:** `stock`, with full normalization for total, free and used, and the unknown cases. Windows, balances and opaque budgets are *capability tags that are rejected as unsupported* until their equations ship (§14).
  - **A pure feasibility kernel:** no IO, no database, no adapter imports.
  - **Wrappers on the old code, not replacements.**
    - `legacy_claim_view` and `legacy_capacity_view` for `ResourceClaim` and `CapacitySnapshot`.
    - A separate wrapper for `advise_resource_admission`.
    - A shadow view of the `exclusive_pool` result that wraps `snapshot_and_claim`'s output as it is, with the reserve already embedded. It does not change the lock or the observer order.
    - Differential golden tests against both evaluators. The exit criterion is no change in behavior.
  - **Tests:**
    - unknown ≠ 0, and absent ≠ empty locks
    - `gpu_required` with `vram_mb=0` stays a predicate of availability
    - legacy float CPU values go to the old path
    - paired total/free/used readings, including the 80-reserved/20-used counterexample
    - two-resource atomic feasibility
    - metamorphic renaming, permutation and an unrelated dimension
    - no release from a timeout, transport completion or a duplicate event
    - residency transfer charged once
    - A~B, B~C, not A~C
    - an unsupported kind gives an explicit "unsupported"
  - **Core import rules.**
  - **Not in slice 0:** the 17 traces stay a roadmap. Each is claimed only when its semantics ship.
- **Slice 1: ledger pilot, shadow mode first.**
  - SQLite serializable transactions, idempotent acquire and settle, durable operation intent, attempt and action ids, generation fencing, crash reconciliation, unknown stays charged.
  - The minimal policy snapshot (classes, compatibility, deadlines, freshness) moves into this slice.
  - The exclusive pool runs in shadow, comparing decisions without authority.
  - Authority switches on per resource boundary, only when every managed user of that resource goes through the ledger. Other users are declared outside use.
  - Fault injection: two processes, crash before and after dispatch, reordered samples, stale generation.
- **Later, one real need at a time.** Each addition ships with its equation, lifecycle, recovery evidence and an adversarial trace:
  - window and balance accounting
  - the policy language and checker
  - the actions and the bounded planner
  - phase graphs and the expression language
  - fairness and objectives
  - coverage, one resource boundary at a time

Each slice ends with a review by someone other than its author, and the questions in §10 are asked again.

## 12. Not in this foundation (on purpose)

- Placement: which engine runs on which device. Arbitration *validates* a chosen bundle against coupling constraints but does not choose it.
- Pausing running work. It could be an adapter action later.
- Hard isolation. An adapter can offer it as enforcement, which raises the guarantee level.
- Paid fallback. It has its own permission system.
- A generic dispatch envelope for new categories. It is a sibling workstream in ENGINE_CONTRACT. Arbitration conformance does not depend on it, and end-to-end "new category tomorrow" does.
- A distributed ledger across installations. Shared resources across installations use the `shared` or `partitioned` modes until such a ledger exists.

## 13. Review findings → fixes

| Finding (report) | Fix |
|---|---|
| Accounting for consumable and replenishing resources (all three, blocker) | §3.1 accounting kinds; §4.2; traces 2, 3, 6 |
| Provenance mistaken for a bound; safety overstated (Meadows B1, prior-art B4) | §3.4 bound_type; §6 guarantee levels; "wrong numbers fail safe" removed |
| Observation meaning and balance equation (conformance M2, prior-art B2) | §3.2; §4.2 |
| Atomic grant is not a crash-safe action (prior-art B3, conformance M4) | §4.3 operation state machine, fencing, idempotency; transport leases separate |
| Demand growth protocol (conformance B2, prior-art M1) | §3.4 phase graph, bounded or amendable growth; §4.5 |
| Authority over outside clients and aliases (Meadows M6, conformance B3, prior-art M4) | §3.1 coupling and authority mode; §6; traces 13, 14 |
| Dispatch contract limits new categories (conformance B4) | §8 scope; §12 sibling workstream |
| Opaque engine contradiction, lease without recovery (Meadows M2, conformance M1, prior-art M2) | §3.3 reservation vs authority; §4.4 recovery evidence and operator reconciliation; trace 7 |
| Goal rewards refusal (Meadows M1) | §3.6 service objectives; §10 level 3; trace 16 |
| Success to the successful, fairness (Meadows M3) | §3.6 principal budgets, qualification opportunity; attribution on every holder |
| Policy composition (Meadows M4) | §3.6 language, checker, simulation, explanations |
| Delay as a control bound (Meadows M5) | §6 delays |
| Admission is not process safety (Meadows M7, prior-art M7) | §2 limits; §3.5 effect schema, authorization evidence |
| Milestone deadlines (conformance M3) | §3.6 one budget, four milestones; trace 9 |
| Non-HTTP and remote adapter seams (conformance M5) | §3.5 opaque actions; §9 adapters parameterized; trace 11 |
| Migration with bypasses (conformance M6, prior-art M8) | §11 slice 1 shadow mode, authority per resource boundary |
| Compatibility is not a group token (prior-art M3) | §3.6 direct relation |
| Planner search bounds (prior-art M5) | §5 |
| Grades mix concerns; expression safety (prior-art M6) | §3.4 bound_type vs derivation; expression language |
| Lexical purity (all three, minor) | §7 behavioral gate |
| Coverage statement stale (conformance m1) | §9 coverage |
| Latency and failure boundary of one authority (prior-art m2) | §4.1 scoped quarantine; §4.6 retrievable decision |

## 14. Draft 7 amendments: the round-2 review and the accounting research

Sources:
- the round-2 dot review `azizam-foundation-rt-round2-20261105`: 22 rows; 9 adequate, 13 partial, none missing
- the dot research `azizam-accounting-kinds-research-20261105`: LiteLLM, OpenRouter, Portkey, Kong, Envoy, Cloudflare, the OpenAI and Anthropic headers, Stripe, AWS Budgets, Google quotas, Redis and Envoy buckets, payment authorization holds

Both are verbatim in the research notes.

**The scope promise.** No finite slice can guarantee that every future semantic is non-breaking (§2 already says so). Azizam promises compatibility for a **versioned, declared supported subset**. Anything that needs an unsupported semantic is rejected, never approximated.

**B1. A settlement has four separate effects.** These are:
- (a) execution ended
- (b) the unspent part of a reservation is released
- (c) an irreversible debit is recorded
- (d) reusable stock is released

Each settlement record has an id, a causal operation, a resource incarnation, an amount, and the effect for its accounting kind. A terminal status proves only the effects that the adapter contract says it proves. A spent debit stays whether or not the holder settles.

Late settlements refer to their original charge and its window. A rollover never erases unresolved liabilities. A correction that reduces an old window's estimate never adds credit to a new window.

Token-bucket refill saturates, so the order of corrections matters. The research gives a 100-capacity example in which a naive late refund creates 60 units of false allowance. Sliding windows and token buckets therefore stay **unsupported** until their own transition rules exist. If a window kind ships first, it is the fixed window.

**B2. Normalizing an observation.** A sample carries an included-use amount: a lower bound on the managed use that it provably contains (§15).
- For an aggregate used upper bound U, and a proven included managed lower bound V:

  outside_upper = max(0, U − V)

  Uncertainty and the growth from sample age are added exactly once.
- Full managed reservations are always charged.
- For a free reading, subtract only the managed liability that is *not yet included*.
- When inclusion cannot be proven, take the conservative path and lower the guarantee level.
- Paired free and used samples under the same assumptions must give the same decision. That is a test.
- Policy declares whether a reserve is extra headroom or a minimum allowance for outside use.

Provider headers such as remaining and reset are observations, never grants. The order in which they arrive is not their causal order. Rounded headers, or headers whose scope changes, never replace local outstanding liabilities. The OpenAI and Anthropic semantics differ, so each adapter normalizes them.

**B3. Deadlines need the store.** Conservation is a safety property. "A decision by the deadline" holds only while the store is available.
- The attempt's identity and its deadline policy persist before any external effect.
- After an outage, the outcome is recorded as overdue or unknown, never backdated.
- Remaining budgets across a reboot use the wall clock for the persisted deadline, and a monotonic clock within one process.

**M2. Holder state is three separate parts.** In place of §4.4's single list:
- **lifecycle:** open, reconciling or settled
- **authority:** held or lost
- **evidence health:** fresh, stale or unknown

Recovery evidence must cover the affected scope and the residual. A parent process exit does not prove that its children stopped. The end of an attempt does not prove that a residency is gone. Operator reconciliation is an audited *assumption*, not physical proof.

**M3. Guarantee levels.** These are already fixed in §6:
- **Enforced** needs a real enforcement mechanism.
- A decision that uses several resources gets the *weakest* level among them, with the reason for each resource.

**M4. Expiry and deadlock.** When a deadline expires:
- the attempt loses permission to wait or to start new effects
- cancellation is requested
- the liability stays until settlement

For a deadlock victim, the adapter declares one of three outcomes: abort and reconcile, continue within the original bounds, or manual recovery. Finding a deadlock does not guarantee recovery from it for engines that cannot be interrupted.

**M5. Coupling.** The first step is canonical resource identity, with each holding contributing once. Cycles and unsupported coupling are rejected. Capacity and constraint snapshots are versioned. When capacity shrinks, existing obligations stay, new grants that cannot fit are blocked, and the overcommit is reconciled. Arbitrary linear constraints are added only when a real case needs them.

**Merged concepts (fewer nouns):**
- One `Holding` record, with an owner kind of attempt or residency. Residency membership is metadata on it.
- "Outside" is a derived term with evidence, not a mutable holder.
- One `EvidenceRef` type for observations, bounds and action verification.
- One `Transition` model for both request phases and the effects of make-room actions.
- Capacity-1 stock for exclusion tokens. Compatibility stays a separate relation.
- No separately persisted "reserved without intent" state (A1).
- No "visible" boolean on holders. Inclusion comes from evidence: each reading may carry an attested amount of managed use that it already reflects (see §15).

**Concurrency findings from the research:**
- Most gateways allow an overrun at response time or under concurrency, for example OpenRouter workspace budgets, Cloudflare spend limits and Envoy usage limits.
- Only reserve-before-dispatch prevents that. A successful read of current spend is not a successful reservation write.
- Redis Lua scripts are atomic, but that does not make them durable across a failover.
- Payment authorization holds are the closest pattern: authorize, then capture, with idempotent recovery.
- A timeout, a disconnect or a cancel request never proves that compute or charging stopped.


## 15. Slice 0 as built (branch design/resource-foundation)

Slice 0 is the pure kernel in `gateway/arbitration/`. No live code calls it yet. These decisions were made while building it:

1. **Inclusion is an amount, not a watermark.** A reading carries `included_managed`: a lower bound on managed use that the reading already shows, from holder-scoped evidence. The first build used a grant-sequence watermark. That let a whole reservation ceiling count as included use, which is the trap in B2: with a total of 100, a reservation of 80, actual use of 20 and a reading of 30 used, the watermark build said 70 was free when only 10 was. The kernel now uses `included = min(managed, attested, shown used)`. A test holds this case.
2. **Partitioned and enforced gives the enforced level.** This matches §6. The first build gave only validated.
3. **Shared is always advisory in slice 0.** §6 allows validated for a shared resource with an outside bound. Slice 0 has no outside-growth bound, so it takes the conservative subset.
4. **Legacy demands are estimates.** Decisions converted from the legacy claim model are advisory. Queue limits, profile limits, float CPU values and unmapped locks stay in the legacy path only.
5. **A legacy snapshot without `observed_at` and validity stays legacy-only.** The slice 1 shadow beside the exclusive pool must supply both from its caller.
6. **Not yet used in arithmetic:** resource granularity, and a split between headroom and the minimum allowance in the reserve. Both wait for a real case.

## 16. Slice 1: the shadow ledger (done criteria)

Slice 1 adds a durable ledger beside the exclusive pool. It has no authority: the pool's lock and the legacy decision still decide every request. The ledger records what it would have decided, so we learn from real traffic before it decides anything.

The store is an adapter, not core: `gateway/arbitration/store.py` may use `sqlite3`; the kernel, types and holdings stay pure. The live wiring stays inside `gateway/exclusive_pool.py`, with the store path set like the other gateway stores (an environment variable, else `data/arbitration.db`).

Done means all of these pass, reported as "N of 11":
1. **Store.** SQLite in WAL mode; each write is one `BEGIN IMMEDIATE` transaction. Tables for operations, resource fencing generations and shadow comparisons. A schema version is checked on open.
2. **Idempotent acquire.** One transaction checks the generation, then records the operation as committed with its reservation. The same key with the same payload returns the first result; the same key with a different payload is refused.
3. **Settle by evidence.** Only `STOCK_RELEASED` evidence for the same resource and operation releases. `EXECUTION_ENDED`, a timeout or a duplicate never release. A second settle changes nothing.
4. **Fencing and takeover.** Taking over a resource raises its generation in one transaction and moves the older generation's open operations to reconciling. They stay charged. A write with a stale generation is refused.
5. **Charge.** The charge on a resource counts every operation that is not settled, whatever its state. All unsettled holdings of a resource incarnation share one unit; an acquire in another unit is refused while any is unsettled.
6. **Shadow in the pool (revised after review).** The durable holding is the pool's exclusion token, a capacity-1 stock that the gateway owns. GPU memory is not held durably: it is evaluated against a fresh observation, and memory that stays resident (a model kept loaded) is outside use, because slice 1 does not manage residency. After the legacy decision, the lease enqueues the token acquire and the comparison. On exit it enqueues one outcome. If the body returned normally, the request received the engine's terminal result for this operation, so the exit settles the token with `STOCK_RELEASED`, source `lease_completed`. If the body raised or was cancelled, the engine may still run, so the exit records `EXECUTION_ENDED` and the token stays charged as unknown until reconciliation. Each comparison also records how many token holdings were unsettled when the legacy path admitted, which is the case the shadow exists to find. The first draft settled on every exit; that made evidence from a transport event, which §4.3, B1 and M2 forbid.
7. **The shadow cannot hurt a request.** The lease never awaits the shadow: both calls are synchronous enqueues onto one owned worker, so they add no cancellation point and no wait. A bounded queue drops an admission and its exit together, never an exit alone. Any store or kernel error is logged once per kind. The lease result, its errors, cancellation and the lock order do not change. Tests inject a failing store, a slow store, a store that commits the acquire and then fails, and a cancellation during the body.
8. **Fault injection.** Two processes started together on one store file (overlap is likely, not forced; a forced-contention test is an open item); a crash after commit and before settle, then a takeover that leaves the operation charged; a stale generation; a duplicate and an out-of-order settle.
9. **A read interface.** `python -m gateway.arbitration.report` prints the agreement counts and the open operations as JSON. It opens the store read-only and never creates or migrates it.
10. **No live change.** The existing pool, mesh and resource tests pass unchanged.
11. **Traceability.** Each store test names the invariant it checks from §4.7 (A1 to A12) or §14. The store refines `Ledger.tla`: its single acquire transaction is the model's Grant and RecordIntent merged (A1); its generation is the ledger fence only, not an engine fence (A5, A12).

Not in slice 1: authority over any resource, other accounting kinds, actions, the planner.
