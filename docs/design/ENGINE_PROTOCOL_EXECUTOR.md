# Engine protocol descriptions, the http-v1 executor and the adapter interface

Status: implemented on `engine/w4-executor`, reviewed fixes on `engine/w4-executor-fix` (engine contract v2.1, section 4).
Code: `gateway/engine_protocol/`. Data: `registry/protocols/`.
Tests: `tests/test_engine_protocol_*.py`, `tests/test_tts_adapter_seam.py`.

This document says what the code does. The reasons are in the engine
contract (section 4) and in divan `engine-request-shapes-fit-about-24-fixed-data-primitives-…`.

## What it is

A **protocol description** is a data file. It says how Azizam talks to one kind
of engine: which requests it sends, how it builds each body, how it reads each
answer. A new engine that speaks HTTP in a shape the grammar covers needs a
description and an engine record. It needs no code and no release.

| Part | Where |
|---|---|
| Description files | `registry/protocols/<id>.yml` (file name = `id`) |
| JSON Schema (closed: unknown keys fail) | `registry/protocols/protocol.schema.json` |
| Loader and validator | `gateway/engine_protocol/description.py` |
| Mapping language | `mapping.py`, `pointer.py` |
| Framing (SSE, NDJSON, multipart) | `framing.py` |
| Executor `http-v1` | `executor.py`, `transport.py`, `assets.py` |
| Adapter interface, registry, record reader | `adapter.py` |
| Generic adapter (description + executor) | `http_adapter.py` |
| First reviewed adapter (speech client) | `inline_tts.py` |

The seed `openai-compatible.yml` describes the operations Azizam sends to an
engine that serves the OpenAI-shaped API: `models`, `chat`, `embeddings`,
`speech`, `transcribe`. LiteLLM still executes chat. The seed stays on its existing execution path. Public execution requires the
record opt-in described below. Operator descriptions also load from
`<admin-state-directory>/protocols`, or `AZIZAM_PROTOCOLS_DIR`; user files
replace seeds by id. Validate a file with
`python -m gateway.engine_protocol.description path/to/id.yml`. An invalid
file disables only that description. The [operator guide](../guides/ADD_HTTP_ENGINE_AS_DATA.md)
includes persistent Docker storage and stored-key setup.

## Public dispatch (W6a)

Set `config.connection.dispatch: protocol` on a stored engine record, name an
`http-v1` description in `connection.protocol`, and declare `config.offerings`.
Top-level contract blocks are also read. Without that flag the runtime uses
its existing dispatch, even when the description says `http-v1`. Seeded
engines remain unchanged.

The fixed operation names served are `chat` (JSON and streams), `models`,
`embeddings`, `images` (sync binary/mapped base64 or submit/poll/fetch), and
buffered `speech` when declared. They reach the public OpenAI routes;
frontend aliases use the same handlers. The offering's upstream id becomes
`/body/model` and `/params/model`; `/params/offering` carries its public id.
`/params/request_id` and `/params/created` provide a unique gateway-generated
response id and Unix timestamp, constant across that response's stream chunks.
An unsupported task is refused before upstream I/O. Inventory lists only
configured offerings present in the mapped answer. Streaming responses are
framed as OpenAI SSE, including multiline raw events and the final `[DONE]`.
A malformed first event returns a typed 502 before SSE headers are sent.
A later broken event produces a typed error event and a failed receipt.
Mapped chat and embedding responses must match the public OpenAI shape.
Chat applies the existing extension validator and records stream, output limit
and request-shape metadata. Embeddings use the existing dimensions validation,
truncation and normalization rules.

Model discovery runs engines concurrently with 1 second per engine and a
1.5-second total network budget. Valid lists are cached for 15 seconds per engine
configuration (up to 256 entries). A failed engine is left out and reported on
Status; it cannot remove models from the engines that answered.

Executor saves require loopback addresses and refuse paid or remote markers
until this path enforces paid-provider access and spend (D24). Unknown placement
is refused without a DNS lookup. The existing provider route remains available.

Core keeps policy, admission, receipts, owner-scoped asset storage, image-job
state and replay. The queued image API uses the same dispatch. A saved upstream
job id is polled on a retry; an ambiguous submit is never blindly repeated.
Startup keeps the existing policy of failing uncertain running jobs. Queued
protocol jobs can resume independently of unrelated engines. Switch-mode
requests use W2's lock, drain, verification and final-byte lease. HTTP
observations compile into bounded executor operations; `{op: name}` can also
select the `value` field from a declared operation. No-switch permissions are
checked while the selection lock is held.

