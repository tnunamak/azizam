# Extension Targets

This document tracks where gateway-specific request controls belong. The rule
is conservative: standard API fields stay top-level, policy stays in profiles,
and backend/workflow knobs only become request extensions when they are
validated against an explicit contract.

## Namespaces

| Namespace | Purpose | Status |
|---|---|---|
| `extensions.com.azizam.workflow` | Manifest-backed workflow controls for local generation/editing jobs. | Implemented for image, audio, and video workflow routes. |
| `extensions.com.azizam.prompts` | `mode: "forget"` disables prompt retention for one request. | Implemented for media generation, speech, and Responses routes. |
| `extensions.com.azizam.chat` | Local chat/runtime behavior that is not an OpenAI field. | Proposed. |
| `extensions.com.azizam.routing` | Per-request routing hints that profiles may allow or deny. | Proposed. |
| `extensions.com.azizam.telemetry` | Receipt/debug verbosity preferences without prompt/body capture. | Proposed. |

Do not use extensions for credentials, profile permissions, backend URLs,
cloud fallback policy, native passthrough permission, cache TTLs, or status
visibility. Those remain configuration/profile concerns.

The live discovery contract is:

- `GET /gateway/v1/extensions` for extension namespaces and route support.
- `GET /gateway/v1/models/{model}/capabilities` for model-specific controls.
- `GET /gateway/v1/workflows` and `GET /gateway/v1/capabilities` for the
  broader workflow catalog.
- `docs/CONDITIONING_CONTROLS.md` for ControlNet/IP-Adapter/style-reference
  conventions and readiness gates.

## Current Surface

| API | Extension Target | Current State | Notes |
|---|---|---|---|
| `POST /v1/images/generations` | `extensions.com.azizam.workflow` | Implemented | Binds direct workflow controls such as `steps`, `guidance`, `seed`, `batch_size`, `negative_prompt`, `sampler`, `scheduler`, and `seam_blending` on `sdxl-seamless-tile` (`0..0.5`). Also supports gateway-composed controls on `flux2-klein-4b-seamless-tile`, including `seam_mask_width`, `seam_mask_expand`, `seam_mask_feather`, `seam_steps`, `seam_guidance`, `seam_denoise`, and `seam_prompt`. Unknown top-level workflow controls are rejected. |
| `POST /v1/images/edits` | `extensions.com.azizam.workflow` | Implemented | Binds `steps`, `guidance`, and `denoise` for `flux2-klein-4b-edit`. Top-level `mask` is a standard OpenAI multipart field, not an extension; masked requests auto-select `flux2-klein-4b-inpaint` when available, which applies the normalized mask inside ComfyUI and exposes `seed`, `mask_expand`, and `mask_feather`. Future conditioning controls such as `control_image` and `controlnet_strength` stay namespaced and manifest-backed; multipart extension assets can reference an extra form field with `{"multipart_field":"control_image"}`. ControlNet is not exposed until a real ControlNet workflow and model are present. |
| `POST /v1/audio/generate` | `extensions.com.azizam.workflow` | Implemented | ACE-Step accepts top-level `tags` and `lyrics`; `prompt` remains an alias for ACE-Step `tags` when `tags` is absent. TangoFlux accepts namespaced `steps`, `guidance`, and `batch_size`; standard `duration`, `seed`, `tags`, and `lyrics` stay top-level route-managed fields. |
| `POST /v1/videos` | `extensions.com.azizam.workflow` | Implemented | LTX 2.3 distilled accepts namespaced `steps`, `stage1_cfg`, and `stage2_cfg`; standard `size`, `seconds`, `seed`, prompt, and image-reference fields stay top-level. |
| `POST /v1/chat/completions` | `extensions.com.azizam.chat`, `extensions.com.azizam.routing` | Proposed | Candidate controls: local sampler extras, backend role hints, prompt-cache preferences, fallback preference, receipt verbosity. |
| `POST /v1/responses` | `extensions.com.azizam.chat`, `extensions.com.azizam.routing`, `extensions.com.azizam.responses` | Proposed | Should share chat extension validation where possible. |
| `POST /v1/completions` | `extensions.com.azizam.chat`, `extensions.com.azizam.routing` | Proposed | Same concerns as chat completions, narrower request shape. |
| `POST /v1/audio/speech` | `extensions.com.azizam.audio` | Proposed | Candidate controls only if Voxtral exposes stable non-OpenAI voice controls. `voice`, `speed`, and `format` stay top-level. |
| `POST /v1/audio/transcriptions` | `extensions.com.azizam.audio` | Proposed | Candidate controls only for stable local STT controls not expressible in OpenAI fields. |
| `POST /v1/messages` | Native Anthropic shape first | Proposed bridge | Prefer Anthropic-native fields. Gateway-only controls can be translated from `metadata.azizam` or an explicitly allowed `extensions` block. |
| `POST /api/chat`, `POST /api/generate` | Native Ollama `options` first | Partial native support | Ollama options are not gateway extensions. Use gateway extensions only for gateway routing/diagnostics. |
| `/gateway/v1/*` | First-class native fields | Implemented | Gateway-native routes should usually use explicit `inputs`, `workflow`, `assets`, and receipt fields instead of an extension envelope. |

ControlNet readiness: there is an audit trail for ControlNet, but the current
local system is not ready. Do not expose fake `controlnet_*`, conditioning,
adapter, or preprocessor request controls until a real workflow, model,
manifest, and readiness check are present. When support is present, put those
controls under `extensions.com.azizam.workflow` or a gateway-native media job
shape, not as global OpenAI-compatible fields.

Prompt memory is a gateway privacy decision, not a workflow input. Use
`{"extensions":{"com.azizam.prompts":{"mode":"forget"}}}` before sending a
media or Responses request. The gateway validates the exact value and removes
the control before backend dispatch. Responses `store:false` has the same
retention effect and keeps its OpenAI meaning: no server-side response row.
An async media request made with `mode:forget` runs from process memory; it
cannot resume after a gateway restart. No prompt text or workflow graph is
recorded in receipts or media-job metadata.

Variant semantics are not a separate extension family. Use top-level `n` where
the public protocol owns batch count, and use manifest-backed `seed` or
route-native `seed` where the selected route already supports it. Do not add
ad hoc extension names such as `variant_count` or `variations` unless a
gateway-native route defines them explicitly.

## Standard Fields

These should not be extensions:

`model`, `messages`, `prompt`, `input`, `instructions`, `tools`,
`tool_choice`, `response_format`, `text.format`, `reasoning`, `metadata`,
`stream`, `temperature`, `top_p`, `max_tokens`, `max_output_tokens`, `stop`,
`n`, `size`, `quality`, `style`, `background`, `voice`, `speed`, `format`,
and multipart file fields such as image-edit `image` and `mask`.

## Implementation Standard

Every extension-bearing route should:

1. Accept only namespaced controls.
2. Reject unknown top-level workflow controls instead of silently dropping them.
3. Validate names, types, ranges, and enums against a manifest or explicit spec.
4. Mutate only documented backend fields.
5. Record accepted/rejected/ignored names and reasons, never raw prompts,
   media bytes, tokens, credentials, or extension values.
6. Expose supported controls through `/gateway/v1/workflows`,
   `/gateway/v1/capabilities`, and the public discovery docs.
