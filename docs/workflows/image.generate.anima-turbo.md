# Anima Turbo (anime): workflow notes

Workflow `image.generate.anima-turbo` (manifest `manifests/workflows/anima-turbo.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `steps`: Turbo model; 8-12 steps is the sweet spot.

## Notes moved from the manifest (2026-10-10)

### Description

Local anime T2I workflow bound to Anima Turbo v1.1 (2B). Fast (about 3.5 s warm at 1024x1024 on a 3090). Accepts booru tags plus prose. CFG fixed at 1.0. Only depict adults.
