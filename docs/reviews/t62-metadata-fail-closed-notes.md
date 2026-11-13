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

Pending: tests are committed and run before production edits.
