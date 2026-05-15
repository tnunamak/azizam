# Extension Targets

This document tracks where gateway-specific request controls belong. The rule
is conservative: standard API fields stay top-level, policy stays in profiles,
and backend/workflow knobs only become request extensions when they are
validated against an explicit contract.

## Namespaces

| Namespace | Purpose | Status |
|---|---|---|
| `extensions.com.vividfish.workflow` | Manifest-backed workflow controls for local generation/editing jobs. | Implemented for images; target for audio/video. |
| `extensions.com.vividfish.chat` | Local chat/runtime behavior that is not an OpenAI field. | Proposed. |
| `extensions.com.vividfish.routing` | Per-request routing hints that profiles may allow or deny. | Proposed. |
| `extensions.com.vividfish.telemetry` | Receipt/debug verbosity preferences without prompt/body capture. | Proposed. |

Do not use extensions for credentials, profile permissions, backend URLs,
cloud fallback policy, native passthrough permission, cache TTLs, or status
visibility. Those remain configuration/profile concerns.

## Current Surface

| API | Extension Target | Current State | Notes |
|---|---|---|---|
| `POST /v1/images/generations` | `extensions.com.vividfish.workflow` | Implemented | Binds manifest controls such as `steps`, `guidance`, `seed`, and `batch_size`. Unknown top-level workflow controls are rejected. |
| `POST /v1/images/edits` | `extensions.com.vividfish.workflow` | Implemented | Binds `steps`, `guidance`, and `denoise` for `flux2-klein-4b-edit`. ControlNet is not exposed until a real ControlNet workflow and model are present. |
| `POST /v1/audio/generate` | `extensions.com.vividfish.workflow` | Next | TangoFlux already has a manifest; route-level strict extension validation still needs to be wired. |
| `POST /v1/videos` | `extensions.com.vividfish.workflow` | Next | Video workflows need manifest-backed binding before accepting workflow-specific controls. |
| `POST /v1/chat/completions` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing` | Proposed | Candidate controls: local sampler extras, backend role hints, prompt-cache preferences, fallback preference, receipt verbosity. |
| `POST /v1/responses` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing`, `extensions.com.vividfish.responses` | Proposed | Should share chat extension validation where possible. |
| `POST /v1/completions` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing` | Proposed | Same concerns as chat completions, narrower request shape. |
| `POST /v1/audio/speech` | `extensions.com.vividfish.audio` | Proposed | Candidate controls only if Voxtral exposes stable non-OpenAI voice controls. `voice`, `speed`, and `format` stay top-level. |
| `POST /v1/audio/transcriptions` | `extensions.com.vividfish.audio` | Proposed | Candidate controls only for stable local STT controls not expressible in OpenAI fields. |
| `POST /v1/messages` | Native Anthropic shape first | Proposed bridge | Prefer Anthropic-native fields. Gateway-only controls can be translated from `metadata.vividfish` or an explicitly allowed `extensions` block. |
| `POST /api/chat`, `POST /api/generate` | Native Ollama `options` first | Partial native support | Ollama options are not gateway extensions. Use gateway extensions only for gateway routing/diagnostics. |
| `/gateway/v1/*` | First-class native fields | Implemented | Gateway-native routes should usually use explicit `inputs`, `workflow`, `assets`, and receipt fields instead of an extension envelope. |

## Standard Fields

These should not be extensions:

`model`, `messages`, `prompt`, `input`, `instructions`, `tools`,
`tool_choice`, `response_format`, `text.format`, `reasoning`, `metadata`,
`stream`, `temperature`, `top_p`, `max_tokens`, `max_output_tokens`, `stop`,
`n`, `size`, `quality`, `style`, `background`, `voice`, `speed`, `format`,
and multipart file fields.

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
