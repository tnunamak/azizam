# SLVP Scope And Extension Shape

Date: 2026-05-15

This document scopes the gateway around the top target users without turning one
operator's current machine into the product spec.

## Implementation Status

The extension ladder, manifest layering, and unknown-parameter policy below
describe target behavior. Status on `main`:

- Pure manifest validation and binding helpers exist in
  `gateway/manifests.py`.
- Pure workflow registry with portable input/output summaries exists in
  `gateway/discovery.py`.
- Pure app-profile policy module exists in `gateway/policy.py`.
- Observational image-route receipts run in
  `/v1/images/generations` and `/v1/images/edits` but do not record accepted
  vs rejected inputs yet.
- The OpenAI-compatible image-edit handler still parses only the standard
  fields listed in the example below. There is no `extensions` container in
  the live request shape and no `/gateway/v1/media/jobs` endpoint.

## Scope Decision

The core product should optimize for three archetypes:

1. Local agent/coding tool power user
2. Multi-backend GPU tinkerer
3. Local multimodal workflow builder

The common need across all three is:

```text
make existing clients and existing engines work together through one reliable
local endpoint, while preserving enough local control to be useful.
```

The product should not promote every local trick into core. It should provide
clear extension points so local tricks can be expressed, tested, and shared when
they become generally useful.

## What Belongs In Core

Core should include:

- public protocol frontends for OpenAI, Anthropic, and Ollama
- backend adapter contract
- workflow manifest schema
- media job/assets API
- credential/tenant plane
- route receipts
- app profiles and compatibility harness
- diagnostics
- resource-aware scheduling boundary
- native passthrough lanes with policy controls

Core should not include:

- every ComfyUI node parameter as a gateway-level concept
- every sampler/controlnet/denoise/upscale setting as a public protocol field
- hardcoded behavior for one operator's workflow names
- backend admin passthrough as a default public surface
- silent acceptance of parameters that do nothing

## Extension Ladder

Use this ladder to decide where a feature belongs.

### 1. Local Manifest Input

Use this for operator-specific workflow controls.

Examples:

- `denoise`
- `controlnet_strength`
- `controlnet_model`
- `preserve_geometry`
- `style_reference_weight`
- custom music generation parameters
- custom video workflow knobs

These live in workflow manifests and map to backend nodes/fields.

### 2. Shared Manifest Convention

Promote to a convention when several workflows use the same concept.

Examples:

- `seed`
- `size`
- `duration_seconds`
- `background`
- `guidance_scale`
- `negative_prompt`
- `strength`

These are still manifest inputs, but the gateway can give them nicer validation,
docs, and app/profile support.

### 3. Canonical Intent Field

Promote to canonical intent only when the concept applies across multiple
backend families and public protocols.

Examples:

- chat messages
- tool calls
- image prompt
- speech voice
- transcription language
- media duration
- output format

### 4. Public Protocol Field

Expose as public protocol compatibility only when a real client protocol already
expects it or the gateway owns the protocol.

Examples:

- OpenAI `response_format`
- Anthropic `tools`
- Ollama `keep_alive`
- gateway media job `parameters`

Gateway-owned protocols should live under `/gateway/v1/*`, not under
vendor-compatible `/v1/*`. This keeps OpenAI-compatible routes clean while still
giving power users a richer API.

## Rule For Weird Local Needs

If a feature is valuable to one local setup but not obviously general:

- support it as a typed manifest input
- validate it
- include it in route receipts
- test that it mutates the backend request
- keep it out of global protocol semantics

That keeps the first user in the product without making the first user's whole
machine the product.

## Example: Image Edit Denoise And ControlNet

An agent observed that passing extra params to `/v1/images/edits` did not change
the output. That matches the current implementation shape: the OpenAI-compatible
edit handler parses standard fields such as:

- `image`
- `mask`
- `prompt`
- `model`
- `size`
- `background`
- `n`
- `response_format`

It then mutates the selected ComfyUI workflow for image upload, prompt, size,
seed/background behavior, and adaptive batching. There is no typed contract for
`denoise` or `controlnet`, so those inputs cannot affect the workflow.

The right product answer is not "add arbitrary passthrough params to OpenAI
Images." The right answer is:

