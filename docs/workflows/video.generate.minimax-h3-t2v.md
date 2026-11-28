# video.generate.minimax-h3-t2v: workflow notes

Workflow `video.generate.minimax-h3-t2v` (manifest `manifests/workflows/video-minimax-h3-t2v.yml`).

## Notes moved from the manifest (2026-10-10)

### Description

MiniMax H3 FL2VA text-to-video with native stereo audio (int8_convrot, 8-step turbo LoRA). Put sound cues after an 'Audio:' paragraph in the prompt. Request via POST /v1/videos: size multiples of 32 (max 1344x768; default 960x544), seconds 2-10 at 24 fps (frames are 17k+5). 120-250 s for 5 s at 960x544. LICENSE: the MiniMax H3 Community License excludes use in the US, EU, UK and South Korea.

### Original manifest text

```yaml
description: 'MiniMax H3 FL2VA text-to-video with native stereo audio (int8_convrot, 8-step turbo LoRA).
  Put sound cues after an ''Audio:'' paragraph in the prompt. Request via POST /v1/videos: size multiples
  of 32 (max 1344x768; default 960x544), seconds 2-10 at 24 fps (frames are 17k+5). 120-250 s for 5 s
  at 960x544. LICENSE: the MiniMax H3 Community License excludes use in the US, EU, UK and South Korea.'
```
