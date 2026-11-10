# Media generation parameters

Image generation accepts `n` as a JSON integer from 1 through 10. Booleans,
strings, and fractional numbers return HTTP 400 with `param: "n"`.

Image edits and variations accept an omitted `size` or `"auto"` to keep the
source dimensions (or the existing fallback when dimensions cannot be read).
An explicit size must use `WIDTHxHEIGHT` with positive decimal integers and
meet the selected workflow manifest's width and height limits. Invalid sizes
return HTTP 400 with `param: "size"` before upload. Excess input images and
missing required masks also return HTTP 400 before any backend upload.

Azizam's `/openai/v1/audio/generate` extension and its `/v1/audio/generate`
alias return the workflow's native encoding. Omit `response_format` or use
`"native"`. Other values, including encoding names such as `"mp3"` and `"wav"`,
return HTTP 400 with `param: "response_format"` before submission. The speech
stream converter accepts raw PCM; generated workflow files are already encoded.
This route rejects conversion requests rather than promising an encoding it
does not produce. The response Content-Type identifies the returned encoding.

Audio `type` must be `"music"`, `"sfx"`, or `"ambient"`; omission selects
`"music"`. Duration must be finite and within the selected workflow's range.
An explicit seed must be a JSON integer from 0 through 4294967295. Only an
omitted seed selects a random seed. Invalid values return HTTP 400 with the
corresponding parameter name in the OpenAI error envelope.

## Error-parameter registry

At startup, the gateway loads `registry/openai_media_schemas.yml`, including the
`wire_error_params` groups and their `wire_error_param_endpoints` bindings.
For each bound group, every public error parameter must be a top-level request
property in at least one supported request content type of the bound endpoint.
An undeclared public property, an absent endpoint, or a binding to an absent
error-parameter group stops startup. These checks are in
`gateway/openai_media_schemas.py:175-213`; the startup load is at
`gateway/openai_media_schemas.py:243-250`.

## Workflow catalog request examples

The workflow catalog's curl, Python and JavaScript request examples follow the
selected execution route. Its field mapping determines JSON body and extension
placement; a route without a field mapping uses the synchronous request mapping.
A submit declaration supplies its method, content type and extra headers.
Extra headers are carried into all three examples. The request uses the model
named by the route or its submit declaration and adds no model when neither
names one. See `gateway/workflows_catalog.py:152-162`,
`gateway/workflows_catalog.py:323-390`, `gateway/workflows_catalog.py:401-429`
and `gateway/workflows_catalog.py:499-503`.

## JSON video-create controls

JSON requests to `POST /v1/videos` may include the optional `extensions`
object and its optional `com.azizam.video` object:

```json
{
  "prompt": "A quiet walk through the forest",
  "extensions": {
    "com.azizam.video": {
      "audio": false
    }
  }
}
```

`audio` is an optional boolean. An empty namespace object
(`"com.azizam.video": {}`) applies its default, `audio: true`; explicitly
setting `audio: false` requests silent output. Other field names inside this
namespace are rejected. Other extension namespaces remain allowed.

Omitting `extensions` or `com.azizam.video` supplies no control override.
For compatibility, the parser also treats a non-object `extensions` value
or a null `com.azizam.video` value as absent. Those legacy inputs are not
advertised by the object-shaped schema. A non-null, non-object namespace
value is rejected. These nested controls are published only for JSON video
create, not for multipart video create.

The loaded groups in `registry/extension_controls.yml` are the single source
for published JSON extension namespaces, fields, primitive types and defaults.
Each declared namespace rejects additional controls; the outer extension object
remains open. New groups need only descriptor data, not a second Python schema.
Duplicate namespaces are rejected at load time. The schema module imports the
descriptor module, which does not import the schema module.