Text completions, transcription and graph clients keep their current paths.
The [worked example](../guides/ADD_HTTP_ENGINE_AS_DATA.md) uses only test data;
no production module names the fictional engine.

## The grammar (`executor: http-v1`, protocol_version 1)

A description has `id`, `executor`, `operations`, and optionally `title`,
`auth`, `errors`, `notes`. Each **operation** has a `class` (`inventory`,
`generate` or `control`) and either a `request` (with an optional `response`)
or a `job`.

**Request.** `method` (GET, POST, PUT, PATCH, DELETE); `path`, a relative
template that starts with `/` and holds `{name}` placeholders (values are
percent-encoded; no host, query, fragment, `..` or backslash is allowed in the
template. Each placeholder must be a single segment: slash, backslash, percent,
semicolon, control characters (including NUL), Unicode dot look-alikes that
NFKC-normalize to dots, lone surrogates, and NFKC-normalized `.` or `..` are refused with a
mapping error for caller-supplied values. Upstream-returned job ids and asset
handles may contain percent and semicolon; the executor encodes them as literal
path values. Other segment defenses still apply. Slash-containing
organization/model IDs belong in query or body fields);
`query`, a list of `{name, from, default}`; `headers`, constant non-credential
headers; `body`.

**Body.** `encoding: none | json | multipart`.
- `json`: `base: empty | passthrough` and `map`, a list of rules. Without a
  `map`, the base is `passthrough`; with one, it is `empty`.
- `multipart`: `parts`, each `{name, kind: field | json | file, from, required,
  default}`. A `file` part reads `/files/<name>`, a canonical asset.

**Mapping language.** Ten rules, each an object with exactly one key:

| Rule | Does |
|---|---|
| `copy {from, to}` | copy a value; an absent source is skipped |
| `rename {from, to}` | move a place inside the output |
| `set {to, value}` | write a constant |
| `default {to, value}` | write a constant if the place is absent |
| `drop {path}` | remove a place |
| `enum {from, to, table, unmapped}` | map by table; `unmapped` is `pass`, `drop` or `error` |
| `clamp {path, min, max}` | limit a number |
| `stringify {from, to}` | put compact JSON inside a string |
| `when {if, then, else}` | run rules if a condition holds; `if` is `{path, present \| equals \| in}` |
| `each {from, to, map}` | map every element of one selected list into an object |

There are no expressions, functions or side effects. `each` is the only
iteration: it maps a selected list under the shared count, depth and time
budgets. Other rules run in order. A pointer cannot create a list: a numeric
segment must index an existing list, built with `each` or a constant skeleton.
Source pointers start with `/body`, `/params` or `/files` in a request map;
`/body`, `/status`, `/headers`, `/params` or `/event` in a response map.

**Response.** `framing: json | sse | ndjson | binary | none`; `select`, named
pointers (a `*` segment walks every item of a list); `map`, rules that build the
canonical answer from `/body`; `terminal` for streams (`sentinel`, `event`, or
`path` with `equals`); `stream`, an alternative streamed framing chosen by a
condition on the request (the same 200 is JSON or SSE by `body.stream`).

**Job** (the one async pattern). `submit {request, id}`; `poll {request, status
{path, states}, error, assets, interval_s, max_wait_s}`; `fetch {request}`
(called once, or once per selected asset when the path uses `{asset}`);
`cancel {request}`. `states` maps the engine's words to `pending`, `running`,
`succeeded`, `failed` or `cancelled`. An unmapped word is an error, not a guess.

**Auth.** `auth.inference` and `auth.control`: `bearer`, `header`, `query` or
`none`. The secret comes from the engine record, never from the description. A
credential is sent only to the origin of the connection address. A credential
the protocol does not use is never sent.

**Validation.** `description.parse_description` lists every problem with its
place (`operations.chat.request.body.map[2].copy`). It refuses: unknown keys;
a key that names code (`class_name`, `script`, `expression`, `python`, `eval`
…); repeated YAML keys; credential-like header names; paths that could leave
the engine; malformed pointers; unknown rules; more than 64 rules in a map;
`when` and `each` nested deeper than 3; constants over 16 KiB; and combinations the
grammar forbids (a GET with a body, a poll path without `{id}`).

**Budgets** (`budgets.py`). Checked on every call: request body 8 MiB, upload
64 MiB, buffered answer 16 MiB, saved asset 256 MiB, one event 1 MiB, one
stream 64 MiB, JSON depth 32, selected values 10 000, assets per job 64,
same-origin redirects 3, mapping time 0.5 s, all assets of one `fetch_assets`
call 512 MiB. The connection adds connect, first-byte and total timeouts.
Depth is checked first and without recursion, on the request body and params
before any rule runs, on every JSON answer, job step and stream event that is
parsed. A deeper value is `BudgetExceeded(max_json_depth)`, never a
`RecursionError`. `mapping_s` is checked between rules and iterations (inside `when` and `each` too), so a
slow map stops while it runs. A stream's total time is checked at each chunk;
a stalled stream ends at the first-byte timeout.

