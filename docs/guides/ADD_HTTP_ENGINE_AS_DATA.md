# Add an HTTP engine as data

Use a protocol description when the engine fits the bounded `http-v1` grammar.
The [grammar reference](../design/ENGINE_PROTOCOL_EXECUTOR.md#the-grammar-executor-http-v1-protocol_version-1) lists the supported rules and budgets. The fictional worked example consists of
[`prism.yml`](../../tests/fixtures/engine_protocol/prism/prism.yml) and
[`engine.json`](../../tests/fixtures/engine_protocol/prism/engine.json), with
the fake upstream in `tests/support/protocol_upstream.py`.

1. Put your description at `data/protocols/<id>.yml`, next to `admin.db`.
   Set `protocol_version: 1` and `executor: http-v1`; the file name must equal `id`.
   `AZIZAM_PROTOCOLS_DIR` overrides the user directory. Docker keeps state at
   `/app/data`, so mount that directory or the existing `gateway-data` volume.
   Files there survive an image rebuild. Bundled descriptions in
   `registry/protocols` load first; a user file with the same id replaces its seed.
   An invalid override disables that id instead of falling back silently.
2. Validate the file before enabling it:

   ```sh
   ./venv/bin/python -m gateway.engine_protocol.description data/protocols/my-engine.yml
   ```

   In the container, use `python -m gateway.engine_protocol.description /app/data/protocols/my-engine.yml`.
   The command prints the file name and all validation problems, and exits 1 on failure.
3. Save an engine through the registry's raw record editor or registry import.
   Put the connection and offerings inside the service's `config` block.
4. Set `config.connection.dispatch: protocol`, its `protocol` to the
   description id, and its `address` to the engine's root, including any prefix.
5. Declare each offering's public `id`, upstream model id and tasks. Call the
   public API with the offering id, or `engine-id/offering-id` to disambiguate.
6. Test with a fake upstream before enabling the record. The proof suite runs
   through `scripts/run-hermetic-pytest.sh -q tests/test_engine_protocol_public.py`.

The example connection is:

```json
{
  "transport": "http",
  "dispatch": "protocol",
  "protocol": "prism",
  "address": "http://127.0.0.1:9100/prefix",
  "credentials": {},
  "headers": [{"name": "X-Region", "value": "test"}],
  "timeouts": {"connect_s": 2, "first_byte_s": 10, "total_s": 30}
}
```

The runtime reads the registry on each request and reloads descriptions when
files change. One invalid file disables only its description; Status and the
admin overview name the file without exposing a server path. Model discovery
calls engines concurrently, allows 1 second per engine and 1.5 seconds for the
network phase, and caches successful lists for 15 seconds. Failed engines are
left out and get a Status issue. The cache is limited to 256 engine configurations.
Changing an engine's address, credentials, offerings or description invalidates
its cache entry. Existing records without `dispatch: protocol` keep their current
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
`origin: http://engine.example:8080`, `header: X-Inference`, with `scheme` omitted for a raw key header.
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

## Worked example: Ollama native HTTP

The bundled [`ollama-native.yml`](../../registry/protocols/ollama-native.yml)
calls `/api/chat`, `/api/tags` and `/api/embed`. It maps NDJSON into OpenAI SSE,
renames model-list item `name` to `id`, and wraps each embedding array in an
OpenAI embedding object. The last two mappings use the bounded `each` rule.
No engine-specific Python is involved.

Import this service record, changing the address and upstream names to models
already available in your engine:

```json
{
  "id": "native-local",
  "name": "Local native HTTP",
  "enabled": true,
  "config": {
    "connection": {
      "transport": "http", "dispatch": "protocol",
      "protocol": "ollama-native", "address": "http://127.0.0.1:11434",
      "credentials": {}
    },
    "offerings": [
      {"id": "local-chat", "kind": "model", "upstream": "llama3",
       "tasks": [{"task": "chat"}], "selection": {"mode": "request_field"}},
      {"id": "local-embed", "kind": "model", "upstream": "nomic-embed-text",
       "tasks": [{"task": "embeddings"}], "selection": {"mode": "request_field"}}
    ]
  }
}
```

Call `/v1/chat/completions` with model `local-chat` and a text `messages` array;
set `stream: true` for SSE. Call `/v1/embeddings` with model `local-embed` and
`input`. `/v1/models` lists the declared offerings that the engine reports.
These are calls to the gateway with the caller's gateway key. The gateway
replaces public model names with upstream names before calling the engine.

This example covers text chat and float embeddings. It omits chat token usage
because the grammar cannot add the two native token counters; embeddings use
the native prompt count for both usage fields. Chat ids and timestamps come
from gateway request metadata. Model creation time is 0 (unknown). Tool calls,
non-text messages and base64 embeddings need additional mappings or a reviewed
adapter. Use native pass-through when you need the full native API.

## Attach an encrypted stored key

1. As the owner, add or edit the engine in the Engines page. Enter the engine
   key in **API key** and save. The key is encrypted in the state directory;
   the page subsequently shows only that a key is configured.
2. From the owner-only configuration export, find this service's
   `config.api_key_secret_id` and `credential_origin`. Copy the reference id,
   not the key value, into `config.connection.credentials.inference`:

   ```yaml
   inference:
     secret_ref: <the saved api_key_secret_id>
     origin: http://127.0.0.1:8080
     header: Authorization
     scheme: Bearer
   ```

3. The description must declare `auth.inference: {kind: bearer}` (or the
   matching named header method). For a keyless seed, copy its description to
   your user directory before changing auth. Save the reference through the
   raw registry editor or import. The reference's approved origin must match
   the connection address, including scheme and port.
4. Make a chat call. The engine receives its stored key, while the caller's
   gateway key remains at the gateway. A missing required inference key returns
   `503 engine_credential_unavailable`. A missing unused control key does not
   block chat. Add a separate control reference only for operations that need it.

An environment reference such as `os.environ/ENGINE_KEY` uses the same
origin-bound credential block. Ordinary connection headers cannot hold keys.

## Local addresses and request safety

Executor dispatch currently accepts loopback engines only. Addresses on a LAN,
container hostnames and public hostnames have unknown placement under the
existing classifier and are refused without a DNS lookup. A record marked paid
or remote, or an address classified remote by the existing registry rules, is
refused at save because this lane has no paid-provider access or spend ledger.
Use the existing provider route for those records. At every HTTP exchange the
gateway resolves the hostname once, checks all answers, and dials only those
checked addresses. It refuses link-local and cloud metadata addresses, its own
listener ports on this computer, and cross-origin redirects; credentials stay
bound to their approved origin. Path templates cannot change the origin or
traverse directories. User path values refuse percent and semicolon; ids the
engine returned may contain them and are encoded again as literal path values.
