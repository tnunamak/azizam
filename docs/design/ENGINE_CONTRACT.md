# Engine and Protocol Contract

Status: decided 2026-10-19 (overnight program, on Tim's authority via the lead).
Supersedes: the "Adapter Manifest" section of docs/CORE_CONTRACTS.md, and these
parts of docs/design/ENGINE_REGISTRY_DESIGN.md: axiom 2 ("never owns
existence"), axiom 4 (file-wins target) and the five-driver lifecycle list.
Everything else in those documents stands.

Identity: docs/PRODUCT_STRATEGY.md "Identity". Extension rules:
docs/EXTENSION_ARCHITECTURE.md. Evidence (divan, local-ai-gateway/): gateways
4eccb54, local runners 8ccad98, platform lifecycle e927fa5, community setups
aa87d59, model picks 95c470a, media stacks c603498, media delivery d627bf2, and the reality audit
(~/Documents/azizam/engine-contract/reality-audit.md).

## 1. The one-sentence model

**An engine is a place that answers requests.** It has a connection (how Azizam
reaches it), offerings (what it can run) and, optionally, a process and residency
(how it comes and goes). Every engine setting belongs to exactly one of those
parts. Each part is data the owner can author in the UI. Code exists only for
generic primitives and for reviewed adapters, and the owner chooses those by id.

## 2. Engine record (schema v2)

```yaml
id: local-llm                 # immutable; display name is separate
name: Local LLM
enabled: true
provenance: {source: ui | provisioned | imported | builtin, package: null}

connection:
  address: http://127.0.0.1:8080      # root address; no operation path
  protocol: openai-compatible         # id of a protocol description (section 3)
  path_prefix: /v1                    # optional; the protocol says how it is joined
  credential: {secret_ref: null, env: null, header: Authorization, scheme: Bearer}
  headers: [{name: X-Example, value: "..."}]   # credential-like names are owner-only
  timeouts: {connect_s: 5, first_byte_s: 120, total_s: 600}
  native_passthrough: false           # privileged lane (section 5)

offerings:                            # what callers can use; one row each
  - public_name: default              # the name apps call; unique across engines
    kind: model | workflow
    upstream: qwen3.8-27b             # id the engine knows
    selector: per_request | engine_state | whole_graph   # how a request picks it (divan c603498)
    options: {voices: {list: {http: {path: /v1/audio/voices, select: /voices/*/id}}, accept_unlisted: false}}
    tasks: [{task: chat, evidence: declared | metadata | real_test}]
    inputs: {}                        # typed inputs (workflows); JSON Schema subset
    start_profile: {}                 # values handed to the process for this offering
    adjustments: []                   # transform ids (gateway/transforms.py primitives)

process:                              # optional; absent = Azizam never starts it
  driver: none | command | process
  start: [argv...]                    # command/process; argv arrays, never a shell string
  stop: [argv...]                     # command only
  status: [argv...]                   # command only; exit 0 = running
  env: {}                             # placeholders {{port}}, {{offering.start_profile.*}}
  cwd: null

probes:                               # missing probe = unknown, never ready
  alive: {http: {path: /health, expect: 200}} | {tcp: {}}
  ready: {http: {path: /v1/models, expect: 200, select: /data/0/id}}

residency:                            # what is loaded, and who owns idling
  observe: {http: {path: /v1/models, select: /data/*/id}}   # JSON pointer selector
  load:   {http: {method: POST, path: ..., body: {...}}}     # templated; optional
  unload: {http: {method: POST, path: ..., body: {...}}}     # templated; optional
  idle_owner: gateway | engine        # exactly one owner per engine
  idle_stop_s: 900                    # only when idle_owner is gateway
  load_deadline: {stall_s: 120}       # resets on progress, never a fixed total

resources:
  gpu: {devices: [0], vram_gb_estimate: 18}    # input to fair sharing
```

**Rules:**
- A record with only `connection` and `offerings` is complete. Process,
  probes and residency are additive.
- **Drivers: three, and no more** (divan e927fa5):
  - `none` connects and probes only (always-on, remote, or self-managing such
    as Ollama and LM Studio).
  - `command` runs owner-written start, stop and status argv against a manager
    Azizam doesn't own: `systemctl --user`, `launchctl`, `docker`, `sc.exe`,
    `lms`, SSH.
  - `process` makes Azizam spawn and own the engine process (PID, logs,
    restart, idle-stop) from owner-written argv. This is the macOS and Windows
    path, and the llama-swap path.
- **Readiness and idle-stop are always Azizam's,** because no OS supervisor
  has HTTP readiness and Windows has no TCP socket activation. A missing
  `ready` probe reports "unknown" and never shows green.
- **Per-offering `start_profile` replaces generated systemd drop-ins**
  (95-activation.conf). Under `command`, profile values reach the engine
  through env and argv placeholders. The existing systemd drop-in writer
  stays as a migration shim, inside the `command` driver only, until golden
  tests prove parity. Then it is deleted.
- **Security:**
  - Only the owner may set or change `process`, the credential, or a
    credential-like header.
  - `process` and `command` are off until the owner enables "Allow Azizam to
    run commands" (default off, as with Nomad's raw_exec).
  - argv arrays only; no shell.
  - Imported packages never enable process drivers without an owner review.
- **Capabilities sit on offerings, not on the engine.** Evidence levels:
  `declared` < `metadata` < `real_test`.
- **The selector kind decides how Azizam picks an offering:**
  - `per_request` sends the upstream id in each request (OpenAI-style, speaches).
  - `engine_state` switches the engine first, as with A1111 checkpoints and
    whisper.cpp `/load`. Azizam serializes those switches and validates the
    name itself, because these engines silently drop unknown names.
  - `whole_graph` sends the workflow graph (ComfyUI). Azizam owns the typed
    `inputs`, because ComfyUI publishes none.
- **Voices are options of a speech offering,** not offerings. List membership
  is advisory unless `accept_unlisted: false`.
- **Unload is verified by observing, never assumed.** ComfyUI's `/free`
  returns before memory is released, so `residency.observe` or a resource
  probe confirms it.
- **Identity:** `id` never changes. Receipts, health history and plans key on
  `id` and `public_name`, never on display names or array positions.

## 3. Protocol descriptions (how Azizam talks to an engine)

A protocol description is data (`registry/protocols/*.yml`, plus package-
provided ones). The engine record refers to it by id. It declares:

```yaml
id: openai-compatible
executor: generic-http | adapter:<id>     # see below
endpoints:                                 # operation -> request
  chat: {method: POST, path: /chat/completions, framing: sse | json}
  models: {method: GET, path: /models, select: /data/*/id}
async: none | submit-poll | event-stream   # generic job patterns
auth: [bearer, header, query, none]
mapping: {}                                # canonical <-> engine, safe mapping language
errors: {}                                 # status/body -> advice rows (data)
```

- **`executor: generic-http`** runs entirely from the description: HTTP calls,
  SSE, NDJSON or JSON framing, the submit-poll async pattern, multipart and
  binary bodies, and a safe, non-Turing-complete mapping language. Adding a
  new HTTP-shaped engine needs no code.
- **`executor: adapter:<id>`** names a reviewed adapter: in-tree for now, or a
  trusted package (section 6). It is used only for what generic-http can't
  express: stateful WebSocket sessions, graph patching, chunked
  speech-to-text merge. The in-tree ComfyUI, speech-to-text and text-to-speech
  clients become adapters behind this one interface. They stop being
  `proxy.py` branches.
- **The adapter interface is small:** `health`, `inventory`,
  `invoke(operation, canonical_request) -> response | job`, `cancel`, and
  `events`. Job state, receipts, policy and admission stay in core.
- **Seeded descriptions are bundled data rows** the owner can copy and edit:
  OpenAI-compatible, Ollama-style, A1111 sdapi, ComfyUI graph (adapter),
  OpenAI audio, and custom-path speech.

## 4. Inbound frontends (how callers talk to Azizam)

`registry/protocol_frontends.yml` becomes an executable mount table. Each row
binds method and path to a canonical intent and a translator id.

- **The translator** is `identity` (the OpenAI canonical shape), a declarative
  mapping (data, the same language as section 3), or a reviewed translator
  (in-tree: Anthropic, Ollama, A1111, MCP, Realtime; or a trusted package).
- **A new vendor SDK** is a frontend row plus, if needed, a mapping. When its
  shape is OpenAI-like, no code at all.
- **Routes mount from the table at boot and on reload.** Method and path
  together are the key (the audit found path-only collisions).

## 5. Native pass-through

This is per engine: `connection.native_passthrough: true`. It exposes the
engine's own API at `/native/{engine_id}/...`, untouched. Azizam adds only
authentication, access plans and receipts. It is disabled by default, scoped
by plan, and never used for remote providers. The cost is no translation; the
benefit is no feature loss.

## 6. Plugins (the escape hatch)

These are docs/EXTENSION_ARCHITECTURE.md packages.
- Data objects (engines, protocols, frontends, offerings, mappings) enable live
  after validation.
- Code objects (adapters, translators) run only after an explicit owner trust
  decision. They run as a sidecar process speaking a small JSON protocol over
  stdio or local HTTP, with declared network and filesystem permissions.
- In-tree adapters implement the same interface. "Builtin" is provenance, not
  a privilege.

## 7. Source of truth

The DB/UI registry is authoritative (docs/CONFIG_SOURCE_OF_TRUTH.md). YAML and
packages are seed and import. Export and restore round-trip the registry, so a
setup is portable without file-wins. Two lifecycle copies (`lifecycle` and
`config.lifecycle`) collapse into `process`, `probes` and `residency`, with a
one-time migration that refuses conflicting copies loudly.

## 8. UI: views of this contract

The engine page has five sections that mirror the record: Connection, What it
offers, How it runs (process, probes, residency), Resources, Stored record.
Add engine is the guided path into the same record: registry pick or address,
then the check, then offerings, then name.

There are no locked or dead fields. A field shows only if the engine's protocol
and driver use it, and every field that shows can be edited by an owner. The
page shows the one thing that belongs to the owner's system (the unit or
compose file named in the `command` argv) as read-only text with its path.

The "These settings can't be changed here" message goes away, because its
cause, a second hidden lifecycle path, goes away.

## 9. Tests that prove the contract

1. **A frozen oracle:** redacted registry shapes, the route graph, catalog
   surfaces and fake-upstream wire bytes for every enabled family. It is
   compared before and after each slice.
2. **A data-only engine:** a new HTTP-shaped engine fixture, added purely as
   data with a new protocol description, appears on every discovery surface
   and serves requests. No code change.
3. **A data-only frontend:** a new inbound frontend fixture mounts from the
   table without editing `proxy.py`.
4. **The process driver:** argv without a shell, readiness timeout, crash and
   restart, PID ownership, concurrent requests, idle-stop, shutdown.
5. **Migration:** every live record shape migrates. Conflicting lifecycle
   copies fail loudly. Export, then a clean DB, then restore gives an identical
   catalog and route graph.
6. **The boundary ratchet falls** only after these pass.
