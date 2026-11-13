# Metadata delivery fails closed

Inventory made before changing gateway code, from `git grep` over gateway/ and proxy.py,
registry/media_metadata_formats.yml, gateway/asset_types.py, registry/local_capability_rows.yml,
registry/protocol_frontends.yml, registry/workflow_request_maps.yml and manifests/workflows/.
No operator configuration, database or user media was read.

## Caller inventory

| Caller | Reachable inputs | Decision and refusal surface |
| --- | --- | --- |
| gateway/media_delivery.py deliver_bytes | Union of all rows below | full returns original; none strips; invalid mode raises ValueError |
| proxy.py speech (multiline call near 5495) | wav/audio/wav, mp3/audio/mpeg, opus/audio/ogg, aac/audio/aac, flac/audio/flac, pcm/audio/pcm | Container handlers; explicit content row for headerless PCM; 502 Could not prepare audio for delivery |
| proxy.py store_asset_bytes near 8167 | All asset suffixes and workflow outputs, including GLB | Strip before hashing/writing; failure aborts storage, workflow failure surfaced by caller |
| proxy.py image response near 8706 | .png, .jpg, .webp | Byte-selected image handler; invalid media aborts response |
| proxy.py CacheAssetPort near 11332/11334 | Stored files plus URL/upload bytes admitted by manifest accepts | Same handlers; new ingest wraps refusal as input_type_unsupported before store/upstream effects |
| proxy.py _resolve_cached_image_asset near 11378 | .png/.jpg/.jpeg/.webp/.gif | Image handlers; exception propagates to route |
| proxy.py _mcp_read_asset near 16505 | All cached assets (bounded preview separately decodes/re-encodes) | Same handlers; refusal propagates to MCP error |
| proxy.py identity result near 17245 | .png | PNG handler; exception aborts result |
| proxy.py asset content near 27150 | All cached assets | 502 Could not prepare asset for delivery |
| gateway/frontend_shapes/video_views.py near 184 | .mp4, .webm, .mov, .mkv, .avi, or unknown filename/.bin | Declared muxer + byte-family validation; 502 Could not prepare video for delivery |
| gateway/prompt_metadata_migration.py near 77 | Digest-named cache files, any suffix | Same handlers; errors recorded, migration pending and retried |

## Format inventory and decisions

