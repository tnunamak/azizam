# video.generate.ltx-2.5-i2v: workflow notes

Workflow `video.generate.ltx-2.5-i2v` (manifest `manifests/workflows/video-ltx-2.5-i2v.yml`).

## Notes moved from the manifest — 2026-10-10

### Description

LTX-2.5 22B distilled image-to-video with synchronized audio. Selected automatically when /v1/videos gets input_reference with model ltx-2.5, or by model id. Same size and length rules as ltx-2.5 (multiples of 64, max 1280x704, 1-10 s). About 85 s for 5 s at 960x512. image_strength default 0.7. License: LTX-2.x Community License.

### Original manifest text

```yaml
description: 'LTX-2.5 22B distilled image-to-video with synchronized audio. Selected automatically when
  /v1/videos gets input_reference with model ltx-2.5, or by model id. Same size and length rules as ltx-2.5
  (multiples of 64, max 1280x704, 1-10 s). About 85 s for 5 s at 960x512. image_strength default 0.7.
  License: LTX-2.x Community License.'
```
