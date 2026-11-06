# Conditioning Controls

Date: 2026-05-15

Conditioning covers workflow controls that steer generation with extra media or
model-side adapters: ControlNet, IP-Adapter, style references, depth/edge maps,
pose maps, masks, regional prompting, and similar mechanisms.

## Product Rule

Offer conditioning in the API, but only as manifest-backed workflow controls.
Do not add global OpenAI-compatible top-level fields such as
`controlnet_strength` or `control_image`. They are not standard OpenAI fields,
and most workflows cannot honor them.

The live contract is:

1. A workflow manifest declares the input, type, range, and description.
2. The manifest maps that input to a real backend field or ComfyUI node.
3. Discovery advertises the input through `/gateway/v1/workflows` and
   `/gateway/v1/extensions`.
4. The route validates, binds, and receipts the input by name.
5. Unknown top-level controls are rejected instead of silently dropped.

Agent rule: discovery is authoritative. Treat examples and naming conventions
as patterns only; do not send conditioning controls unless the selected
workflow advertises them through `/gateway/v1/workflows` or
`/gateway/v1/extensions`.

## OpenAI-Compatible Image Edit Shape

For multipart image edits, extension-valued image assets can reference an extra
multipart field:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $AZIZAM_API_KEY" \
  -F model=flux2-klein-4b-edit-controlnet \
  -F prompt='preserve the geometry, paint it as underwater rock' \
  -F image=@input.png \
  -F control_image=@edge-map.png \
  -F 'extensions={"com.azizam.workflow":{"control_image":{"multipart_field":"control_image"},"controlnet_strength":0.75}}'
```

`control_image` in the form is transport only. The actual API control is still
`extensions.com.azizam.workflow.control_image`, and it must be listed by the
selected workflow manifest as an `asset` input.

JSON clients that already have a backend-ready or gateway asset reference can
send the asset value directly under the same extension name:

```json
{
  "model": "image.edit.example-controlnet",
  "prompt": "preserve geometry",
  "extensions": {
    "com.azizam.workflow": {
      "control_image": {"asset_id": "asset_123"},
      "controlnet_strength": 0.75
    }
  }
}
```

Live image results expose gateway-native `asset_id` handles such as
`img_<sha256>`. Those handles can be reused for asset-valued workflow inputs
only when the selected manifest declares and binds that input. Reuse is scoped
to the authenticated API key that minted the asset.

## Naming Convention

Use common names when they honestly bind to real workflow behavior:

| Name | Type | Meaning |
|---|---|---|
| `control_image` | `asset` | Conditioning image, edge map, depth map, pose map, or similar. |
| `controlnet_strength` | `number` | ControlNet weight/strength. Typical range is workflow-specific. |
| `controlnet_start` | `number` | Optional start fraction/step if the workflow exposes it. |
| `controlnet_end` | `number` | Optional end fraction/step if the workflow exposes it. |
| `controlnet_model` | `string` | Optional model selector if safe for the access plan and backend. |
| `controlnet_preprocessor` | `string` | Optional preprocessor selector if the workflow has a real preprocessor stage. |

Other conditioning families should use equally explicit workflow controls, for
example `ip_adapter_image`, `ip_adapter_weight`, `style_reference_image`, or
`regional_prompt`. Keep these as manifest inputs until multiple users and
backend families prove a stronger convention is needed.

## Readiness Gate

A ControlNet-capable workflow should not be advertised until all of these are
true:

- `scripts/local/audit_controlnet_readiness.py` reports the local ComfyUI
  install can enable ControlNet.
- The manifest has `requires.custom_nodes` and `requires.models` entries for
  the ControlNet nodes and model artifacts it depends on.
- The workflow JSON contains real loader/apply/preprocessor nodes as needed.
- Every advertised control has a backend binding.
- Tests assert the backend request changes at the intended node or field.

Current local status:

- `image.generate.flux2-klein-4b-seamless-tile` is the recommended local
  texture-tile endpoint when agents need an actually tileable result today. It
  is gateway-composed: generate with `flux2-klein-4b`, offset by 50%/50%, mask
  the centered seam cross, repair that mask through `flux2-klein-4b-inpaint`,
  then offset back. It exposes base `steps`, `guidance`, and `seed`, plus
  seam-specific `seam_mask_width`, `seam_mask_expand`, `seam_mask_feather`,
  `seam_steps`, `seam_guidance`, `seam_denoise`, and `seam_prompt`.
- `image.generate.sdxl-seamless-tile` is still available as the direct SDXL
  workflow. It uses `Image Seamless Texture` postprocessing and accepts
  `steps`, `guidance`, `seed`, `batch_size`, `negative_prompt`, `sampler`,
  `scheduler`, and `seam_blending` as workflow extensions. `seam_blending` is
  constrained to `0..0.5`, matching the installed WAS/img2texture backend. This
  is not the full circular SDXL stack; this machine does not currently have
  Make Circular VAE, Asymmetric/Tiled KSampler, or ControlNet Tile weights
  installed.
- `image.edit.flux2-klein-4b` exposes `steps`, `guidance`, and `denoise`.
  Standard OpenAI `mask` is accepted; without a native mask workflow it falls
  back to gateway post-composite preservation.
- `image.edit.flux2-klein-4b-inpaint` is auto-selected for
  `flux2-klein-4b-edit` requests that include `mask`. It binds the normalized
  OpenAI mask into ComfyUI with `SetLatentNoiseMask` and exposes
  `steps`, `guidance`, `denoise`, `seed`, `mask_expand`, and `mask_feather` as
  workflow extensions.
- ControlNet and IP-Adapter are not advertised on this machine yet. ComfyUI has
  node support, but the local inventory is missing ControlNet weights,
  IP-Adapter weights, and CLIP-vision assets. ControlNet-like controls are
  rejected until a real workflow, manifest, and model artifacts are installed.
