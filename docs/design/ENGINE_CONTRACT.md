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
  dispatch: protocol                # explicit public http-v1 execution opt-in; absent keeps the current path
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
  - **A record holds no secret** (decision D26). A header value, an environment
    value, a command argument that is a secret, the user info, query and fragment
    of a URL, a package source credential and a provider key live in the
    encrypted, origin-bound store (`gateway/secret_store.py`). A record holds an
    *entry* that points at it (`gateway/record_carriers.py` defines the carriers;
    `gateway/secret_refs.py` creates, replaces and deletes secrets and never reads
    one). One module reads a secret back, `gateway/runtime_secrets.py`, at the
    moment an engine request is sent, a process or command starts, the router
    config is written, or a package is cloned; an architecture test lists the
    modules that import it. Schema 11 moves what older records held, once and
    idempotently; a registry with nothing inline is not touched. Each reader of a
    stored URL (the driver's load, unload and verify paths, the readiness poll, the
    resource, catalog, placement and context-window probes, the process-ready
    observation) puts the stored query and user info back when it sends the
    request; a part that cannot be read stops the request, and no reader falls back to
    a default path. Every runtime lookup that fails does so as `SecretUnavailable`
    (a missing row, another origin, damaged bytes, a malformed entry), never as a
    `KeyError`.
  - **Where a key goes** (owner-only). A record that holds a credential (a stored
    key, an environment variable name, a driver's admin or API key) sends it only to
    the origin its owner bound it to (`config.credential_origin`, and
    `config.driver_credential_origin` for a lifecycle driver). The fingerprint that
    decides whether a save moves a key holds the origin of every address a
    credential path dials: `base_url`, `api_base` and `default_base_url` each on its own, the native
    URL, the address the form stores, and the host and port (with scheme) the
    lifecycle driver dials. The driver takes its address from the same function
    (`service_registry.lifecycle_dial_url`), so what it dials is always covered, and it sends nothing,
    and logs that, when that origin is not the bound one. Taking a credential
    reference away (`api_key_secret_id`, `secret_refs`, a driver key) is an owner
    change as well. Admin schema 12 binds the driver keys of records that exist.
  - **Where the credentials of a URL go** (owner-only, decision D39). The credentials
    stored with a URL field (`<field>_entries`) are bound to the field's effective origin:
    its own origin when it is an absolute http(s) URL, the origin of the base it is joined to
    when it is a path (one leading slash). Case and the default port do not matter
    (`HTTP://H` is `http://h:80`). A field with no such origin (a protocol-relative `//host/path`, a
    text with no leading slash, a scheme that is not http(s), a host with a percent sign or a
    backslash) cannot hold credentials: a write is refused, and a stored one is never put back.
    Each reader passes the base it dials and its own join; the credentials are put back only
    when the address it dials has the stored origin, and otherwise nothing secret is sent, the
    request is refused with a fixed text and the refusal is logged without a value. The
    fingerprint holds, for every URL field that has credentials, the origin it is bound to (or its own
    text when it has none), so an administrator's change of the field, or of the base it joins to, is
    owner-only (403) on every route. The owner may move such a field; its credentials stay bound
    to the old origin until the owner sets them again. Admin schema 13 binds the credentials of
    records that exist. A host with user info (`user:pw@host` in `config.host`) is refused at a write;
    a stored one has its user info kept apart and not sent. Every log line a lifecycle driver prints
    passes through `runtime_secrets.scrub`, which removes the secret values this process has resolved (as
    stored and URL-encoded) and cuts the line. An environment value that was a mapping or a list when a
    migration stored it is marked `structured` and a process with it is not started, with a message
    that names the variable; setting the value again as text clears the mark.
  - **What a read shows** (decision D28). Every management response is built from
    a registered projection type (`gateway/projection_types.py`): each field is
    classified once as public data, a reference to the store, a carrier of
    entries, or omitted; a field nobody named is dropped; `projection_types.verify`
    fails startup and CI when the code that builds a record writes a field no
    type classifies. A secret is never a value and never a reference id: a header
    is `{"id", "name", "configured"}`; an environment value is
    `{NAME: {"id", "configured"}}`; a command is a list of public literals
    (strings) and `{"id", "configured", "prefix"?}`; a URL field is its public form
    (no user info, no query, no path-parameter values) beside `<field>_entries`
    (`{"userinfo": {...}, "params": [{"id", "name", "configured"}], "query": [{"id", "name", "configured"}], "fragment": {...}}`;
    a URL kept whole shows `{"whole": {"id", "label", "configured"}}`);
    a stored provider key is `{"configured": bool}`. A value the type does not
    expect (text where a mapping belongs) is left out and reported, never shown.
  - **How a secret changes** (decision D27). Never by a marker, an empty string,
    `null` or a position. `POST /admin/api/operator/services/{service_id}/secret-entries`
    (owner only; `If-Match: "os-N"` is required) takes
    `{"commands": [...]}`. Each command names a `carrier` (`config.headers`,
    `process.env`, `config.env`, `process.start`, `lifecycle.driver_config.start`,
    `config.base_url`, ...) and an `operation`:
    `add` (`name`, `value`, optional `public: true` for a header or variable that is
    not a secret), `replace` (`entry_id`, `value`), `rename` (`entry_id`, `name`; headers),
    `retain` (`entry_id`; no value; it checks the id exists), `clear` (`entry_id`),
    `set` (a command: `items` in order, each `{"literal"}`, `{"retain": entry_id}`,
    `{"replace": entry_id, "secret": value, "prefix"?}` (a new value under the same id) or
    `{"secret": value, "prefix"?}`, and `clear`: every stored entry the list drops
    must be named there; a `literal` is public as declared, no flag name is read to guess otherwise), `set_credentials` (`url`; or `url`, `whole: true` and a `label` to keep a
    URL whose secret sits in its path as one secret, shown by its label) and `clear_credentials` (a URL
    field). Ids are server-issued (`ent_...`) and stable across reorder, rename and
    replace. A value that is one of the marker texts (`[redacted]`, `<redacted>`,
    `<configured>`), empty, not text, over 8 KiB or holding NUL is refused. Unknown
    keys, unknown carriers, a repeated entry and an id that is not in that carrier
    are refused before anything is stored; a refused or stale call (`412`) stores
    nothing. The answer is the answer of `GET /admin/api/operator/service-registry`.
    Error codes: `invalid_commands`, `invalid_command`, `unknown_carrier`,
    `invalid_entry_id`, `duplicate_entry`, `unknown_entry`, `invalid_value`,
    `reserved_value`, `invalid_name`, `duplicate_name`, `unmentioned_entry`,
    `credential_header_public` (a header named like a credential cannot be `public`), `invalid_label` (a URL kept whole needs a short label that repeats no part of the URL), `carrier_unavailable`, `owner_required` (403),
    `service_not_found` (404), `generation_mismatch` (412).
  - **Writing a whole record** (the registry `PUT`, `POST` and `PATCH` of a
    service, the engine page, the left-out record `PUT`). A carrier that holds
    entries comes back exactly as the read showed it, or is left out (then it is
    kept); anything else is refused with a `400` that names `secret-entries`.
    Inline input (a header with a `value`, an environment value, a command argument
    after a credential flag, a URL with user info) becomes a new entry, and only the
    owner may send it. The engine page therefore leaves `values.headers` out unless it
    adds headers (on create every row is a new secret header; on an engine that
    already has entries, headers change only through the commands). The view shows a
    start or stop command that holds an entry as `null`. The key field keeps its
    mask for now (phase 2).
    A reference slot that has no entry id (`config.api_key_secret_id`, `secret_refs`) is read as
    `{"configured": true}` or as names; a write that sends back exactly what a read shows for the stored
    value leaves it as it is (the same rule as for a carrier), and any other marker, mapping or reserved text there
    is refused with fixed text. What a read shows for an entry (`{"id", "configured"}` with no reference) written
    where no stored entry has that id, and a `<field>_entries` part that is not an entry or an empty or gap marker,
    are refused too, as is a mapping or a list where an environment value goes (the runtime never passed one on).
    A URL is kept byte for byte, query string, `;name=value` path parameters and an empty user info (`http://@host/`) included (`?a=b+c`, `?a=`, `&&`, `%2f`);
    one that holds a backslash, a control character or an invalid port is refused on a write (it would read as
    another host to another client) and kept whole and private when a stored record has it. The URL fields are
    every one the runtime reads as an address (`record_carriers.URL_FIELD_PATHS`, a model row's `url`). Inline input
    that is a plain line or list of text, written before D26, is read by a rule: the value after `-H`, `--header`,
    `-e`, `--env` or a credential flag is a secret; a line `shlex` cannot split is stored whole as one secret with a
    public label; a header row is public only when it says `public: true` exactly and is not named like a
    credential.
  - A record the registry leaves out goes through the same projection. One that
    parses into its type is listed with its error and its record and can be edited
    or removed on the Services page (`PUT` and `DELETE` under
    `/admin/api/operator/service-registry/excluded/<section>/<id>`, body
    `{"record": ...}`); one that cannot be read is listed only with its id, its
    error, the fields that failed and the secret entries it holds. A service the
    schema 2 migration refused is such a record: its error is rebuilt from its
    blocks, and the owner keeps one block and drops `migration_refused`.
  - **Config extras are stored and never shown** (decision D34, which replaces the D28 rule that made them public).
    `config` is a typed map: the keys production code reads (`projection_types.CONFIG`, each classified as a plain
    setting, a descriptor an operator authors as data, a URL, or a carrier) and nothing else. Any other key is kept in
    storage unchanged and omitted from every management output; the object reports only how many settings it hides
    (`hidden_settings`, "2 settings hidden: not classified"). A write built from a read does not carry them, so the
    store keeps them; a write that names one replaces it. `tests/test_config_keys_are_classified.py` fails when
    production code reads a `config` key that is not classified. A secret goes in a carrier (a header, an
    environment value, a command argument, a URL credential) or in the provider key route. The same holds for the
    `provenance`, `residency`, `resources`, `process` and `lifecycle` blocks, the observations, the sections of the
    YAML file in the effective configuration, the package index and the overrides. A header row has `id`, `name`,
    `secret_ref`, and for a row declared `public: true` (exactly `true`) a `value`; any other field of a row is
    dropped. A record's id must equal the key it is stored under, and two records may not end up with one id.
  - **The router config** (decision D30). `GET /admin/api/operator/litellm-config`
    answers with a preview (a header's name with `<configured>`, an endpoint without
    credentials). The executable config is rendered only by `gateway/litellm_runtime.py`,
    from references, into a file only this user can read.
  - **One egress** (decision D31). A management handler answers only with
    `egress.projected_response`, `plain_json_response` or `safe_error_response`;
    a router built with `egress.ManagementRoute` refuses anything else, and the
    `EgressGate` middleware holds a management response until it is admitted and gives
    framework 404, 405, 422 and 500 a fixed envelope that never echoes a body or an
    exception. Startup fails for an admin route that is neither a management route
    nor in `egress.UNMANAGED_ADMIN_MODULES`, and freezes the table of management routes.
    The configuration export (`GET /admin/api/config/export`, owner only) goes through
    the same projection: public configuration and configured entries, never a value.
  - **The oracle** (decision D32). `tests/test_secret_canary_matrix.py` plants distinct
    canaries (provider-shaped and unremarkable) through the real write routes in every
    carrier and searches every GET route as three clients, error answers, the export, the
    audit log and the process logs; `tests/test_admin_secret_sweep.py` and
    `tests/test_admin_secret_sweep_writes.py` walk the route table of the exported app
    (mounts and WebSockets included) and call every route, requiring a success from each
    management GET route; `tests/test_secret_noninterference.py` replaces every secret
    with different bytes and requires identical management output;
    `tests/test_wire_compat_with_main.py` compares what an engine receives with what
    `origin/main` sent for the same records. `tests/test_review_w1_r3_cases.py` carries each
    finding of the independent review of the earlier projection (opaque header values, the host
    move, containers, neutral strings, ids that disagree with keys, error text, route
    classification, response shapes). Positive controls show the canaries reached
    the store, the runtime resolves each, and the detector can fail.
  - What phase 2 still does: MCP, CLI, logs and receipts through the same projection
    library; the engine page on the command protocol; a restorable encrypted secret
    backup; moving the remaining admin routers (settings, sign-in, accounts, categories,
    home, activity) onto `ManagementRoute`; Schemathesis and a Hypothesis state machine.
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

  The **mapping language** has ten rules: copy, rename, set
  (constant), default, drop, enum (table), clamp, stringify, `when`, and
  bounded `each` (D23 amends D14). Conditions use `present`, `equals` and `in`.
  `each` maps one selected list with nested rules; it shares size, depth, rule,
  iteration and time budgets. There is no other iteration, arbitrary
  expression or side effect.
  The claim "a new HTTP-shaped engine needs no code" applies exactly to this
  grammar. It is proven by a second-engine fixture for each pattern.

Public execution is opt-in in W6a. Until this path enforces paid-provider
access and spend, paid or remote records cannot enable it (D24). Save validation
requires a loopback address without DNS resolution; other placements use the
existing provider route. A stored service uses
`config.connection.dispatch: protocol`, a `protocol` id resolving to
`executor: http-v1`, and `config.offerings`. The runtime also accepts top-level
contract blocks. With no opt-in, even an `http-v1` description leaves the
existing path in charge. No seeded engine opts in during this slice.

The public handlers serve `chat` (JSON and streaming SSE/NDJSON mapped to
OpenAI SSE), `models`, `embeddings`, `images` (synchronous binary or mapped
base64, and submit/poll/fetch), and declared buffered `speech`. The operation
names are fixed canonical intents, not engine names. Offerings declare the
tasks they serve; generation uses their upstream id in body and parameters.
Core applies access plans and admission, finalizes receipts, and owns durable
media jobs, idempotency, cancellation settlement and asset access. Queued image
jobs use this same dispatch. Startup keeps the existing fail-closed policy for
uncertain running work rather than resubmitting it. Text completions,
transcription, graph workflows and other existing paths are not migrated here.
See [Add an HTTP engine as data](../guides/ADD_HTTP_ENGINE_AS_DATA.md).

- **`executor: adapter:<id>`** is a reviewed adapter, for anything outside
  http-v1: graph patching (ComfyUI), framed or socket protocols (Wyoming),
  chunked speech-to-text merge, novel stateful streams. Divan 5d1bed8 names
  the five things measured outside the roughly 24 HTTP primitives: request
  signing (for example AWS SigV4), results held in object storage,
  binary-tail framing, WebSocket protocol translation, and engines whose HTTP
  surface is user code. Mapping cost is measured against a bare hand-written
  comprehension with no shared helpers (round 3, `--each-absolute`): `each`
  over 1,000 model items takes 3.6 ms against 0.07 ms (51x), and over 10,000
  takes 37.8 ms against 0.80 ms (47x). Over 1,000 embeddings of 768 floats it
  takes 577 ms against 0.06 ms, and over 10,000 it takes 6.5 s. The D23 bound
  (10,000 items add at most 5 ms) is not met, and the default mapping budget
  refuses the 1,000-item embedding case. The earlier "about 3x" figure compared
  `each` with a mapper that shares its safety helpers and is withdrawn. The
  evidence and the open decision are in
  [ENGINE_PROTOCOL_EXECUTOR.md](ENGINE_PROTOCOL_EXECUTOR.md#w6a-round-3-absolute-each-measurement).
  JSONata and CEL-class languages (375x to 1,158x) are not used. The in-tree ComfyUI,
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
- **A registry that cannot be applied.** A hot reload that fails because the
  store cannot be read or a route cannot be built keeps the old config,
  constants and routes, and raises; it never falls back to the bootstrap
  file's backends. A stored registry whose records do not all validate applies
  the ones that do, as boot does (the `partial` mode). Reloads run one at a
  time under one lock, so the config that stays live is the one read last. The
  Overview lists the state (`kind: registry`, `registry_apply` in the JSON)
  until a reload succeeds, and `POST /admin/api/config/reload` applies the
  stored registry again. Boot cannot keep an old config, so it serves the
  stored services that validate on their own, and starts with no registry
  backends (the `empty` mode) when the store cannot be read at all. Boot never
  serves the bootstrap file's backends in place of the registry, and it leaves
  the LiteLLM config file as it is. Each mode logs the cause and each record
  left out, and is listed on the Overview as a problem.
- **Records that do not validate.** Every reader of the stored registry (the
  request paths, the Overview, the service registry page, workflows,
  capabilities, frontends) reads it record by record
  (`service_registry.read_registry`), so one bad record never stops the rest.
  A record is left out when it is not an object, has no usable id, has a
  block of the wrong shape (`config`, `lifecycle`, `secret_refs` or
  `provenance` not an object; `models` neither a list nor an object;
  `protocol` not a plain name; an engine with no host or address), fails its
  model checks, or shares a public model alias with another service (both
  services of the clash are left out). The gateway keeps no list of protocol
  names in code, so `protocol` is checked for its form only.
  - The service registry page and every registry write answer with the left-out
    records beside the registry (`excluded`: section, id, error, and the stored
    record with its secrets hidden). The Overview lists them
    (`registry_excluded`).
  - A write keeps each left-out record exactly as stored. It drops one only
    when the operator removes it (`DELETE` of that service) or replaces it by
    id (an edit of that service, or a full save that names it). Adding a
    service under the id of a left-out one is refused (409), and an edit that
    leaves it invalid is refused (400) with the store unchanged.
  - A request for a model of a left-out service answers 503
    `model_not_deployed`, and the message names the service and the error.
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
  key or a caller's per-identity downstream secrets. (`auth_inject` rules are
  still used by the older chat, speech-synthesis and audio paths, which read
  them through `prepare_downstream_headers`, and only the owner may add, change
  or remove them: any change to a record's rules is a credential binding change.
  The Overview warns when an enabled engine has rules and native pass-through on,
  because a call to the provider route then reaches it with only the key under
  `connection.credentials`.) A credential that is not
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
