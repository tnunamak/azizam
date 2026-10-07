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
