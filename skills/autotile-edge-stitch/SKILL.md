---
name: autotile-edge-stitch
description: Build 16-case marching-squares (dual-grid) autotile sprite sheets from two game-art materials. Uses an operator-configured OpenAI-compatible Images API for base textures and an operator-configured ComfyUI inpaint workflow for transitions. Use for 2D terrain tiles, material boundaries, and reproducible offline composition of existing sub-tiles.
---

# Autotile edge stitch

This is a client-side asset workflow, not a gateway backend integration. It keeps
model IDs, workflow-specific ComfyUI loader and stitch nodes, model files,
endpoints, and local directories as operator configuration. It does not add
engine-specific gateway code.

The core output is a 4×4 atlas of the 16 marching-squares cases. Bit order is
`TL=0`, `TR=1`, `BL=2`, `BR=3`; bit set means that terrain cell is filled.
`tile_00` is empty and `tile_15` is filled. The scripts also write individual
tiles, a preview, and a manifest.

## Before a live build

1. Discover that the selected model has Image-generation capability. For Vivid
   Fish, use `GET /gateway/v1/models/{model}/capabilities` with an App bearer
   token. Use a model whose operator configuration makes tileable base textures.
2. Configure a ComfyUI inpaint workflow that provides the node types and files
   named below. The edge stitcher submits only this operator-supplied workflow
   data directly to the configured ComfyUI URL.
3. Keep the gateway bearer key in an environment variable. Never put a key in a
   command line, source file, prompt, or token cache.

Example configuration (replace every value with your own installation):

```bash
export AI_GATEWAY_API_KEY='…' # set locally; never commit or paste this value
export AUTOTILE_GATEWAY_BASE_URL='https://your-gateway.example/openai/v1'
export AUTOTILE_TEXTURE_MODEL='my-seamless-image-model'
export AUTOTILE_COMFY_URL='http://your-comfy-host:your-port'
export AUTOTILE_COMFY_INPUT_DIR="$HOME/ComfyUI/input"
export AUTOTILE_COMFY_OUTPUT_DIR="$HOME/ComfyUI/output"
export AUTOTILE_INPAINT_MODEL_FILE='my-inpaint-model.gguf'
export AUTOTILE_UNET_LOADER_NODE='your-unet-loader-node'
export AUTOTILE_TEXT_ENCODER_1='my-text-encoder-1.gguf'
export AUTOTILE_TEXT_ENCODER_2='my-text-encoder-2.safetensors'
export AUTOTILE_DUAL_CLIP_TYPE='your-dual-clip-loader-type'
export AUTOTILE_DUAL_CLIP_LOADER_NODE='your-dual-clip-loader-node'
export AUTOTILE_VAE_FILE='my-vae.safetensors'
export AUTOTILE_VAE_LOADER_NODE='your-vae-loader-node'
# Override these only when your installed workflow uses other node types.
export AUTOTILE_INPAINT_CROP_NODE='InpaintCropImproved'
export AUTOTILE_INPAINT_STITCH_NODE='InpaintStitchImproved'
```

`AUTOTILE_GATEWAY_API_KEY_ENV` can name a different environment variable. The
builders use `AI_GATEWAY_API_KEY` by default. `AUTOTILE_TEMP_DIR` defaults to
`~/.tmp`; use it for large verification scratch space instead of `/tmp`.

## Build a two-material set

```bash
python3 skills/autotile-edge-stitch/scripts/build_dualgrid.py \
  --out-dir "$HOME/.tmp/stone-grass" \
  --filled-prompt 'weathered grey stone, top-down game art, seamless texture' \
  --empty-prompt 'lush green grass, top-down game art, seamless texture' \
  --texture-model "$AUTOTILE_TEXTURE_MODEL" \
  --size 512 \
  --seed 42
```

The gateway request is `POST {AUTOTILE_GATEWAY_BASE_URL}/images/generations`
with the documented OpenAI fields `model`, `prompt`, `size`, `n: 1`, and
`response_format: "b64_json"`. The builder intentionally does not invent
workflow controls. If a chosen gateway model requires controls, discover them
and configure that model/workflow at the gateway boundary first.

Use `--reuse-existing` to recomposite already-generated `sub_*.png` images
without inference. This is the fastest path after manual edits or a compositor
change. The exact output layout and the four-bit lookup are deterministic given
the sub-tile inputs; diffusion output is not.

## Other scripts

- `build_layered.py`: stacked transparent terrain layers from a JSON spec.
- `build_direct.py`: directly inpaints each non-base 16-case layer. It costs
  more than precomposition, but can give different transition geometry.
- `build_set.py`: builds a declared ordered set of edge-constrained tiles.
- `edge_stitch.py`: one inpaint operation, then hard-composites preserved mask
  pixels so the black (preserve) mask region is byte-identical to its reference.
- `validate_tileset.py`: checks alpha quadrants and creates a human review sheet.

All live builders accept `--gateway-*` and/or `--comfy-*` overrides. Run a
script with `--help` to see its complete configuration surface.

## Verification

```bash
skills/autotile-edge-stitch/scripts/verify.sh
```

The default verifier is deterministic and offline: it checks the hard
post-composite invariant, all 16 composition cases, and atlas layout. It needs
only Python, NumPy, and Pillow. It performs no inference and reads no secrets.

Run the explicitly opt-in integration smoke test only after setting the above
configuration:

```bash
skills/autotile-edge-stitch/scripts/verify.sh --live
```

The live smoke test checks only that a real configured request completes and
that the preserved region of its output is byte-identical in every reference channel after post-composite. It
does not certify visual quality or seamlessness; inspect the generated preview
and `validate_tileset.py` contact sheet before shipping art.

## Limitations

Quarter-tile precomposition gives deterministic reuse of the sampled quadrants;
it does not prove visual seamlessness between independently generated images or
guarantee engine-specific import rules. Treat the atlas as an interoperable
16-case marching-squares source asset, then configure the target engine’s tile
rules and inspect the result in that engine. See [reference provenance](docs/README.md)
for technique and integration sources.
