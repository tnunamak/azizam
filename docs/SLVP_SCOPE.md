# SLVP Scope And Extension Shape

Date: 2026-05-15

This document scopes **Vivid Fish AI Gateway** (the product formerly known as
`openai-proxy`) around the top target users without turning one operator's
current machine into the product spec.

Canonical public host: `https://ai.vivid.fish`.
Compatibility alias: `https://openai.vivid.fish` (kept indefinitely so apps
already configured against the legacy host keep working).

## Implementation Status

The extension ladder, manifest layering, and unknown-parameter policy below
describe the implemented first slice plus the remaining target behavior.
Status on `main`:

- Pure manifest validation and binding helpers exist in
  `gateway/manifests.py`.
- Pure typed request-extension validation exists in `gateway/extensions.py`.
- Pure workflow registry with portable input/output summaries exists in
  `gateway/discovery.py`.
- A mounted `/gateway/v1` router exists in `proxy.py` for workflow discovery,
  capabilities, durable receipts, and media jobs.
- A pure capability model exists in `gateway/capabilities.py`.
- Pure app-profile policy module exists in `gateway/policy.py`.
- A pure app-profile loader for existing key-entry config exists in
  `gateway/profiles.py`.
- Profile-aware image-route receipts run in
  `/v1/images/generations` and `/v1/images/edits`, recording safe field-name
  accepted/rejected/ignored outcomes, manifest identity, durable persistence,
  and body-free firehose events.
- The OpenAI-compatible image handlers accept a namespaced `extensions`
  container for supported workflow-manifest controls and reject unknown
  top-level gateway-native controls instead of silently dropping them.
- `/gateway/v1/media/jobs` provides the first generic durable media-job API;
  asset serving and backend scheduling are still follow-up work.

## Scope Decision

The core product should optimize for three archetypes:

1. Local agent/coding tool power user
2. Multi-backend GPU tinkerer
3. Local multimodal workflow builder

The common need across all three is:

```text
make existing apps and existing engines work together through one reliable
local endpoint, while preserving enough local control to be useful.
```

The product should not promote every local trick into core. It should provide
clear extension points so local tricks can be expressed, tested, and shared when
they become generally useful.

The anti-micromanagement rule:

```text
Profiles select capability metadata.
Capabilities declare metadata.
Per-model/per-workflow allow-lists are overrides.
```

Adding a model/workflow/service should usually mean editing that thing's
manifest or adapter metadata, not every App or Profile. New stable interactive
chat models can become available automatically to chat Profiles. Experimental,
expensive, native, or admin-power capabilities require opt-in.

Community fit:

- r/selfhosted wants ordinary admin UX: OIDC, App keys, revoke, audit, preview.
- r/LocalLLaMA wants model/backend visibility and OpenAI/Anthropic/Ollama
  compatibility without hiding local power.
- r/StableDiffusion wants named workflows with typed controls and dependency
  checks, not a flattened toy image API.
- The SLVP advisor wants this proven through a few complete flows before
  building a broad RBAC/quota/scheduler UI.

## Product Model (Brief)

Detail lives in `docs/CORE_CONTRACTS.md`; the short version the dashboard
should optimize for is:

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

Capabilities, Providers, Workflows, Jobs, Receipts, UsageRecords, resource
claims, service accounts, roles, and audit events are still real internal
facts. They should surface when they answer a concrete operator question, not
as concepts every family member or app integrator has to learn.

Invariant: every executed request resolves an App + Profile + policy decision
before it runs, and produces exactly one body-free Receipt. Anything that
violates that invariant is out of scope for the SLVP.

## What Belongs In Core

Core should include:

- public protocol frontends for OpenAI, Anthropic, and Ollama
- backend adapter contract
- workflow manifest schema
- media job/assets API
- access plane: Users, Apps, Profiles, Keys
- route receipts
- app templates, policy Profiles, and compatibility harness
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
seed/background behavior, and adaptive batching. The current gateway now has a
typed workflow-extension contract for manifest-backed controls. For
`image.edit.flux2-klein-4b`, that includes `denoise`, `steps`, and `guidance`.
It still does not include ControlNet because the local workflow and model
inventory do not contain a real ControlNet path.

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
field such as `extensions`. Asset-valued extension controls can reference an
extra multipart field:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-edit-controlnet \
  -F prompt='preserve the geometry, watercolor style' \
  -F image=@input.png \
  -F control_image=@edge-map.png \
  -F 'extensions={"com.vividfish.workflow":{"control_image":{"multipart_field":"control_image"},"controlnet_strength":1.1}}'
```

The form field `control_image` is upload transport. The portable control remains
`extensions.com.vividfish.workflow.control_image`, and it only works when the
resolved manifest declares and binds that asset input.

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

See `docs/CONDITIONING_CONTROLS.md` for the concrete ControlNet/conditioning
readiness gate and naming convention.
