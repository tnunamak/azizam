# video.edit.scail-2-replace: workflow notes

Workflow `video.edit.scail-2-replace` (manifest `manifests/workflows/video-scail-2-replace.yml`).

## Notes moved from the manifest — 2026-10-10

### Description

Reference character image + driving video -> the reference character performs the driving video's motion in the driving scene (SAM3.1 tracking, replacement mode, first 81-frame segment). source_subject and reference_subject are the SAM3 text prompts that find the person to replace in the video and the character in the image. Only one person is replaced: the largest tracked one (subject_index 0), another by subject_index, or the person under source_point_x/source_point_y (a point in the first frame, 0 to 1). The reference character is rescaled and placed to match the replaced person (reference_fit, default true). The job reports a warning when several people were tracked. SCAIL-2 Q4_K_M GGUF, 6 steps. ~300 s warm at 832x448. MIT.

### Original manifest text

```yaml
description: >-
  Reference character image + driving video -> the reference character performs the driving video's motion in the driving scene (SAM3.1 tracking, replacement mode, first 81-frame segment). source_subject and reference_subject are the SAM3 text prompts that find the person to replace in the video and the character in the image. Only one person is replaced: the largest tracked one (subject_index 0), another by subject_index, or the person under source_point_x/source_point_y (a point in the first frame, 0 to 1). The reference character is rescaled and placed to match the replaced person (reference_fit, default true). The job reports a warning when several people were tracked. SCAIL-2 Q4_K_M GGUF, 6 steps. ~300 s warm at 832x448. MIT.
```
