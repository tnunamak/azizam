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
   The owner can also install a description without a file: `PUT /admin/api/engine-protocols/<id>` with the raw YAML or
   JSON, `If-None-Match: *` to create and `If-Match: <ETag>` to replace or delete (`GET` reads one, `DELETE` removes it).
   The description is stored in `admin.db`, checked by the same parser, and every gateway worker sees it on its next
   request. A shipped id refuses writes (409), and so does deleting a description an engine names. The add-engine dialog
   then offers the operations it declares as tasks. A stored description replaces a file of the same id.
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

## Add a server from the Engines dialog with `record.dispatch`

A definition in `registry/engine_definitions.json` can carry a `record.dispatch`
block. The saved record then dispatches through the named protocol description,
and each ticked model becomes one offering built from the block's `offering`
template. A `{upstream}` string in the template is replaced by the model's name
on the server. The `observations` map is copied into the record. The
`sdapi` definition uses this: its offering switches the server's checkpoint with
the `choose` control call, reads it back with `selected`, and returns 503 with no
generation call if the read-back differs. A checkpoint that the server did not
list is never sent.

Editing a saved dispatch record: a new list of ticked models rebuilds the
offerings from the same template, so the models and the offerings never disagree.
An unticked model loses its offering. A record whose offerings were changed by
hand is not rebuilt: an edit of its models is refused with a message. A models edit
sent without `definition` and `values` takes the same path. Moving the server
re-runs the check at the new address.

The `size` mapping rule turns an OpenAI `size` string such as `1024x1024` into two
integer fields (`size: {from: /body/size, width: /width, height: /height}`). It
reads the same spellings as the image edit route, sends nothing for `auto` or an
absent size, and refuses any other value with a 400 that names `size`. The request
map runs before the first selection call, so a refused value changes nothing on the
server.

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

## This computer, or another computer on your network

1. Open Engines, then Add engine.
2. Under **Where does it run?**, choose **On another computer on my network**.
3. Enter the engine address. Azizam suggests a network from that address.
4. Check that it matches the network you trust before you save.
5. Tick **I trust this network** only if you know the address belongs to your own network.

Use plain http only on the approved network, and only without a key.
If the engine needs a key, or you cannot confirm the network, use https.
Ask whoever runs the engine for its https address.
Some mesh networks use addresses from 100.64.0.0 to 100.127.255.255.
Internet providers also use this range. Azizam cannot tell your mesh from someone else's.

Only the owner can add an engine this way. Members and app keys cannot.
Stable Diffusion's web API, speech and transcription engines offer this choice.
A speech engine on another computer must return the whole answer in one response.
A transcription or picture engine must also return one complete response.
Node graph engines such as ComfyUI stay on this computer. The dialog tells you this.
Use the existing provider route for paid-provider records.

### Technical note

By default an `http-v1` engine runs on this computer, at a loopback address such as `http://127.0.0.1:7860`.
Without remote opt-in, container names, LAN addresses and public hostnames are refused without DNS lookup.
Paid or remote records fail at save because this route has no paid-provider access or spend ledger.
You can leave an older refused record unchanged while editing other services, or disable it.
Other edits and re-enabling must pass the current checks.

A hand-written `http-v1` record can opt in to the private-network path.
Azizam suggests one network, a `/24` for IPv4 or a `/64` for IPv6, so you never type a range.
Saving sets `connection.remote_dispatch` with `enabled: true`, its network in `private_cidrs`, `transport` and `tasks`.
It also sets `cost_class` to `free-local`. The audit line names the network.

```json
"remote_dispatch": {
  "enabled": true, "version": 1,
  "transport": "private_plaintext",
  "private_cidrs": ["192.0.2.0/24"],
  "tasks": ["image"]
}
```

The transport is `tls` for `https` or `private_plaintext` for `http`.
A plain HTTP save is refused with a plain message unless all addresses the name resolves to are inside the approved network.
The record must have no key, and the owner must tick the trust box.
Fixed headers count as keys by name (`Authorization`, `X-Api-Key`, `Cookie`) or by value pattern (`Bearer …`, `sk-…`).
Use HTTPS or remove the key to use plain HTTP.
Loopback, link-local, cloud-metadata and this computer's own addresses are refused regardless of the network.
Public addresses are never approved for plain HTTP.
The private pools are `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` and `fc00::/7`.

Tailscale and other mesh networks can use shared address space `100.64.0.0/10`.
The owner must tick the trust box after seeing the shared-space warning.
Saving adds `"shared_space": true` beside `private_cidrs`; without it a policy naming shared space is refused.
The cloud metadata address within this range remains refused.

The definition lists permitted `tasks` in `record.remote.tasks`.
Only buffered speech, a synchronous picture and buffered transcription are supported.
The gateway refuses chunked or streamed answers, and refuses offerings outside the task list at save.
The `sdapi`, `openai-speech` and `openai-transcription` definitions allow `image`, `speech` and `transcription` respectively.
The speech and transcription adapters use the same guarded transport client function as `http-v1`.
It lives in `gateway/engine_protocol/bound_wire.py`; neither adapter owns a client.
Node graph engines use their own sockets and a WebSocket, so they stay on this computer.

For each exchange the gateway resolves the hostname once, checks all answers and dials only checked addresses.
It never follows redirects, refuses `transfer-encoding`, caps bytes and time, and binds credentials to their approved origin.
Path templates cannot change origin or traverse directories.
User path values reject percent and semicolon.
Engine-returned IDs may contain them and are encoded again as literal path values.
See [REMOTE_ENGINES.md](../design/REMOTE_ENGINES.md) for the design and its tests.
