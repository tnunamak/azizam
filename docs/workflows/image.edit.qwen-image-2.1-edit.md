# Qwen-Image 2.1 Edit: workflow notes

Workflow `image.edit.qwen-image-2.1-edit` (manifest `manifests/workflows/qwen-image-2.1-edit.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `reference_resolution`: Reference image is resized to ~resolution x resolution before encoding. Lower values are the documented fix for Qwen 2.1's reference-resolution speed footgun (10-20x slower with multi-megapixel references).
- `cfg`: Official template default. One Reddit commenter reports CFG 1 can show a warm color-cast ("piss filter") and recommends CFG 3-5 with a negative prompt instead; not reproduced in our test image, but worth trying if output looks off.