### Bounded list mapping (`each`)

```yaml
- each:
    from: /body/models
    to: /data
    map:
      - copy: {from: /item/name, to: /id}
      - copy: {from: /index, to: /index}
```

`from` selects one list. Each element runs `map` into a new object, and `to`
receives the resulting list in order. `/item` and zero-based `/index` are
lexical roots inside the nested map; existing roots remain available. Nested
`each` restores the outer item and index when it finishes. A missing source
skips the rule, an empty list produces an empty list, and a non-list fails.
No filter, join, expression or other iteration is allowed. Nested `when` and
`each` share the depth limit; their rules share the compile-time count.
Iterations share `max_selected` across the whole invocation, including nested
maps. The mapping deadline is shared. JSON depth and request/response byte
budgets apply to the assembled result as it grows. The result owns its mutable
containers so later writes cannot change the input.

## The executor

```python
executor = HttpV1Executor(description, transport=..., assets=...)
await executor.invoke(conn, CanonicalRequest(op="chat", body={...}))   # Reply | StreamReply
await executor.submit(conn, request) -> JobRef
await executor.poll(conn, ref)       -> JobStatus
await executor.wait(conn, ref)       -> JobStatus     # polls every interval_s, gives up after max_wait_s
await executor.cancel(conn, ref)     # polls first; a job that already ended is left alone
await executor.fetch_assets(conn, ref, status) -> tuple[AssetRef, ...]
await executor.preview(conn, request) -> {method, url, headers (key masked), body}
```

- A redirect to another origin raises `RedirectRefused` before anything is sent
  to it. A same-origin redirect is followed up to the budget.
- A non-2xx answer raises `UpstreamError` with the description's advice row.
  Error text never holds a credential.
- Answers are requested uncompressed; a compressed answer is refused.
- `wait` polls every `interval_s` and raises `BudgetExceeded(max_wait_s)` when
  the job has not ended after `max_wait_s`; the limit also cuts a poll that
  hangs. Transport failures and HTTP 5xx are retried at the declared interval,
  bounded by that same deadline; HTTP 4xx, DNS, TLS and blocked-address failures,
  and malformed statuses are not retried. A deadline error includes the last
  scrubbed failure. The schema requires `interval_s >= 0.5`; no shipped
  description needs a lower interval.
  No poll starts at or after the deadline. `cancel` skips its request only when
  a poll positively reports succeeded, failed or cancelled. Poll failures,
  including unmapped states and 404, still lead to the cancel request with its
  own total timeout. A cancel response of 404, 409 or 410 means the job already
  ended; repeated cancellation succeeds. Caller cancellation still propagates. `wait` is an executor method, not
  part of the adapter interface: core keeps the durable job state and may poll
  on its own clock.
- All reads of one `fetch_assets` call share one total deadline and one size
  cap, and the cap holds whatever the asset store does with its `max_bytes`.
- A saved asset's type is not the engine's claim. `assets.served_media_type`
  allows image, audio and video types only (no HTML, SVG, script or text). The
  claimed type must be on that list, and the first bytes must show the same kind
  of file; the sniffed type is saved when the label and bytes differ. Bytes with
  no signature are saved only as `application/octet-stream` or `audio/pcm`, and
  markup is refused. Anything else is `MalformedResponse` and nothing is saved.
- A key sent as query auth (or any credential) is redacted from every error
  text, preview, answer header and job failure reason, in the raw, percent-encoded
  and JSON-escaped spellings. Errors leave with no chained cause that holds the
  URL. `transport.py` also keeps URL queries out of the `httpx` request log.
  Every non-empty credential is scrubbed, including one-to-three-character
  secrets; this deliberately favors secrecy even when ordinary error text is
  masked. Error bodies are scrubbed before the 64 KiB diagnostic cap, with
  bounded credential-length lookahead and trailing partial credential prefixes
  removed at the cut. The message excerpt is capped at 160 characters after
  scrubbing, without splitting UTF-8 or retaining a partial credential prefix.
  Successful response bodies are returned as the engine sent them.
