# Engine and Protocol Contract (v2.1)

Status: decided 2026-10-19 (overnight program, on Tim's authority via the lead).
v2.1 folds in the red-team (~/Documents/azizam/engine-contract/redteam-codex.md,
findings F1–F16).

Supersedes: the "Adapter Manifest" section of docs/CORE_CONTRACTS.md, and these
parts of docs/design/ENGINE_REGISTRY_DESIGN.md: axiom 2 ("never owns
existence"), axiom 4 (file-wins target) and its driver list. Everything else in
those documents stands, including its routing resolution (section 3 here).

Identity: docs/PRODUCT_STRATEGY.md "Identity". Extension rules:
docs/EXTENSION_ARCHITECTURE.md. Evidence (divan, local-ai-gateway/): gateways
4eccb54, local runners 8ccad98, platform lifecycle e927fa5, community setups
aa87d59, media stacks c603498, model picks 95c470a, media delivery d627bf2, declarative adapters 5d1bed8, plugins and inbound shapes 14001b7. Code
audit: ~/Documents/azizam/engine-contract/reality-audit.md.

## 1. Model and scope

**An engine is a place that answers inference requests.** It has:
- a **connection**: how Azizam reaches it
- **offerings**: what it can run
- optionally, a **process**: how it starts and stops
- optional **observations**: what Azizam can verify about it

Every owner setting belongs to exactly one part, and each part is owner data.
Code exists only as generic primitives, and as reviewed adapters and
translators that the owner selects by id.

**Out of scope for the engine form** (F9, F12):
- the managed LiteLLM substrate, a system record with derived, locked fields,
  which is honestly shown as such
- routing objects (aliases and pools), section 3
- training and other long-lived workloads. They get a separate future
  `workload` contract with its own lifecycle, admission and receipts; they are
  not forced into `model | workflow`.

## 2. Engine record

```yaml
id: local-llm                       # immutable
name: Local LLM
enabled: true
provenance: {source: ui | provisioned | imported | builtin, package: null}

connection:
  transport: http                   # http | websocket | tcp | unix (non-http needs an adapter)
  address: http://127.0.0.1:8080    # root; the protocol joins operation paths
  protocol: openai-compatible       # protocol description id (section 4)
  credentials:                      # per operation class, each bound to the address origin (F5)
    inference: {secret_ref: null, header: Authorization, scheme: Bearer}
    control: null                   # load, unload, switch, admin; owner-only
  headers: []                       # credential-like names are owner-only, stored as secrets
  timeouts: {connect_s: 5, first_byte_s: 120, total_s: 600}

offerings:
  - id: qwen27b                     # immutable within the engine; identity = (engine_id, offering_id)
    kind: model | workflow
    upstream: qwen3.8-27b
    tasks: [{task: chat, evidence: declared | metadata | real_test}]
    inputs: {}                      # typed inputs; required for whole-graph workflows
    options: {voices: {list: ..., accept_unlisted: true}}
    selection:                      # how a request reaches this offering (F1)
      mode: request_field | switch | whole_graph
      switch: {call: <protocol op>, args: {...}, observe_selected: <observation>,
               exclusive_scope: engine, drain_timeout_s: 30, queue_wait_s: 60,
               on_failure: restore | fail}
      implicit_load: true | false | unknown
    start_profile: {}               # values the process needs to serve this offering (F3)
    adjustments: []                 # transform primitive ids

process:                            # absent = Azizam never starts or stops it
  driver: none | command | process
  start: [argv]                     # process: Azizam spawns and owns it
  stop: [argv]                      # command: an external manager owns it
  status: [argv]
  profile_apply:                    # command only: how start_profile reaches the engine (F3)
    kind: none | write_env_file_then_restart
    file: ~/.config/systemd/user/local-llm.service.d/95-azizam.conf   # owner-chosen path
    reload: [argv]
    restore: on_failure
  port: {fixed: 8080}               # process: fixed or reserved, never guessed

observations:                       # each optional; missing = unknown, never green (F11)
  process_ready:  {http: {path: /health, expect: 200}}
  inventory:      {http: {path: /v1/models, select: /data/*/id}, means: available | loaded}
  offering_ready: {http: ..., select: ...}
  selected:       {http: ..., select: ...}
  resident:       {http: ..., select: ...}
  sleep_state:    {http: ..., select: ...}

residency:
  load:   {op: <protocol op>}       # optional
  unload: {op: <protocol op>}       # optional; verified with `resident`, never assumed
  sleep:  {op: ...}                 # memory sleep, e.g. vLLM; distinct from unload
  wake:   {op: ...}
  idle_owner: gateway | engine      # exactly one owner per engine
  idle_stop_s: 900
  load_deadline: {stall_s: 120, max_s: 900, progress: <observation | none>}

native:                             # section 6; default deny
  allow: []                         # [{method, path_template, class: generate | control}]

resources:
  gpu: {devices: [0], vram_gb_estimate: 18, enforced: false}   # advisory unless enforced
```

**Rules:**
- Only `connection` plus one offering is required. Every other block is additive.
- **Drivers.** The current primitives are `none`, `command` and `process`.
  Adding a driver requires evidence (F16).
  - `command` controls an external manager. It never claims a PID or an
    environment.
  - `process` makes Azizam own the process group (a Job Object on Windows),
    with PID plus start-time identity, a fixed or reserved port, bounded
    readiness and stop, a restart budget with backoff, adoption or termination
    of orphans after a gateway crash, and size-capped, redacted logs (F14).
- **`start_profile` reaches the engine only through a declared mechanism:**
  argv or env for `process`, or `profile_apply` for `command`. The
  `write_env_file_then_restart` kind is the generic form of today's systemd
  activation drop-in: write an owner-chosen file, run the owner's reload argv,
  verify, and restore on failure. The current systemd activation code is
  retained until golden tests prove byte and rollback parity; then it is
  replaced by this kind (F3).
- **Switching is a transaction (F1).** For `selection.mode: switch`, Azizam:
  1. acquires an exclusive lease on the scope
  2. drains in-flight requests up to `drain_timeout_s`
  3. switches, then verifies with `observe_selected` (and `offering_ready` if
     declared)
  4. holds the lease until the final response byte
  5. on failure, restores the previous selection or fails

  Waiting callers get 503 with Retry-After after `queue_wait_s`. A switch that
  can't be verified reports "unknown". Unknown names are refused before
  switching.
- **Security (F5).** `command` and `process` grant host code execution. argv
  arrays do not make that safe; they only remove shell parsing. So:
  - These drivers are off until the owner enables "Allow Azizam to run
    commands on this computer".
  - Every change is owner-only, recorded and shown as a dry run first.
  - Server-side file paths in control calls must sit under owner-allowlisted
    roots.
  - No view of a record shows a command secret. A caller sees `start`, `stop`
    and `status` with each credential argument replaced by `[redacted]`, and
    `env` as names with `[redacted]` values. A save that sends the marker back
    keeps the stored value; a marker with no stored value is refused. The v2
    `process` block is an allow-list of fields, never a copy of the stored one.
  - Credentials are per operation class and bound to the address origin.
    Cross-origin redirects are refused.

## 3. Routing objects (aliases and pools) (F9, F10)

- A **public name** (alias) is its own registry object. It maps to ordered
  candidate offerings `(engine_id, offering_id)`, with a selection and fallback
  policy (the existing ENGINE_REGISTRY_DESIGN routing resolution). Pools and
  local-plus-remote fallback are first-class.
- Name collisions are checked when an alias is created.
- Receipts record the alias, the chosen `(engine_id, offering_id)`, the
  upstream id and the fallback reasons.
- The LiteLLM substrate is compiled from aliases and offerings, and the frozen
  oracle compares the compiled sidecar config byte for byte.

## 4. Protocol descriptions (how Azizam talks to an engine)

A protocol description is data: `registry/protocols/*.yml`, or one provided by
a package. Each declares `operations` (named requests: chat, models, switch,
load, unload and so on), `auth`, `errors` (advice rows) and an `executor`.

- **`executor: http-v1` is a bounded, versioned grammar (F2, F16).** It covers:
  - methods, path templates and query strings
  - JSON request bodies built by a mapping
  - response selection with JSON pointers
  - framing: JSON, SSE and NDJSON
  - multipart bodies, with named file parts from canonical assets
  - binary responses saved as assets
  - one async pattern: submit, then poll a status path, then fetch an
    artifact, with typed id, status and asset selectors

  The **mapping language** is fixed at nine rules: copy, rename, set
  (constant), default, drop, enum (table), clamp, stringify, and `when`, with
  `present`, `equals` and `in` conditions (D14). No loops, no
  arbitrary expressions, no side effects, and size, depth and time budgets.
  The claim "a new HTTP-shaped engine needs no code" applies exactly to this
  grammar. It is proven by a second-engine fixture for each pattern.
- **`executor: adapter:<id>`** is a reviewed adapter, for anything outside
  http-v1: graph patching (ComfyUI), framed or socket protocols (Wyoming),
  chunked speech-to-text merge, novel stateful streams. Divan 5d1bed8 names
  the five things measured outside the roughly 24 HTTP primitives: request
  signing (for example AWS SigV4), results held in object storage,
  binary-tail framing, WebSocket protocol translation, and engines whose HTTP
  surface is user code. Mapping cost stays within about 3x a hand-written
  mapping. JSONata and CEL-class languages (375x to 1,158x) are not used. The in-tree ComfyUI,
  STT and TTS clients become adapters behind one interface.
- **The adapter interface is versioned:** `health`, `inventory`,
  `invoke(op, canonical_request)`, and for jobs `submit`, `poll` or `events`,
  `cancel`, `recover` and `fetch_assets`.
- **Core owns the durable job state machine** (F8): idempotency keys, retry
  boundaries, event order, asset lifetime and access, and receipt
  finalization. Adapters return typed result, job, event or asset references.

## 5. Inbound frontends (how callers talk to Azizam) (F7)

A frontend descriptor is data. It declares:
- `transport` (http, or websocket)
- routes, with method and path together as the key
- the canonical intent
- the translator: `identity`, an http-v1 mapping, or a reviewed translator id
- request, response and error framing
- the stream lifecycle
- auth and handshake
- conformance fixtures

Descriptors mount at boot and on reload, with collision checks and rollback
when a reload fails.

- **Mounting.** `gateway/frontend_mount.py` builds the table. Builtin rows are
  placed first, so a package row can never take a path a builtin row holds. A
  row that collides or names an unknown translator is skipped and logged, and
  its frontend is reported as broken (`GET /admin/api/operator/frontends`, and
  the Overview attention list); the gateway keeps serving. A reload builds the
  new table before it publishes any config, then swaps it in one step at the
  old rows' position. A build or swap that fails leaves the old config and
  routes serving, and the manager's own route list is put back too. Each row is
  checked and added on its own: a row the router rejects (an unknown path
  converter, a repeated parameter name) is skipped like a colliding row, and
  never stops boot or a reload. An admin save whose hot reload fails is stored
  and answers `500 registry_saved_not_applied`.
- **A registry that cannot be applied.** A hot reload that fails for any
  reason (the stored registry does not normalize, the store cannot be read, a
  route cannot be built) keeps the old config, constants and routes, and
  raises; it never falls back to the bootstrap file's backends. Reloads run one
  at a time under one lock, so the config that stays live is the one read
  last. The Overview lists the state (`kind: registry`, `registry_apply` in the
  JSON) until a reload succeeds. Boot cannot keep an old config, so it serves
  the stored services that validate on their own (the `partial` mode), reads
  the frontends, workflows and capabilities record by record the same way, and
  leaves out and logs each service that does not validate or that shares a
  public model alias with another. When the store cannot be read at all it
  starts with no registry backends (the `empty` mode). Boot never serves the
  bootstrap file's backends in place of the registry, and it leaves the
  LiteLLM config file as it is. Both modes log the cause and are listed on the
  Overview as a problem.
- **Authentication.** The mount runs an authenticator before every operation
  that does not check the caller itself, on any path prefix, so a row at
  `/x/{provider}` gets a 401 without a key. The operations that check the
  caller themselves (the OpenAI, Anthropic and Ollama handlers, which answer
  in their own error shape, and the signed-content routes) are marked
  `self_authenticates`; a new operation is authenticated by default.
- **Who may mount what.** A package, UI or imported row may name only a
  package-mountable translator (`identity`, `anthropic`, `ollama`). The
  privileged translators (`provider-native`, `native`, `gateway`, `realtime`)
  are reserved for builtin rows, which come from the seed that ships with the
  code. A package is refused at review if a row names a privileged translator or
  opens its path with a parameter; the mount checks again, and a stored row
  that claims `builtin` for an id the seed does not hold is a package row. A
  builtin's id belongs to the seed: a stored or imported record that takes it
  (and is not the builtin) is refused on write, at package review and on
  package import, and at
  mount it is ignored and reported as broken while the builtin mounts from the
  seed. A write that adds a row the router rejects is refused.

- **The data-only promise covers HTTP frontends** whose shapes fit identity or
  http-v1 mapping. In practice that means OpenAI-structured shapes, because
  the mapping has no loops. No surveyed gateway makes a new inbound shape pure
  data (divan 14001b7).
- **Each frontend declares a `strictness`:** `exact` refuses any field it
  can't carry; `lenient` drops it and records it. Every translated response
  carries a loss report (dropped or approximated fields), both in the receipt
  and in a response header (`x-azizam-loss`).
  - A row declares `carries`, the request fields its translator handles, as
    paths: `options.temperature`, `messages[].role`. A name with no nested path
    carries everything under it. `exact` also refuses a body that is not a JSON
    object, and a nested field the translator flattens away.
  - The builtin anthropic and ollama rows declare `carries` from their
    translators' code, and stay `lenient`: clients send fields these
    translators do not map, and a refusal would break them. The header names
    them.
  - A row with a multipart body declares `body: multipart`. It is not loss
    checked, and a translated response says `unchecked` in its report.
- **Other shapes** (new handshakes, WebSocket session protocols, signed
  webhooks) need a reviewed translator. A translator package becomes
  installable without a release once the trusted loader exists (section 7).
  Until then, it is an in-tree reviewed module.

## 6. Native pass-through (F4)

Native pass-through is default deny, per engine and per method plus path
template, through `native.allow`.
- A `generate` entry is plan-scoped.
- A `control` entry (load, settings, restart, file paths) is owner-only and
  never implied by generation access.
- Paths match exactly. An entry declares the query keys it allows (`query:
  [limit]`); with none declared it takes no query string, and any other key,
  or a repeated key, is refused. A `generate` entry also refuses a value that
  looks like a file path (a slash, a backslash, `..`, `~`, a drive letter,
  `file:`, a control character). A control entry's values pass.
- Control wins over generate. A request that matches a control entry only once
  letter case is folded is ambiguous and refused (`native_ambiguous_path`). An
  engine whose paths are case-insensitive declares `native.case_insensitive:
  true` and is matched case-folded.
- `HEAD` is not an allowed method: the route does not serve it.
- Azizam forwards the request with the engine's own credential for that class:
  `connection.credentials.inference` for `generate`, `.control` for `control`,
  each bound to the origin it was attached for. A class with no credential
  sends none, and never the other class's. It never forwards the client's auth
  or cookies, and it does not use the service record's single `auth_inject`
  key or a caller's per-identity downstream secrets. (`auth_inject` rules stay
  for the older chat paths, and only the owner may add, change or remove them:
  any change to a record's rules is a credential binding change.) A credential that is not
  bound to the address it would go to is not sent, and the call is refused.
- Request and response bytes are capped. Redirects are refused. Configured
  hosts are SSRF-checked with `gateway/engine_check.py`: the address as written
  when the record is saved, the lookup result when it is saved and on every
  request, and the request dials only the addresses just checked.
- The engine's reply does not relay `Set-Cookie`, `Strict-Transport-Security`,
  CORS, `WWW-Authenticate`, `Alt-Svc` or the other headers that belong to the
  engine's origin. Azizam relays the body decoded, so it drops the engine's
  `Content-Encoding` and `Content-Length`.
- The older route `/gateway/v1/providers/{provider}/{path}` reaches the same
  engine record and keeps these rules, with two differences. It sends the
  same per-class credential and no other (never `auth_inject`), and it drops a
  body sent with a `GET` or `HEAD`. A call the record's `native.allow` names is
  classed as the list says. Any other `GET` or `HEAD` is a `generate` read: its
  query keys must be plain key names (letters, digits, `_`, `.`, `-`, `[`, `]`),
  and a value that looks like a file path is refused. Any other method is a
  `control` call, which only the owner may make. Reply headers, the request
  and reply caps and the per-request SSRF check are the same as on `/native/`.
- `native.unlisted_reads` sets what that route does with a `GET` or `HEAD` the
  list does not name: `allow` (the default) serves it as a `generate` read,
  `deny` refuses it with `native_not_allowed`. A `GET` that changes state (a
  stop, a reload, a delete by URL) cannot be told from a read, so list it as a
  `control` entry, for example `{method: GET, path: /admin/stop, class:
  control}`, and set `unlisted_reads: deny` so a state-changing `GET` you did
  not list is refused rather than served. `/native/{engine}/` refuses every
  call the list does not name, whatever this value is.
- A `generate` value is also refused when it still holds an encoded `/`, `\`
  or `.` (`%2F`, `%5C`, `%2E`) after the one decode the lane does, because the
  engine may decode it again. A `control` value passes.
- The caps are data: `native.max_request_bytes` (default 8 MiB) and
  `native.max_response_bytes` (default 256 MiB), on both routes.
- The receipt records the operation and says "cost unknown" when usage can't be
  measured.
- Remote providers have no native lane in this version. That is an intentional,
  stated fidelity limit.

## 7. Plugins (F6)

These are docs/EXTENSION_ARCHITECTURE.md packages.
- **Data objects** (engines, protocols, frontends, offerings, aliases,
  mappings) enable live after validation.
- **Code objects** (adapters, translators) are trusted host code. Until a real
  OS sandbox enforces mounts, network and process limits, declared permissions
  are reviewed, not enforced, and the UI says so.
- **A code object runs as a sidecar,** after an explicit owner trust decision.
  Sidecars win over Wasm: Kong dropped Wasm, Bifrost deprecated it, and a stdio
  hop costs about 33 µs. Unprivileged bubblewrap is the first enforcing
  sandbox to adopt, because `systemd-run --user` sandboxing was silently
  ignored on the test host (14001b7). The sidecar runs with:
  - a versioned RPC over stdio, with peer authentication
  - time, memory and output limits
  - restart on crash
  - secrets delivered as scoped references
  - hash pinning
  - atomic disable, update and uninstall, with dependency checks

  The sidecar loader is staged after the first concrete package needs it
  (F16). In-tree adapters implement the same interface, and "builtin" is
  provenance only.

## 8. Source of truth

The DB/UI registry is authoritative. YAML and packages are seed and import.

- **Export** marks references that can't move between machines: host paths,
  secret references, package hashes, process permissions.
- **Restore** reconciles missing secrets, package hashes and collisions in one
  transaction (F15).
- **The two lifecycle copies** collapse into `process`, `observations` and
  `residency`. A one-time migration refuses conflicting copies loudly.

## 9. UI (F13)

The engine page mirrors the record: Connection, What it offers, How it runs,
What Azizam can check, Resources, Stored record. Add engine is the guided path
into the same record: registry pick or address, then the check, then
offerings, then name.

- **Rule: every editable setting shown has an effect.**
  - Settings that the protocol or driver doesn't use are hidden.
  - Derived and system values are read-only, with their source and reason
    (the LiteLLM substrate stays honestly locked).
  - The page shows the effective request path, the credential destination,
    the selected offering, the process owner and the evidence behind each
    observation.
- **Test** traces exactly what will be sent.
- Files that belong to the owner's system (a unit or compose file named in
  `command`) are shown read-only with their path.
- "These settings can't be changed here" disappears, because the hidden second
  lifecycle path disappears.

## 10. Proof (F15)

1. **A frozen differential oracle,** compared before and after each slice:
   - the shapes of registry records
   - the route graph and catalog surfaces
   - fake-upstream wire bytes, including final stream bytes
   - the compiled LiteLLM config and `current`
   - systemd drop-in bytes and their rollback
   - swap deferral
   - media jobs and assets (cancel races, replay)
   - policy-denial receipts
2. **The second-engine fixtures:** an http-v1 engine added as data only, and a
   switch-mode engine that verifies its selection.
3. **A data-only frontend fixture** that mounts without editing `proxy.py`.
4. **The process state machine,** on Linux in CI, with Windows and macOS
   semantics documented and their tests marked platform-gated.
5. **Migration,** rehearsed on a copy of the live registry, plus export, then
   a clean DB, then restore.
6. **The boundary ratchet falls** only after these pass.
