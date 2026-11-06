# Krea 2 Turbo + SNOFS (uncensored): workflow notes

Workflow `image.generate.krea2-turbo-snofs` (manifest `manifests/workflows/krea2-turbo-snofs.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Input notes (moved from the manifest on 2026-10-09)

- `steps`: Turbo model; 8-12 steps is the community sweet spot. Values above ~16 show diminishing returns.
- `lora_strength`: SNOFS LoKr strength. 1.0 is the author-recommended value; 0.6 was less reliable in the bake-off.

## Notes moved from the manifest (2026-10-10)

### Description

Local T2I workflow for uncensored output. Krea 2 Turbo (int8_convrot) plus the SNOFS v1.4 LoKr (Ashen3) loaded with stock LoraLoaderModelOnly at strength 1.0. Bake-off 2026-10-03 showed no visible SFW quality loss and explicit output where stock Krea 2 Turbo returns clothed images. Write natural-language prompts that say 'photograph' (not 'photorealistic'). CFG-distilled turbo model, CFG fixed at 1.0. Only depict adults.