- An unmapped stream is relayed as raw bytes without a JSON parse. A mapped
  stream yields dicts. SSE and NDJSON retain mutable buffers and scan offsets;
  only new bytes and the SSE delimiter overlap are scanned. SSE normalizes line
  endings only in new chunks and retains a split CR. The regression feeds a
  1 MiB event one byte at a time and bounds counted scan work linearly rather
  than relying on wall-clock speed.
- The transport (`HttpxTransport`) reuses the address checks of
  `gateway/engine_check.py`: one lookup, every address checked, only checked
  addresses dialled, link-local and cloud-settings addresses refused, the
  gateway's own ports on this computer refused.

## The adapter interface (version 1)

`health`, `inventory`, `invoke(conn, op, request)`, and for jobs `submit`,
`poll`, `events`, `cancel`, `recover`, `fetch_assets`. Results are typed
references: `Reply`, `StreamReply`, `JobRef`, `JobStatus`, `JobEvent`,
`AssetRef`, `Health`, `Inventory` (`types.py`). Core owns the durable job state
(idempotency keys, retry boundaries, event order, asset lifetime, receipts); an
adapter keeps nothing core cannot rebuild from a `JobRef`. A part an adapter
does not implement raises `UnsupportedByAdapter`.

`AdapterRegistry.adapter_for(protocol_id)` returns a `HttpV1Adapter` for an
`http-v1` description. For `executor: adapter:<id>` it needs a registered,
reviewed factory; an unregistered id or a different interface version is refused
by name. `connection_from_record` reads the `connection` block of an engine
record (contract section 2) into a `Connection`, looking secrets up by
reference.

### The first adapter: the speech client

`inline_tts.py` wraps the call the speech dispatch always made. It changes where
the request leaves the gateway, not what it sends: same URL, same headers (the
caller prepares them), same body bytes, same timeouts, same failure texts.
`proxy.py` still decides the engine, voice, headers and audio conversion.
Operations: `speech.synthesize` (buffered) and `speech.stream` (raw chunks).

### Mapping of the other in-tree clients (later slice)

**ComfyUI (graph patching).** Cannot be `http-v1`: the graph is patched by node
id from the workflow manifest, and progress comes over a WebSocket.
- `health`: system stats. `inventory`: node and model lists.
- `submit`: upload input assets, patch the graph, post the prompt. The `JobRef`
  holds the prompt id.
- `poll`: history and queue, mapped to `JobState`. `events`: the WebSocket
  (`executing`, `progress`, `executed`) as `JobEvent`.
- `cancel`: interrupt or delete from the queue. `recover`: look the prompt id up
  in history and queue after a restart; `None` if neither knows it.
- `fetch_assets`: read each output file into an `AssetRef`.
- Today's tests that must stay green: `tests/test_upstream_faults_comfyui*.py`,
  the workflow validation and catalog tests.

**Speech-to-text (chunked transcription).** Each upstream call is a multipart
POST that `http-v1` could describe. The chunk planner, the probe of accepted
formats, and the merge of text, timestamps and speakers across chunks are
stateful, so they stay code.
- `invoke("transcribe", …)` runs the planner and the per-chunk calls and merges.
- `health` and `inventory` can come from the description (`models`).
- Long audio becomes a job: `submit` plans and stores chunks, `poll` reports
  chunks done, `fetch_assets` returns the transcript. Core owns retries.
- Tests that must stay green: `tests/test_stt_tiers.py`,
  `tests/test_stt_chunking*.py`.

## What `http-v1` does not cover

Request signing (for example AWS SigV4); results held in the caller's own
object storage; binary-tail framings; translation between WebSocket protocols;
engines whose HTTP surface is user code. These need a reviewed adapter. A
description cannot name code, and the loader refuses any key that does.

## Overhead

Measured by `scripts/bench_engine_protocol.py` (best of 9 runs; the host was
loaded, so trust ratios, not microseconds). The "hand-written" side is a Python
function written for the same mapping.

| Case | Executor | Hand-written | Ratio |
|---|---|---|---|
| 5 rules over a passthrough copy, vs a hand-written function that copies as the executor does | ~2.2 µs | ~0.9 µs | ~2.4x |
| The same 5 rules, vs a hand-written function that shares containers | ~2.4 µs | ~0.3 µs | ~7x |
| Request map for a 20-message chat, with the JSON write that follows | ~9.6 µs | ~7.9 µs | ~1.2x |
| One SSE event, decoded, parsed and mapped | ~4.1 µs | ~1.7 µs | ~2.5x |
| One SSE event relayed unparsed | ~0.8 µs | ~0.09 µs | ~8x (under 1 µs absolute) |
| Whole `invoke` (event loop, mapping, JSON write and parse, stub transport) | ~153 µs | ~64 µs | ~2.4x |