1. Create or select a workflow that actually has denoise/controlnet nodes.
2. Describe those controls in a workflow manifest.
3. Accept them through an explicit gateway extension field or media job request.
4. Validate them.
5. Map them through a backend-specific binding.
6. Record them in the route receipt.
7. Test that the backend request changed.

The manifest should not be a universal ComfyUI-flavored DSL. It should have two
layers:

1. **Portable contract:** inputs, outputs, validation, resources, and user-facing
   semantics.
2. **Backend binding:** how this specific backend realizes those semantics.

Example manifest fragment:

```yaml
id: flux2-edit-controlnet
intent: image.edit
backend: comfyui
workflow: workflows/images/flux2-edit-controlnet.json
inputs:
  prompt:
    type: string
    required: true
  denoise:
    type: number
    minimum: 0
    maximum: 1
    default: 0.45
  controlnet_strength:
    type: number
    minimum: 0
    maximum: 2
    default: 0.8
  control_image:
    type: asset
    required: false
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/images/flux2-edit-controlnet.json
    map:
      denoise:
        - node: "31"
          input: denoise
      controlnet_strength:
        - node: "44"
          input: strength
      control_image:
        - node: "42"
          input: image
```

If the backend were A1111/Forge, the binding would not mention nodes:

```yaml
bindings:
  a1111:
    endpoint: /sdapi/v1/img2img
    body:
      denoising_strength: $inputs.denoise
      alwayson_scripts.controlnet.args[0].weight: $inputs.controlnet_strength
      alwayson_scripts.controlnet.args[0].input_image: $inputs.control_image
```

If the backend were a future purpose-built image-edit API, the same portable
contract could bind to ordinary JSON fields:

```yaml
bindings:
  image_edit_service:
    endpoint: /edit
    body:
      prompt: $inputs.prompt
      denoise: $inputs.denoise
      control_strength: $inputs.controlnet_strength
      control_image_asset: $inputs.control_image
```

OpenAI-compatible request shape should stay compatible. If a client/SDK can
carry extra request body safely, gateway-specific controls should be contained
in a namespaced `extensions` object, for example:

```json
{
  "workflow": "flux2-edit-controlnet",
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

For multipart image edits, the same concept can be represented as a JSON form
field such as `extensions`.

For gateway-owned media jobs, use a cleaner native shape:

```json
{
  "intent": "image.edit",
  "workflow": "flux2-edit-controlnet",
  "input": {
    "prompt": "preserve the geometry, watercolor style",
    "image": "asset_123"
  },
  "parameters": {
    "denoise": 0.35,
    "controlnet_strength": 1.1
  }
}
```

## Proving The Feature

Do not prove this by eyeballing generated images.

Prove it with a fake ComfyUI backend and manifest contract tests:

1. Submit an image-edit request with `extensions.com.vividfish.workflow.denoise=0.35`, or a gateway media job with `parameters.denoise=0.35`.
2. Capture the backend request produced by the adapter.
3. Assert the configured backend binding changed to `0.35`.
4. Assert the route receipt lists `denoise` as accepted.
5. Submit an unknown input.
6. Assert the gateway rejects it or records it as ignored according to policy.
7. Submit the same request through an OpenAI-compatible client.
8. Assert compatibility fields still work.

Optional visual regression tests can be added later, but they should not be the
only correctness oracle.

## Unknown Parameter Policy

Silent no-ops are bad product behavior.

For public protocol frontends:

- accept protocol-defined fields
- accept documented gateway extension containers
- reject unknown top-level gateway extension inputs
- record accepted/ignored/rejected inputs in route receipts

For native passthrough lanes:

- allow backend-native shapes when the key has passthrough scope
- label the request as non-portable
- still emit route receipts

## Promotion Criteria

Promote a local extension toward core only when at least two are true:

- two or more users need it
- two or more backend families expose the same concept
- a target app/client already has a field for it
- it can be tested without subjective output judgment
- it affects routing, scheduling, or safety
- it is needed for the SLVP compatibility matrix

Under this rule, denoise/controlnet probably starts as a workflow-manifest
extension. It may later become a shared image-edit convention, but it should not
be a global public protocol promise on day one.