Image aliases: .png/image/png; .jpg/.jpeg/.jpe/.jfif with image/jpeg or image/jpg;
.webp/image/webp; .gif/image/gif. Real bytes select PNG/JPEG/WebP/GIF regardless of alias.
Manifest accepts include image/* and exact image/png, image/jpeg, image/webp.
Unknown image types (including any later type admitted by image/*) are refused.

Container aliases: .mp4/.m4v (video/mp4), .mov (video/quicktime), .webm (video/webm),
.mkv (video/x-matroska), .avi (video/x-msvideo); .wav (audio/wav, audio/x-wav,
audio/wave, audio/vnd.wave), .flac (audio/flac, audio/x-flac), .mp3 (audio/mpeg,
audio/mp3), .ogg/.oga/.opus (audio/ogg, audio/opus), .m4a (audio/mp4, audio/x-m4a,
audio/m4a), .aac (audio/aac), .mka (audio/x-matroska).
All have handlers and required muxers. Byte families must agree with the declared muxer.
Workflow output media types are image/png, video/mp4, audio/flac, audio/mpeg, audio/wav,
model/gltf-binary, and application/json. JSON workflow outputs take the kernel's JSON
result path, not asset storage (gateway/workflow_kernel.py and workflow_projections.py).
No plain-text output reaches stripping in shipped workflows, so no text bypass is added.
Headerless speech .pcm/audio/pcm has no container metadata channel and gets a content row.
Known container signatures take priority over that row, preventing a renamed image bypass.

3D: Pixal3D, Trellis and mesh edits output model/gltf-binary; mesh edits also accept it.
Add .glb/model/gltf-binary handler. Keep BIN exactly, remove descriptive JSON fields.
Asset type aliases also include .gltf/model/gltf+json, .obj/model/obj, .stl/model/stl,
.ply/model/ply and .usdz/model/vnd.usdz+zip. None is a shipped workflow output;
these have metadata channels and are refused until a stripper exists, never content rows.
Generic assets can carry unknown suffixes/MIME through future operator workflows: refuse.

Expect it to work because all paths converge on strip_metadata, and the inventory confirms
GLB and PCM need support before closing unknown formats. Local parsers preserve payloads;
ffmpeg filter_units removes codec user data during stream copy. Synthetic decode comparisons
check the likeliest payload regression. Confidence before tests: 75%.

## Red evidence

Before production edits: 30 failed, 57 passed, 27 subtests passed.
The three correctly declared image cases already passed; mismatches reproduced leaks.

```text
E           AssertionError: MediaMetadataError not raised
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00 \x00\x00\x00\x18\x08\x02\x00\x00\x00\x141hc\x00\x00\x000eXIfMM\x00*\...xd9\x86\x1e\x90\xeb\x01\xa9\xbd\x8dy\xc4r\x03\t\xb4@\x07\xb4K?\x03\xe7\x1c7\x13R\xbf\x97\x00\x00\x00\x00IEND\xaeB`\x82'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00 \x00\x00\x00\x18\x08\x02\x00\x00\x00\x141hc\x00\x00\x000eXIfMM\x00*\...xd9\x86\x1e\x90\xeb\x01\xa9\xbd\x8dy\xc4r\x03\t\xb4@\x07\xb4K?\x03\xe7\x1c7\x13R\xbf\x97\x00\x00\x00\x00IEND\xaeB`\x82'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00 \x00\x00\x00\x18\x08\x02\x00\x00\x00\x141hc\x00\x00\x000eXIfMM\x00*\...xd9\x86\x1e\x90\xeb\x01\xa9\xbd\x8dy\xc4r\x03\t\xb4@\x07\xb4K?\x03\xe7\x1c7\x13R\xbf\x97\x00\x00\x00\x00IEND\xaeB`\x82'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00 \x00\x00\x00\x18\x08\x02\x00\x00\x00\x141hc\x00\x00\x000eXIfMM\x00*\...xd9\x86\x1e\x90\xeb\x01\xa9\xbd\x8dy\xc4r\x03\t\xb4@\x07\xb4K?\x03\xe7\x1c7\x13R\xbf\x97\x00\x00\x00\x00IEND\xaeB`\x82'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xe1\x008Exif\x00\x00MM\x00*\x00\x00\x00\x08...W\x00w\xad(l\x19\xce\xd8\x97j\xfa\xe3\x93E\x15\x85Y\xca\x94iR\xa6\xec\xa5{\xdb}-\xd7\xe6}\x8e\x07\x11Q\xea\xd9\xff\xd9'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xe1\x008Exif\x00\x00MM\x00*\x00\x00\x00\x08...W\x00w\xad(l\x19\xce\xd8\x97j\xfa\xe3\x93E\x15\x85Y\xca\x94iR\xa6\xec\xa5{\xdb}-\xd7\xe6}\x8e\x07\x11Q\xea\xd9\xff\xd9'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xe1\x008Exif\x00\x00MM\x00*\x00\x00\x00\x08...W\x00w\xad(l\x19\xce\xd8\x97j\xfa\xe3\x93E\x15\x85Y\xca\x94iR\xa6\xec\xa5{\xdb}-\xd7\xe6}\x8e\x07\x11Q\xea\xd9\xff\xd9'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xe1\x008Exif\x00\x00MM\x00*\x00\x00\x00\x08...W\x00w\xad(l\x19\xce\xd8\x97j\xfa\xe3\x93E\x15\x85Y\xca\x94iR\xa6\xec\xa5{\xdb}-\xd7\xe6}\x8e\x07\x11Q\xea\xd9\xff\xd9'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'RIFFV\x01\x00\x00WEBPVP8X\n\x00\x00\x00\x08\x00\x00\x00\x1f\x00\x00\x17\x00\x00VP8 \x00\x01\x00\x00P\x07\x00\x9d\x01...\x00*\x00\x00\x00\x08\x00\x01\x01\x10\x00\x02\x00\x00\x00\x16\x00\x00\x00\x1a\x00\x00\x00\x00prompt:PRIVATE_MARKER\x00'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'RIFFV\x01\x00\x00WEBPVP8X\n\x00\x00\x00\x08\x00\x00\x00\x1f\x00\x00\x17\x00\x00VP8 \x00\x01\x00\x00P\x07\x00\x9d\x01...\x00*\x00\x00\x00\x08\x00\x01\x01\x10\x00\x02\x00\x00\x00\x16\x00\x00\x00\x1a\x00\x00\x00\x00prompt:PRIVATE_MARKER\x00'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'RIFFV\x01\x00\x00WEBPVP8X\n\x00\x00\x00\x08\x00\x00\x00\x1f\x00\x00\x17\x00\x00VP8 \x00\x01\x00\x00P\x07\x00\x9d\x01...\x00*\x00\x00\x00\x08\x00\x01\x01\x10\x00\x02\x00\x00\x00\x16\x00\x00\x00\x1a\x00\x00\x00\x00prompt:PRIVATE_MARKER\x00'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'RIFFV\x01\x00\x00WEBPVP8X\n\x00\x00\x00\x08\x00\x00\x00\x1f\x00\x00\x17\x00\x00VP8 \x00\x01\x00\x00P\x07\x00\x9d\x01...\x00*\x00\x00\x00\x08\x00\x01\x01\x10\x00\x02\x00\x00\x00\x16\x00\x00\x00\x1a\x00\x00\x00\x00prompt:PRIVATE_MARKER\x00'
E       Failed: DID NOT RAISE MediaMetadataError
E       Failed: DID NOT RAISE MediaMetadataError
E       Failed: DID NOT RAISE MediaMetadataError
E       Failed: DID NOT RAISE MediaMetadataError
E       Failed: DID NOT RAISE MediaMetadataError
E       AssertionError: assert b'PRIVATE_MARKER' not in b'\xff\xd8\xff\xe0\x00\x10PRIVATE_MARKER\xff\xe2\x00\x10PRIVATE_MARKER\xff\xe3\x00\x10PRIVATE_MARKER\xff\xe4\x00\x10PR...W\x00w\xad(l\x19\xce\xd8\x97j\xfa\xe3\x93E\x15\x85Y\xca\x94iR\xa6\xec\xa5{\xdb}-\xd7\xe6}\x8e\x07\x11Q\xea\xd9\xff\xd9'
E       AssertionError: assert b'PRIVATE_MARKER' not in b'GIF89a\x08\x00\x08\x00\x81\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00!\xff\x0bNETSCAPE2.0\x03\x01\x02\x...\x00\x00\x00\x08\x0f\x00\x01\x08\x1cH\xb0\xa0\xc1\x83\x08\x13*L\x18\x10\x00!\xff\x0bXMP DataXMP\x0ePRIVATE_MARKER\x00;'
E       AssertionError: assert {'asset': {'v...eLength': 4}]} == {'asset': {'v...eLength': 4}]}
E         
E         Omitting 1 identical items, use -vv to show
E         Differing items:
E         {'nodes': [{'extras': {'private': 1}, 'name': 'mesh'}]} != {'nodes': [{'name': 'mesh'}]}
E         {'asset': {'version': '2.0', 'generator': 'PRIVATE_MARKER', 'copyright': 'PRIVATE_MARKER'}} != {'asset': {'version': '2.0'}}
E         Left contains 1 more item:
E         {'extras': {'secret': 1}}
E         Use -v to get more diff
E       Failed: DID NOT RAISE ValueError
E       Failed: DID NOT RAISE ValueError
E       Failed: DID NOT RAISE ValueError
E       Failed: DID NOT RAISE ValueError
E       AssertionError: assert b'SYNTHETIC_CAMERA_LOCATION' not in b'\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41\x00\x00\x00\x08free\x00\x00\x02\xebmdat\x00\x00\x02\xcb\x06\x0...0\x00\x00\x00\x00!hdlr\x00\x00\x00\x00\x00\x00\x00\x00mdirappl\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08ilst'
E       AssertionError: assert b'x265 (build' not in b'\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41\x00\x00\x00\x08free\x00\x00\x00\x18mdat\x00\x00\x00\x0c(\x01\xa...0\x00\x00\x00\x00!hdlr\x00\x00\x00\x00\x00\x00\x00\x00mdirappl\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08ilst'
E           Failed: DID NOT RAISE MediaMetadataError
E       Failed: DID NOT RAISE ValueError
E       Failed: DID NOT RAISE ValueError
FAILED tests/test_media_metadata.py::KindTests::test_unknown_refused - Assert...
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[PNG-.jpg]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[PNG-.webp]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[PNG-.gif]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[PNG-.bin]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[JPEG-.png]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[JPEG-.webp]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[JPEG-.gif]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[JPEG-.bin]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[WEBP-.png]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[WEBP-.jpg]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[WEBP-.gif]
FAILED tests/test_media_metadata.py::test_real_bytes_choose_image_handler[WEBP-.bin]
FAILED tests/test_media_metadata.py::test_unknown_bytes_refused[.bin] - Faile...
FAILED tests/test_media_metadata.py::test_unknown_bytes_refused[.png] - Faile...
FAILED tests/test_media_metadata.py::test_unknown_bytes_refused[.jpg] - Faile...
FAILED tests/test_media_metadata.py::test_unknown_bytes_refused[.webp] - Fail...
FAILED tests/test_media_metadata.py::test_unknown_bytes_refused[.gif] - Faile...
FAILED tests/test_media_metadata.py::test_jpeg_app_allowlist - AssertionError...
FAILED tests/test_media_metadata.py::test_gif_extensions_and_frames - Asserti...
FAILED tests/test_media_metadata.py::test_glb_json_and_bin_preserved - Assert...
FAILED tests/test_media_metadata.py::test_invalid_delivery_mode[None] - Faile...
FAILED tests/test_media_metadata.py::test_invalid_delivery_mode[] - Failed: D...
FAILED tests/test_media_metadata.py::test_invalid_delivery_mode[ful] - Failed...
FAILED tests/test_media_metadata.py::test_invalid_delivery_mode[provenance]
FAILED tests/test_media_metadata.py::test_codec_userdata_removed_or_codec_refused[libx264]
FAILED tests/test_media_metadata.py::test_codec_userdata_removed_or_codec_refused[libx265]
FAILED tests/test_media_metadata.py::test_codec_userdata_removed_or_codec_refused[mpeg4]
FAILED tests/test_media_metadata_formats_data.py::test_stream_handler_requires_muxer[audio]
FAILED tests/test_media_metadata_formats_data.py::test_stream_handler_requires_muxer[video]
```

## Verification findings and implementation decisions

The first changed-code run: 8 failed, 377 passed, 39 subtests passed. It exposed an
incorrect assumption in the brief: filter_units removes packet SEI but leaves HEVC
hvcC extradata unchanged. The synthetic x265 encoder string remained there. Four
single-pass filter chains (plain filter_units, Annex B first, and either with
extract_extradata=remove=1) all retained it. Stream-copy through raw HEVC or MPEG-TS
removed it; Matroska retained it. The final implementation uses a private memfd
MPEG-TS intermediate for HEVC to rebuild codec configuration from filtered units,
then the declared muxer. Both passes filter packet user data and strip tags. Probe
results must retain all stream types/codecs in order, otherwise delivery refuses.
This is a necessary deviation from the proposed single rewrite, with no re-encoding.
Transport-stream-incompatible combinations can now refuse rather than leak.

Video payload pins that previously compared compressed hashes now compare decoded
pixels; removing SEI deliberately changes the compressed hash. The shared synthetic
MP4 fixture used mpeg4, a codec the brief explicitly requires refusing: it now uses
single-thread libx264. The job-view unit test keeps its fake bytes and stubs delivery;
actual delivery is exercised by synthetic codec tests. A corrupt stored image test
now writes corruption after a valid store, since new writes reject unknown bytes.
The resolver converts cached stripping refusals to input_type_unsupported, matching
new-upload refusals. No proxy.py change is needed.

## Final evidence

The starting origin/int/t63 commit was bfa23f6ed50524d99904c97961c9e6686462e6ae.
The shared remote-tracking ref advanced during this lane; this branch keeps that original base.

Every FAILED node listed in the red evidence above passes in the final primary run:

| Red regression | Before | After |
| --- | --- | --- |
| Image handlers selected from actual bytes | 12 mismatched cases retained PRIVATE_MARKER; 3 matching cases already passed | All 15 pass; marker gone and decoded pixels equal |
| Unknown bytes, including the replaced legacy fail-open pin | 6 cases DID NOT RAISE | All 6 refuse |
| JPEG APP allowlist | PRIVATE_MARKER survived | Removed; ICC/Adobe retained; pixels equal |
| GIF extensions | PRIVATE_MARKER survived | Comment/XMP removed; loop and both decoded frames retained |
| GLB JSON | Generator/copyright/extras survived | Removed recursively; BIN bytes and lengths retained |
| Invalid modes | 4 cases DID NOT RAISE | All 4 raise invalid media metadata mode |
| Codec user data | H.264/HEVC markers survived; mpeg4 DID NOT RAISE | H.264/HEVC markers removed, AAC streams retained, pixels equal; mpeg4 refuses |
| Required muxer | 2 rows DID NOT RAISE | Both fail with formats row 1 and muxer in the error |

Additional tests cover eight container-family contradictions, missing ffprobe, six malformed
GIF/GLB cases, PCM byte-signature priority, an AV1 metadata OBU with equal decoded pixels,
a synthetic Pixal3D job and its actual GLB download, and migration of a previously completed
GIF cache entry. H.264, HEVC and AV1 encoders were available; none of these tests skipped.
The GLB journey uses a valid triangle model with scenes, mesh, accessor and BIN positions.
The downloaded JSON still points to its three vertices; the BIN buffer is unchanged.

The cache migration now uses version 2 and admits GIF to the new parser. A version-1
completion cannot exempt a cached file from the stricter stripping rules. Speech storage
wraps PCM as WAV; the buffered speech response itself uses the explicit PCM content row.

Primary command (there are no tests/test_media_delivery*.py files in this checkout):

```sh
scripts/run-hermetic-pytest.sh tests/test_media_metadata.py tests/test_media_metadata_formats_data.py tests/test_media_metadata_format_pins.py tests/test_domain_literal_ratchet.py tests/test_t58_domain_data.py tests/test_media_provenance.py tests/test_prompt_metadata_migration.py tests/test_video_job_views.py tests/test_media_inputs*.py tests/test_prompt_memory_integration.py tests/test_prompt_memory_http_privacy.py tests/test_content_access_inventory.py -q -p no:cacheprovider
```

Result: **403 passed, 40 subtests passed**, 52 existing Pillow deprecation warnings, 42.27s.
The content-access inventory passed, so its hash fixture needed no refresh.

Caller command:

```sh
scripts/run-hermetic-pytest.sh tests/test_comfy_workflow_metadata_catalog.py tests/test_media_contract_validation.py tests/test_openai_schema_conformance.py tests/test_prompt_memory_cleanup_r2.py tests/test_prompt_memory_confirmation_r2.py tests/test_prompt_memory_identity.py tests/test_prompt_memory_lifecycle.py tests/test_request_history_e2e_matrix.py tests/test_s7_parity_audio.py tests/test_video_keyframes.py tests/test_proxy_audio_handler_gaps.py tests/test_engine_knowledge_boundary.py tests/test_work_budget.py -q -x -p no:cacheprovider
```

Result: **596 passed, 5 xfailed, 157 subtests passed**, 167.00s. The xfails are existing
expected failures. Total final coverage: **999 passed, 5 xfailed, 197 subtests passed**.
The final caller command had an outer 180-second timeout; it finished normally.
An earlier caller run was stopped after a refusal stranded tests using invalid placeholder
video. Replacing those success fixtures with valid H.264 removed the wait and passed them.

Budget: 28795 → 28846, exactly **+51** cases: 32 initial regressions, 16 refusal/parser
boundaries, Pixal3D journey, AV1 OBU, and GIF migration. The two reviewed domain-literal
counts change from video=4/audio=4 to video=6/audio=5, with reasons in the reviewed data.
No proxy.py changes. No live restart, real upstream, real media or full-suite test run.
All test runs used the hermetic runner against committed trees. Load was below 48 before
running. Old fail-open names/patterns and placeholder-video literals were searched again;
touched production paths and test diffs were read for consistency. git diff --check passes.

## Open risks

- HEVC uses two remux passes. MPEG-TS-incompatible stream combinations refuse; stream
  type/codec comparison prevents silently dropping a stream. AAC plus HEVC is covered;
  arbitrary codec combinations and timestamp edge cases are not exhaustively tested.
- Types without strippers (including non-GLB mesh formats) now refuse. Older QuickTime
  files without ftyp and unrecognized container signatures also refuse.
- Headerless PCM depends on its declared content contract; a known container signature
  takes precedence and can reject PCM bytes that happen to resemble a different format.
- Streaming speech bypasses these buffered delivery callers, as it did before this change.
  No guarantee is made here about metadata inside a stream delivered directly by a backend.

Confidence: 90% for the specified buffered formats, based on synthetic end-to-end delivery,
lossless payload checks and caller coverage. Real backend and arbitrary-file fuzz testing
remain outside this lane. Review should focus on HEVC's header-rebuild intermediate.

## Review follow-up (dot review 2886)

Fixed in this train:

- JPEG kept only a segment prefix and copied the rest of the payload. The JFIF header now
  leaves without its thumbnail, JFXX is dropped, and an Adobe segment must have its fixed
  12-byte payload or the file is refused (dropping it would change how CMYK decodes).
- A dropped GIF plain-text block now takes the graphic control block that governs it, so
  that block no longer retimes the next frame.
- An H.264 MP4 strip was not a fixed point: the bitrate hint box carried the source's
  maximum through the filtered copy, so a second strip changed it. The MP4 and MOV
  rewrites now leave that box out.
- The UI fixture's subprocess guard did not know the codec probe or the stream filters,
  so the household Library video answered 500 under the e2e suite.

Not fixed here, open:

- GLB node, mesh and material names are kept. They are model content, not a known
  prompt location, but a file can carry text there.
- ICC profiles are kept whole, including their description and copyright tags.
- MP3 keeps bytes after the audio frames apart from a final ID3v1 tag, and FLAC keeps bytes
  after its last metadata block; an appended tag can survive. This predates train 62.
- WAV keeps a LIST chunk's sub-chunks without checking each one.
- A few-frame HEVC clip fails the probe after the MPEG-TS rebuild and is refused (no leak).

## Files that are not media (lead decision, 2026-11-13)

A stored item whose bytes match no known media family and whose declared name or type is
not media (a File, or the explicit headerless PCM row) passes through unchanged. Every
other case still goes by the bytes:

- Bytes that sniff as a family with a handler (PNG, JPEG, WebP, GIF, GLB, audio, video)
  always go through that handler, whatever name they arrived under, and are refused if
  the handler fails.
- TIFF, HEIF/HEIC/AVIF, PDF and JPEG XL carry EXIF, XMP, GPS or an author, and there is no
  handler for them here. They are refused under any name, including as a File. An iPhone
  photo is HEIC and carries GPS, so passing it as a File would leak what this train closes.
- A name or type that declares media while the bytes match no family is refused.

The pass-through is labelled. A Library item carries `metadata_handling` (`removed`, `refused` or
`unchanged`) and a `metadata_note` sentence; an `md=none` download carries the
`X-Azizam-Metadata` header with the same value. `unchanged` says the gateway left the
bytes as they were and removed no metadata, so `md=none` does not read as "clean" for a
File.

Open:

- A `.gltf` (JSON) model is a File and passes unchanged; its `asset.generator`,
  `extras` and names can carry text. There is no JSON-aware handler.
- An unknown container that carries metadata and that the sniff does not recognise passes
  as a File, labelled `unchanged`.
