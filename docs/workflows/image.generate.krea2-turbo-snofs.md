# Krea 2 Turbo + SNOFS (uncensored): workflow notes

Workflow `image.generate.krea2-turbo-snofs` (manifest `manifests/workflows/krea2-turbo-snofs.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `steps`: Turbo model; 8-12 steps is the community sweet spot. Values above ~16 show diminishing returns.
- `lora_strength`: SNOFS LoKr strength. 1.0 is the author-recommended value; 0.6 was less reliable in the bake-off.
