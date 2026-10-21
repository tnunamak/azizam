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
  add-guide node at frame `length // 2`. A math node in the graph reads the length node, so the
  index follows `seconds`.
- The H3 nodes have no strength input for either guide, so there is no `last_frame_strength`.

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
