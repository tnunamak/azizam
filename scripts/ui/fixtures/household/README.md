# Household media

These fictional household assets were generated through the localhost Azizam
OpenAI-compatible API. No personal photos or operator library items were used.
`manifest.json` records the original prompt, generating model, fictional owner,
file, dimensions and duration for each item.

Images use `/v1/images/generations` with the `image` alias, then are resized to
768 pixels and encoded as WebP at quality 82. Speech uses `/v1/audio/speech`
with `tts-1`, the `alloy` voice and MP3 output. The short garden video uses
`/v1/videos` with the `video` alias; it is compressed with H.264 CRF 28 and AAC
64 kbps. Its poster is extracted from the first frame.

The household fixture copies these files into its private media cache. The
normal browser-test seed does not load them. Prompts and ownership remain in
fixture data so Library details can be checked against Activity receipts.
