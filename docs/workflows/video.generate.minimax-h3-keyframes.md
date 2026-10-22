# video.generate.minimax-h3-keyframes: workflow notes

Workflows `video.generate.minimax-h3-keyframes` (first and last picture) and
`video.generate.minimax-h3-keyframes-mid` (first, middle and last picture), manifests
`manifests/workflows/video-minimax-h3-keyframes.yml` and `video-minimax-h3-keyframes-mid.yml`.
Both are `video-minimax-h3-i2v` with more pictures. The licence is the MiniMax H3 Community
License (not for use in the US, EU, UK or South Korea).

## How the pictures are pinned

- First picture: `input_reference` on `POST /v1/videos` (as for i2v), into `first_frame` of the
  conditioning node.
- Last picture: `extensions.com.azizam.workflow.last_frame`, cropped to the canvas like the first
  picture and fed to the node's `last_frame` input.
- Middle picture (`-mid` only): `extensions.com.azizam.workflow.middle_frame`, anchored by an
  add-guide node at `extensions.com.azizam.workflow.mid_frame_index`. When that input is omitted,
  a math node in the graph reads the length node and the index is `length // 2`, exactly as
  before; a supplied value replaces the link. The manifest requires a value of at least 1 and
  documents the upper limit (`frames - 1`).
- `frames` is an exact count on the 17N+5 grid (124 and 243 are accepted; 240 is refused with a
  400 that names `frames`). It wins over `seconds` when both are sent. `seconds` keeps its 10 s
  cap for requests that do not send `frames`.
- `extensions.com.azizam.video.audio = false` removes the audio track from the stored clip and
  the returned file. It is a gateway control, not a workflow input, and works on every video
  model. Omitted or `true` leaves the bytes unchanged.
- The H3 nodes have no strength input for either guide, so there is no `last_frame_strength` or
  `mid_frame_strength`.

## Measurements (2026-10-21, 3090, 960x544, 124 frames, seed 11, ComfyUI at :7890)

PSNR and SSIM of decoded frames against the picture (cropped to the canvas). The ceiling for
exact is about 31 to 32 dB: the first frame of every clip scores 31.4 to 31.7 dB (VAE and h264).

| Clip | Frame | Compared with | PSNR | SSIM |
|---|---|---|---|---|
| keyframes, same picture first and last | last | that picture | 31.7 | 0.823 |
| i2v, no last picture | last | that picture | 17.3 | 0.689 |
| keyframes, last = mirrored picture | last | mirrored picture | 31.5 | 0.832 |
| keyframes, last = mirrored picture | last | first picture | 16.2 | 0.574 |
| keyframes-mid, middle = colour-shifted picture | 62 | middle picture | 31.3 | 0.841 |
| keyframes-mid | 62 | first picture | 20.3 | 0.528 |
| keyframes-mid | last | mirrored picture | 31.4 | 0.819 |

Render time: 110 to 125 s per clip (parent i2v: 135 s typical), one clip at a time on an idle queue.
