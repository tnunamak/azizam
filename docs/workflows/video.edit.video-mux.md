# Add a Soundtrack to a Video: workflow notes

Workflow `video.edit.video-mux` (manifest `manifests/workflows/video-mux.yml`,
graph `workflows/videos/video-mux.json`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes (2026-11-09)

### Description

Puts an audio track onto a video. The graph uses core ComfyUI nodes only: `LoadVideo` and `GetVideoComponents` give the
frames and the frame rate, `LoadAudio` loads the new track, `CreateVideo` joins them at the original frame rate, and
`SaveVideo` writes one MP4. The original sound of the video is dropped. The picture is not re-timed.

### Why it exists

An agent that made a song and some clips found no tool to join them. This workflow is that tool: any generated or
uploaded video, any generated or uploaded audio, both passed as asset inputs on the run route and the MCP `run` tool.

### Limits

- The new sound always replaces the old. A "keep or mix the old sound" switch needs a mixing node; core ComfyUI has none,
  so the workflow does not offer one.
- When the track is longer than the video, the result follows `CreateVideo`'s own rule. Check the result length with a
  real run before relying on a given case.
