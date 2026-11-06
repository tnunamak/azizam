# Qwen-Image 2.1 Edit: workflow notes

Workflow `image.edit.qwen-image-2.1-edit` (manifest `manifests/workflows/qwen-image-2.1-edit.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `reference_resolution`: Reference image is resized to ~resolution x resolution before encoding. Lower values are the documented fix for Qwen 2.1's reference-resolution speed footgun (10-20x slower with multi-megapixel references).
- `cfg`: Official template default. One Reddit commenter reports CFG 1 can show a warm color-cast ("piss filter") and recommends CFG 3-5 with a negative prompt instead; not reproduced in our test image, but worth trying if output looks off.

## Notes moved from the manifest (2026-10-10)

### Description

Single-image instruction edit via Qwen-Image 2.1 (int8_convrot) + qwen3vl_8b_w4a8 text encoder + madebyollin texture-fix VAE. Community consensus (Sep 2026, r/StableDiffusion) ranks this the best local editor available, ahead of Flux.2 Klein 9B, especially for edits needing world knowledge. Output resolution tracks the input image. License: Qwen research license (disputed scope on Reddit re: output rights) -- flagged for legal review before commercial-use reliance per research-image.md.
