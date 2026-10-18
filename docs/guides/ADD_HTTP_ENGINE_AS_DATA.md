# Add an HTTP engine as data

Use a protocol description when the engine fits the bounded `http-v1` grammar.
The fictional worked example consists of
[`prism.yml`](../../tests/fixtures/engine_protocol/prism/prism.yml) and
[`engine.json`](../../tests/fixtures/engine_protocol/prism/engine.json), with
the fake upstream in `tests/support/protocol_upstream.py`.

1. Put a description at `registry/protocols/<id>.yml` with
   `protocol_version: 1` and `executor: http-v1`, where the file name equals `id`.
2. Save an engine through the registry's raw record editor or registry import.
   Put the connection and offerings inside the service's `config` block.
3. Set `config.connection.dispatch: protocol`, its `protocol` to the
   description id, and its `address` to the engine's root, including any prefix.
4. Declare each offering's public `id`, upstream model id and tasks. Call the
   public API with the offering id, or `engine-id/offering-id` to disambiguate.
5. Test with a fake upstream before enabling the record. The proof suite runs
   through `scripts/run-hermetic-pytest.sh -q tests/test_engine_protocol_public.py`.

The example connection is:

```json
{
  "transport": "http",
  "dispatch": "protocol",
  "protocol": "prism",
  "address": "http://prism.test:9100/prefix",
  "credentials": {},
  "headers": [{"name": "X-Region", "value": "test"}],
  "timeouts": {"connect_s": 2, "first_byte_s": 10, "total_s": 30}
}
```

The runtime reads the registry on each request and reloads descriptions when
files change. Existing records without `dispatch: protocol` keep their current
execution path, including records that name `openai-compatible`.

## Operations exposed by the public API

| Public route | Operation name | Offering task |
|---|---|---|
| `POST /v1/chat/completions` | `chat` | `chat` or `chat.generate` |
| `GET /v1/models` | `models` | Inventory for enabled opted-in records |
| `POST /v1/embeddings` | `embeddings` | `embeddings` or `text.embeddings` |
| `POST /v1/images/generations` | `images` | `image` or `image.generate` |
| `POST /v1/audio/speech` | `speech` | `speech` or `speech.synthesis` |

The frontend aliases of these routes reach the same handlers. A generation
operation must have `class: generate`, and the gateway refuses unsupported
operations and tasks without falling back to the old engine client.
Other intents, including text completions and transcription, keep their existing
paths in this slice.

The description maps request `/body/model` and `/params/model` to the offering's
**upstream** id. `/params/offering` is its public id. Other request fields stay in
`/body`, and responses must map into the public OpenAI shape. Inventory maps its
list into `data`; the gateway lists only declared offerings present in that
answer. Chat supports JSON, SSE and NDJSON upstream; streams leave the gateway
as SSE with a final `[DONE]` only after a complete upstream stream.

The fictional engine changes request paths and field names. Its chat mapping
clamps temperature and translates finish-reason enums. Its stream uses a
custom SSE event shape. Embeddings use `/vectors`. Its `images` operation
submits a render ticket, polls a status word, and fetches a PNG. The fixture
also declares `images_sync` to test synchronous binary results; the tests
select it as `images` using description data only.

## Credentials, jobs and selection

Credentials are references, never values in descriptions. For example,
`credentials.inference` may contain `secret_ref: os.environ/ENGINE_KEY`,
`origin: http://engine.example:8080`, `header: X-Inference`, and `scheme: ""`.
The protocol's `auth.inference` must declare the matching auth method. Control
has a separate reference, and the gateway refuses an origin mismatch before
sending any request. Caller keys are never forwarded, and ordinary headers
cannot hold credentials.

Images may return binary bytes, mapped OpenAI `data[].b64_json`, or a declared
submit/poll/fetch job. The gateway stores assets in its existing owner-scoped
cache, signs delivery URLs, and keeps receipts and durable media job state.
It does not fetch arbitrary URLs from image JSON. The existing queued media
job API also runs these offerings. A stored upstream id prevents blind
resubmission. An interrupted submission without a saved id fails closed.
Startup retains the existing policy: uncertain running work is marked failed,
not submitted again; queued opted-in work does not depend on another engine's
availability.

For switch-mode offerings, declare `selection.switch.call`, `args`,
`observe_selected` and the time limits. The call names a `class: control`
operation. An observation may name an operation (`{op: selected}`), whose
response selects `value`, or use the contract's HTTP form:

```yaml
observations:
  selected: {http: {path: /selected}, select: /active}
  offering_ready: {http: {path: /ready}, select: /ready}
```

Both forms use the executor through the existing switch controller, which locks, drains,
changes selection, verifies, and holds a serving lease through the final byte.
A plan that cannot load models cannot change selection. A failed verification
restores or fails according to the record. Unknown observations never count
as success.
