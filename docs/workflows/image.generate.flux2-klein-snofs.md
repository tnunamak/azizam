# FLUX.2 Klein 9B + SNOFS (uncensored): workflow notes

Workflow `image.generate.flux2-klein-snofs` (manifest `manifests/workflows/flux2-klein-snofs.yml`,
graph `workflows/images/flux2-klein-snofs.json`).

Research and provenance notes for owners and maintainers. No screen or API renders this file;
the manifest keeps only a plain `label` and one-sentence `help` per input.

## Graph

The graph is the stock FLUX.2 Klein 9B txt2img graph (`workflows/images/flux2-klein.json`) plus one node:

- Node `11` (`LoraLoaderModelOnly`) takes the UNet from node `1` and loads
  `klein_snofs_v1_4.safetensors` at `strength_model` 1.0.
- KSampler `31` takes its model from node `11`.

Nothing else changes. `TestFlux2KleinSnofsWorkflow` in `tests/test_uncensored_image_manifests.py`
checks that the graph equals the stock graph apart from node `11` and that one link.
The LoRA sits in the same place as in `image.generate.krea2-turbo-snofs`.

Node `90` (`RMBG`) comes from the stock graph. Nothing consumes its output, so ComfyUI
does not run it. The manifest still requires the RMBG custom node, as the stock graph and the
Krea manifest do.

## LoRA facts

- File: `Diffusion/loras/klein_snofs_v1_4.safetensors` (1.09 GB, on `/media/windows/AI Models`).
- Format: LoKr (`lokr_w1` and `lokr_w2` keys). `LoraLoaderModelOnly` applies it to the UNet only.
- Base: the safetensors metadata has `ss_base_model_version` = `flux2_klein_9b` and was written by
  ai-toolkit 0.9.4. The LoRA is for the 9B base, so it does not belong on the 4B workflows.
- I read the safetensors header only. I did not check the author's recommended strength or the
  training data.

## Input notes

- `lora_strength`: default 1.0. This follows the Krea SNOFS default, where 1.0 is the
  author-recommended value. The Klein author's value is not known here. Treat 1.0 as a starting
  point until a comparison says otherwise.
- `steps`: the stock graph is a 4-step distilled sampler (`cfg` 1, `euler`, `simple`).

## Verification (2026-10-28)

- Renders at 512 x 512, seed 7, prompt "a photograph of an adult woman reading a book in a sunlit
  kitchen, natural light". Both the stock graph and the SNOFS graph rendered successfully.
- The LoRA changes the output. Mean absolute difference per channel was 46.9, 53.5 and 60.5
  (maximum 243).
- Timing: the SNOFS render took 42 s and the stock render 15 s. One run each, back to back, so this
  is not a warm benchmark. The manifest keeps `typical_seconds: 20` from the stock graph and is not
  timed yet.
- ComfyUI log: no LoRA key warnings. The only warning was about `model.lm_head`, which is unrelated.
- Not done: no comparison against the base, no quality or content judgement, and no check of the
  author's recommended strength.
- The gateway reads manifests at startup. The workflow appears in the model list after a restart.
- No alias points at this workflow. `image-uncensored` still resolves to Krea 2 Turbo + SNOFS.
