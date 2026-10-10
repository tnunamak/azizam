# Qwen-Image 2.1 Inpaint (LanPaint): workflow notes

Workflow `image.edit.qwen-image-2.1-inpaint` (manifest `manifests/workflows/qwen-image-2.1-inpaint.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

Mask-bound inpaint combining Qwen-Image 2.1 with LanPaint (scraed/LanPaint; no dedicated Qwen 2.1 inpaint checkpoint exists, LanPaint is the community-consensus universal inpainting tool, updated for Qwen 2.1 support Sep 2026). Mask is the source image's own alpha channel (OpenAI edit-API convention: transparent = regenerate). KNOWN LIMITATION, reproduced in our own test and consistent with community reports (research-image.md): a faint horizontal blend seam/haze band can appear at the mask boundary on hard-edged masks. Mask feathering (mask_feather, default 48px) reduces but does not eliminate it -- this is flagged in the community as unresolved ("neither fix is perfect") as of Sep 2026. Prefer masks with generous feather/soft edges over pixel-exact cutouts.
