# SeedVR2 7B Sharp Upscale: workflow notes

Workflow `image.edit.seedvr2-upscale` (manifest `manifests/workflows/seedvr2-upscale.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

One-step image upscale/restore via SeedVR2 7B Sharp (int8_convrot, Apache-2.0, ICLR 2026). Verified at 2x (1024->2048) and 4x (1024->4096): both sharp, no grid/halo artifacts, genuinely enhanced fine detail (tested on food photography with fine scallion/noodle texture). steps=1, cfg=1 is the official template default -- SeedVR2 is a one-step flow-matching refine, not a traditional multi-step diffusion upscaler, so this is intentional rather than under-configured.
