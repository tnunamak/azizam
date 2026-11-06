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
- `img_compression` (0 to 100, default 18) is one input bound to every `LTXVPreprocess` node in
  the graph.
- `frames` is an exact count on the 8N+1 grid (121 frames is 5 s at 24 fps); 240 is refused with
  a 400 that names `frames`. It wins over `seconds` when both are sent. `seconds` keeps its 10 s
  cap for requests that do not send `frames`.
- `extensions.com.azizam.video.audio = false` removes the audio track from the stored clip and
  the returned file. It is a gateway control, not a workflow input, and works on every video
  model. Omitted or `true` leaves the bytes unchanged.
- Derived from the working reference graph for first, middle and last guides; the
  `video.generate.ltx-2.5-keyframes-mid` workflow adds the middle guide.

## Measurements (2026-10-21, 3090, 960x512, 121 frames, seed 11)

| Clip | Frame | Compared with | PSNR | SSIM |
|---|---|---|---|---|
| keyframes, same picture first and last, strength 1 | last | that picture | 32.4 | 0.893 |
| i2v, no last picture | last | that picture | 17.2 | 0.579 |
| keyframes, last = mirrored picture | last | mirrored picture | 32.0 | 0.893 |
| keyframes, last = mirrored picture | last | first picture | 16.3 | 0.612 |

Render time: 40 s warm (second and third clip); 105 s for the first clip after other models were loaded.