The isolated map step is above the 3x aim only against a hand-written function
that does less work (it shares containers and skips the defensive copy). Where
the comparison includes the work every request needs anyway (JSON write, parse)
the ratio is 1.2x to 2.5x. The absolute cost is a few microseconds per request
and per streamed event.

## Fixtures

- `tests/fixtures/engine_protocol/acme/`: a made-up second engine (custom paths,
  nested answer, NDJSON stream, header key, multipart upload, submit-poll-fetch
  job) as `acme.yml` plus `engine.json`, with golden request and response bytes
  in `golden/` (`AZIZAM_UPDATE_GOLDEN=1` rewrites them for review).
  `test_adding_the_second_engine_changed_no_gateway_code` fails if any gateway
  module names it.
- `tests/fixtures/engine_protocol/negative/`: invalid grammar and static-budget
  files with the message each must produce (`expectations.json`), and
  `runtime.yml` for budgets exceeded and a cross-origin redirect.

### W6a connection and invocation measurement

The W6a hermetic run adds per-request connection and adapter construction to
the non-streaming fake-upstream comparison. It measured 137.485 µs versus
66.284 µs for the direct mapping path, 2.07x and 0.071 ms extra. The executor
alone measured 100.230 µs versus 62.142 µs, 1.61x. The request map plus JSON
write measured 9.052 µs versus 7.137 µs, 1.27x; five rules with equal defensive
copying measured 2.99x. These include the applicable work for the roughly 3x
mapping-cost target. Isolated response-only mapping was 12.63x a function that
shares containers; the SSE decode/map case was 3.10x. These are separate costs,
not evidence that every isolated primitive is below 3x. No real network,
provider inference, admission database or public-handler latency is measured
by this microbenchmark.

### W6a Round 2 bounded iteration measurement

`scripts/bench_engine_protocol.py --each --json` compares `each` with a
hand-written mapper that enforces the same iteration, depth, deadline, byte and
ownership checks, including the final JSON write. For 1, 32 and 256 scalar
items, the measured ratios were 1.11x, 1.14x and 1.20x. The 256-item case took
911 µs versus 761 µs. Mutable-item cases measured 0.91x and 1.12x; the result
below 1 is timing noise, not evidence of a speed advantage.

The two implementations share safety helpers, which account for much of their
cost. A separate comparison with a bare hand-written mapper plus JSON, without
those checks, measured 4.92x, 11.94x and 13.84x. The 0.91x to 1.20x figures
measured `each` against a mapper that shares its size and check helpers, so
they compared the code with itself and are withdrawn as evidence of the roughly
3x aim. The [Round 2 evidence](../verification/ENGINE_W6A_ROUND2.md) records
both baselines and the limits of the measurement.

### W6a Round 3 absolute `each` measurement

Round 3 measures absolute milliseconds against a bare comprehension that shares
no helpers: `scripts/bench_engine_protocol.py --each-absolute`. Embeddings are
lists of 768-float vectors; model lists are objects with a name, size and
digest. The timed applies run with the budgets raised, so the time is the
mapping's own. The last column is what the default budgets do with the same
input. The run was on a shared host at load 25 to 36 (48 is the ceiling).

| Case | Bare | `each` | Added | Ratio | Default budgets |
|---|---|---|---|---|---|
| 1,000 model items | 0.070 ms | 3.585 ms | 3.515 ms | 51x | ok |
| 10,000 model items | 0.801 ms | 37.800 ms | 36.998 ms | 47x | ok |
| 1,000 embeddings (768 floats) | 0.064 ms | 576.853 ms | 576.789 ms | about 9,000x | refused: `mapping_s` |
| 10,000 embeddings (768 floats) | 0.807 ms | 6504.803 ms | 6503.996 ms | about 8,000x | refused: `mapping_s` |

A bare comprehension that also copies each vector (`list(v)`) is 9.2 ms for
1,000 embeddings and 81.2 ms for 10,000. Against that baseline `each` is 64x
and 78x. The D23 bound, 5 ms added for 10,000 items, is not met in any case.

Where the time goes, from a profile of the 10,000-model case: exact JSON sizing
of each mapped item about 36%, the depth walk about 28%, and one deadline check
per step about 18%. Exact byte accounting of float vectors costs about 0.35 µs
per float, which no per-item change removes. Meeting the bound needs a change
to how the byte budget is computed, such as a sound bound from the source size,
which is a budget decision. Narrowing `each` to list items that are already
OpenAI-shaped (the D23 fallback) is the other option. `each` should not ship on
these numbers until the owner picks one.
