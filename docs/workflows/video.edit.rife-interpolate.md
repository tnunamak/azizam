# RIFE 2x Frame Interpolation: workflow notes

Workflow `video.edit.rife-interpolate` (manifest `manifests/workflows/rife-interpolate.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

Doubles video frame rate via ComfyUI-Frame-Interpolation's native RIFE VFI node (rife49.pth). Frame count and output fps both double, so real-time duration is unchanged. Audio passes through unchanged. Verified end to end on a 3s/24fps synthetic test clip -> 48fps/143 frames, ~3s duration preserved, 6.4s wall time, ~6.3GB VRAM delta. Published in workflow and capability discovery and executable through the manifest-driven /gateway/v1/workflows/{workflow_id}/run route.
