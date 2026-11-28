# video.generate.ltx-2.5-keyframes-mid: workflow notes

Workflow `video.generate.ltx-2.5-keyframes-mid` (manifest
`manifests/workflows/video-ltx-2.5-keyframes-mid.yml`) is `video-ltx-2.5-i2v` with a last
picture and a middle picture pinned. It is the three-picture sibling of
`video.generate.ltx-2.5-keyframes`. The graph is a copy of that graph with two more guides
and the two extra pictures' resize and preprocess nodes.

## How the pictures are pinned

- First picture: `input_reference` (as for i2v). Its stage-2 strength is the route's
  `image_strength` field (default 0.7, same node as in i2v); stage 1 pins it at 1.
- Last picture: `extensions.com.azizam.workflow.last_frame`, added as a guide on frame -1 at
  both sampling stages. `last_frame_strength` (0 to 1, default 1) sets both guides.
- Middle picture: `extensions.com.azizam.workflow.mid_frame`, resized like the first picture at
  each stage, preprocessed, and added as a guide after the last-frame guide at both stages.
  `mid_frame_strength` (0 to 1, default 1) sets both mid guides.
- `mid_frame_index` chooses the guarded frame. A math node in the graph computes half the frame
  count (`a // 2`) when the input is omitted, so the index follows `frames` or `seconds`; a
  supplied value replaces that link on both guide nodes. The manifest requires a value of at
  least 1; the graph has no way to reject an index at or above `frames - 1`, so the route
  documents that limit in the input help instead of enforcing it. The guide node rounds an
  index down to the nearest multiple of 8, so 24 is exact for a 49-frame clip.
- `img_compression` (0 to 100, default 18) is one input bound to every `LTXVPreprocess` node in
  the graph, including the two new middle-picture nodes.
- Guide frames are cropped away after each stage, as in the two-picture workflow.

## Length

`seconds` is still capped at 10 s. `frames` is an exact count on the model's 8N+1 grid
(121 frames is 5 s at 24 fps); 240 is refused with a 400 that names `frames`. When both are
sent, `frames` wins and the rendered clip uses that exact count. The `seconds` value is still
validated against its cap, but it no longer decides the length.

## Audio

`extensions.com.azizam.video.audio = false` removes the audio track from the stored clip and
the returned file. It is a gateway control, not a workflow input, and works on every video
model. Omitted or `true` leaves the bytes unchanged.

## Measurements

The two-picture workflow's measurements are in `video.generate.ltx-2.5-keyframes.md`. A
single live render of this graph is recorded in the branch change that added it; run
`ffmpeg -i clip.mp4 -vf "select=eq(n\,60)"` and compare frame 60 with the middle picture to
check the guide.
