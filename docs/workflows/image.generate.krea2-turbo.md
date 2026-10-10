# Krea 2 Turbo: workflow notes

Workflow `image.generate.krea2-turbo` (manifest `manifests/workflows/krea2-turbo.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `steps`: Turbo model; 8-12 steps is the community sweet spot. Values above ~16 show diminishing returns.

## Notes moved from the manifest (2026-10-10)

### Description

Local T2I workflow bound to Krea 2 Turbo (int8_convrot), the current (Sep 2026) community-consensus realism/prompt-adherence leader on Reddit r/StableDiffusion, overtaking Z-Image-Turbo. CFG-distilled turbo model (negative conditioning is zeroed, CFG fixed at 1.0).
