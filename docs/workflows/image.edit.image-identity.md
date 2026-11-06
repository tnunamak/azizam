# Identity Scene (real people into a new scene): workflow notes

Workflow `image.edit.image-identity` (manifest `manifests/workflows/image-identity.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

Put the real people from one or more reference photos into a new scene or pose and return the best of N seeds with objective likeness scores. The gateway crops a roomy head per person from the ORIGINAL photo (never a tiny face tile), tags every input <imageN> for Qwen-Image 2.1 multi-reference edit, runs up to `attempts` seeds, scores each result with ArcFace (buffalo_l + antelopev2, CPU) against the real faces, and keeps the best. Served at POST /gateway/v1/images/identity (typed; async media job by default, sync with mode=sync, JSON with `likeness`) and at POST /v1/images/edits with model=image-identity (options in extensions["com.azizam.workflow"]). Infant faces are scored but flagged reliability=low: the score only ranks candidates, so verify a baby by eye. Measured on a family photo: 0.60 adult, 0.48 infant (docs/IDENTITY_LIKENESS.md).
