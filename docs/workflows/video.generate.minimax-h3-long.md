# video.generate.minimax-h3-long: workflow notes

Workflow `video.generate.minimax-h3-long` (manifest `manifests/workflows/video-minimax-h3-long.yml`).

## Notes moved from the manifest (2026-10-10)

### Description

MiniMax H3 long video with native audio. The manifest binds the single-segment (10 s) reference graph; POST /v1/videos builds longer chains (shots, seconds past 10 s) and POST /v1/videos/extensions extends a video, both in code. Each added segment is pinned to the end of the video so far with the H3 Motion Context nodes. LICENSE: the MiniMax H3 Community License excludes use in the US, EU, UK and South Korea.

### Original manifest text

```yaml
description: >-
  MiniMax H3 long video with native audio. The manifest binds the single-segment (10 s) reference
  graph; POST /v1/videos builds longer chains (shots, seconds past 10 s) and POST /v1/videos/extensions
  extends a video, both in code. Each added segment is pinned to the end of the video so far with the
  H3 Motion Context nodes. LICENSE: the MiniMax H3 Community License excludes use in the US, EU, UK and
  South Korea.
```
