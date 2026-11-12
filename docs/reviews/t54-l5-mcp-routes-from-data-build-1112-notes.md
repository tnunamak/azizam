# Train 54 L5 — MCP routes from frontend data

Request: t54-l5-mcp-routes-from-data-build-1112-r0
Base: a048a58a12c41cef532ea72a2f95092402c2b10a

## Changes

- Added a pure compiler and cached loader in gateway/mcp_server.py. Frozen target
  and job-profile descriptors contain immutable scalars/tuples and JSON-encoded
  nested data; each capability response receives fresh schema/output objects.
  Unsupported metadata, duplicate targets/profiles, invalid lifecycle methods,
  missing input declarations and unclassified served OpenAI rows fail compilation.
- Speech and transcription are declared on their existing registered route rows.
  Their canonical HTTP methods, paths, intents and multipart declaration supply
  dispatch; the generic adapter does not branch on a model, engine or target name.
  All 63 enabled registered OpenAI rows are classified: 2 exposed targets and
  61 explicit not-exposed reasons.
- Speech preserves the old compact capability row, detailed schema/key order,
  optional voice lookup, model precedence, response-format default, stored-audio
  result envelope and resource links. Its schema maximum remains 4.0, preserving
  JSON bytes rather than merely numeric equality. The old speech behavior that
  accepts mode=async but executes synchronously is explicitly retained in metadata.
  The old omission of speech from intent-filtered workflow lists is also retained.
- Transcription accepts only a full owned aud_ asset identifier or a base64 audio
  data URI. Asset resolution calls deps.read_asset with the request's auth.
  Missing/foreign IDs, paths, URLs, partial IDs, wrong URI media kinds, invalid
  base64, empty data and data over the row's 32 MiB limit fail before _send.
  Encoded inline data is bounded before decoding. The existing callback returns
  complete asset bytes, so its byte limit is checked after that owner-scoped read.
  Actual audio-format validation remains with the HTTP route: ambiguous container
  magic or formats without magic do not invalidate an authoritative owned audio
  asset, and audio/mp4 data URIs preserve their declared media type.
- Transcription sends the resolved bytes as multipart field file through _send.
  Its response is JSON under the existing result envelope. Async mode is refused
  before reading or dispatching, and stream is forced false, including when an
  input asks for true. HTTP access enforcement and receipt creation remain in
  the existing route.
- Job lifecycle URLs and wrapping policy now come from data profiles. An async
  workflow's declared execution kind is carried into polling/content/cancellation,
  and responses include job_profile plus next_arguments for continuation.
  There is no job-ID-prefix classifier.
  For older callers supplying only an opaque ID, the adapter probes the finite
  declared GET poll routes with caller credentials. It probes all candidates,
  continues only on 404, stops on other HTTP errors, and refuses multiple matches.
  Cancellation does not mutate anything until identity/profile resolution succeeds.
  The selected profile is cached only in that Tools instance/authenticated call.
  IDs are quoted as one path segment. The existing continuation text is preserved.
- The capabilities hint names the OpenAI-shaped speech/transcription routes and
  GET /health at the gateway root.
- gateway/openai_media_schemas.py and registry/openai_media_schemas.yml are
  unchanged byte for byte. Removing only the new MCP metadata from the parsed
  frontend registry reproduces the complete original registry document.

## Validation

### Executed

1. Python syntax parsing of the implementation and test file: passed.
2. YAML parsing and compiled inventory: passed; 63 OpenAI rows, 2 exposed,
   61 not-exposed reasons, 2 job profiles.
3. Isolated allowed-source validation: 12 unittest methods passed with subcases.
   This ran the actual compiler, immutable descriptors, operation body builder,
   capability/result formatting, bounded file resolution and job-profile
   resolution code extracted from the scoped module. Checks include malformed
   metadata, classification gaps, an unfamiliar data-defined target, exact
   speech capability/list/result formatter bytes, model precedence, forced stream
   false, caller auth passed to asset reads, path/URL/partial-ID rejection,
   encoded-size refusal before decode, ambiguous audio containers, opaque job IDs,
   ambiguity refusing cancellation, and stopping on 401/403/500.
   The harness explicitly supplied fake core, exception and media-sniffer
   contracts. It did not exercise the unavailable workflow_call implementation,
   input validator, actual media decoder, HTTP route or full application.
4. Existing test-file bytes remain an exact prefix of the updated test file:
   passed. No existing speech tests were edited.
5. Both HTTP-schema files are byte-identical to the pinned base, and all
   pre-existing parsed frontend data is unchanged: passed.

### Added, not executed as project pytest

Appended 20 test functions, representing 37 parameterized pytest cases, in
tests/test_mcp_server.py. These include full MCP speech wire goldens, synthetic
target dispatch, transcription multipart/auth/refusal cases, compiler coverage,
and declared/ambiguous job-profile cases.

Command attempted: python -m pytest tests/test_mcp_server.py -q

Result: could not start; the scoped local runtime reports "No module named pytest".
No project cases were collected or executed. The isolated workspace also
deliberately contains only allow-listed source files, so out-of-scope gateway
imports were not fetched. Available bridge checks expose IDs but no command
definitions; no opaque broad check, paid/provider check, coding CLI or external
service was run. Generated full-application OpenAPI equality was not executed;
the existing HTTP schema source/data were left untouched.

## Retained literals that are wire/configuration contracts

- gateway/mcp_server.py:222 openai - published protocol label used to require
  exposure classification for the requested OpenAI route catalog.
- gateway/mcp_server.py:236 binary_asset / json_result - closed MCP descriptor
  response codecs, not engine or target identities.
- gateway/mcp_server.py:298 model - OpenAI request field; its established
  alias-over-explicit-over-default precedence is preserved.
- gateway/mcp_server.py:342 application/octet-stream - standard MIME fallback
  for a container whose audio subtype cannot be inferred by the generic sniffer.
- gateway/mcp_server.py:473 job_profile - MCP continuation parameter naming a
  declared lifecycle profile; it does not accept a caller-provided URL.
- gateway/mcp_server.py:887 default / ids - existing capability supplement output
  keys; lookup paths and source field names are declared in frontend data.
- gateway/mcp_server.py:917 GET /health - the public health route named in the
  explicitly requested user-facing capabilities hint.
- gateway/mcp_server.py:1007 status / target / assets - existing MCP result keys.
- gateway/mcp_server.py:1111 next_arguments - MCP continuation arguments alongside
  the preserved human-readable continuation hint.
- Existing MCP/JSON-RPC method names, error codes, schema keywords and resource-link
  fields remain protocol contracts. Speech target/model/route selection facts and
  lifecycle family/path selection facts were moved to data.

## Scope and limits

Only the six allow-listed source paths were read; changes are confined to
gateway/mcp_server.py, registry/protocol_frontends.yml, tests/test_mcp_server.py,
and this notes output. No root gateway module, budget file or reviewed-literal
allowance list was opened or changed. No provider/model calls or live service
actions were performed. The referenced prior design request was unavailable
("message not found"); this implementation follows the accepted brief and current
source, without adopting any other worker's claim.

The source patch is ready for owner review. Full repository pytest remains an
integration gate for the owner environment; isolated checks are not reported as
full-suite coverage.

