# Engine protocol descriptions, the http-v1 executor and the adapter interface

Status: implemented on `engine/w4-executor` (engine contract v2.1, section 4).
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
`speech`, `transcribe`. LiteLLM still executes chat. The chat entry serves
probes, inventory and the direct executor, and must equal what LiteLLM sends.

## The grammar (`executor: http-v1`, protocol_version 1)

A description has `id`, `executor`, `operations`, and optionally `title`,
`auth`, `errors`, `notes`. Each **operation** has a `class` (`inventory`,
`generate` or `control`) and either a `request` (with an optional `response`)
or a `job`.

**Request.** `method` (GET, POST, PUT, PATCH, DELETE); `path`, a relative
template that starts with `/` and holds `{name}` placeholders (values are
percent-encoded; no host, query, fragment, `..` or backslash is allowed);
`query`, a list of `{name, from, default}`; `headers`, constant non-credential
headers; `body`.

**Body.** `encoding: none | json | multipart`.
- `json`: `base: empty | passthrough` and `map`, a list of rules. Without a
  `map`, the base is `passthrough`; with one, it is `empty`.
- `multipart`: `parts`, each `{name, kind: field | json | file, from, required,
  default}`. A `file` part reads `/files/<name>`, a canonical asset.

**Mapping language.** Nine rules, each an object with exactly one key:

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

There are no loops, no expressions, no functions and no side effects. A map is
straight-line, so its cost is the rule count times the size of the values it
touches. A pointer cannot create a list: a numeric segment must index a list
that exists, so a `set` of a constant skeleton gives an array its shape.
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
conditions nested deeper than 3; constants over 16 KiB; and combinations the
grammar forbids (a GET with a body, a poll path without `{id}`).

**Budgets** (`budgets.py`). Checked on every call: request body 8 MiB, upload
64 MiB, buffered answer 16 MiB, saved asset 256 MiB, one event 1 MiB, one
stream 64 MiB, JSON depth 32, selected values 10 000, assets per job 64,
same-origin redirects 3, mapping time 0.5 s. The connection adds connect,
first-byte and total timeouts. A stream's total time is checked at each chunk;
a stalled stream ends at the first-byte timeout.

## The executor

```python
executor = HttpV1Executor(description, transport=..., assets=...)
await executor.invoke(conn, CanonicalRequest(op="chat", body={...}))   # Reply | StreamReply
await executor.submit(conn, request) -> JobRef
await executor.poll(conn, ref)       -> JobStatus
await executor.cancel(conn, ref)
await executor.fetch_assets(conn, ref, status) -> tuple[AssetRef, ...]
await executor.preview(conn, request) -> {method, url, headers (key masked), body}
```

- A redirect to another origin raises `RedirectRefused` before anything is sent
  to it. A same-origin redirect is followed up to the budget.
- A non-2xx answer raises `UpstreamError` with the description's advice row.
  Error text never holds a credential.
- Answers are requested uncompressed; a compressed answer is refused.
- An unmapped stream is relayed as raw bytes without a JSON parse. A mapped
  stream yields dicts.
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
