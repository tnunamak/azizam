# video.generate.ltx-2.5-keyframes: workflow notes

Workflow `video.generate.ltx-2.5-keyframes` (manifest `manifests/workflows/video-ltx-2.5-keyframes.yml`)
is `video-ltx-2.5-i2v` with the last picture pinned.

## How the pictures are pinned

- First picture: `input_reference` (as for i2v). Its stage-2 strength is the route's
  `image_strength` field (default 0.7, same node as in i2v); stage 1 pins it at 1.
- Last picture: `extensions.com.azizam.workflow.last_frame`, resized like the first picture at each
  stage, pre-processed (compression 18) and added as a guide on frame -1 at both sampling stages.
  `last_frame_strength` (0 to 1, default 1) sets both guides. The guide frames are cropped away
  after each stage.
- Derived from the working reference graph for first, middle and last guides; this workflow ships
  first and last only.

## Measurements (2026-10-21, 3090, 960x512, 121 frames, seed 11)

| Clip | Frame | Compared with | PSNR | SSIM |
|---|---|---|---|---|
| keyframes, same picture first and last, strength 1 | last | that picture | 32.4 | 0.893 |
| i2v, no last picture | last | that picture | 17.2 | 0.579 |
| keyframes, last = mirrored picture | last | mirrored picture | 32.0 | 0.893 |
| keyframes, last = mirrored picture | last | first picture | 16.3 | 0.612 |

Render time: 40 s warm (second and third clip); 105 s for the first clip after other models were loaded.
