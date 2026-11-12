# Train 54 L2 — gateway literals to existing rows

Request: `t54-l2-gateway-literals-to-rows-build-1112-r0`
Repository: `tnunamak/azizam`
Base: `a048a58a12c41cef532ea72a2f95092402c2b10a`
Date: 2026-11-12

## Result and remaining work

**Partial implementation: 50 of the 53 listed literal sites are removed.** A scoped AST comparison matched all 53 baseline sites by file, function/module scope, and exact literal, with no baseline count mismatches. The three retained sites are two occurrences in the Make source requirement, which cannot be correctly integrated through the available runtime interface, and one published input-field name that the brief explicitly permits retaining.

This is not a passing full-repository ratchet result. No budget or reviewed-literal allowance file was read or edited. No root gateway module was opened or imported.

The owner's 17:32 note explicitly requested semantic fixes for four spelling-based decisions. Three are implemented and covered by focused cases: automatic response formatting, receipt usage classification, and explicit video workflow identity. The fourth, Make source validation against the selected input contract, remains blocked as detailed below.

## Changes

### Existing asset-kind rows

Both private prefix maps now call `asset_types.asset_kind_names_by_prefix()`. Home's media-family set uses `ASSET_PREFIX_BY_TOP_TYPE`, and ingestion recognizes the values of its derived prefix map. Shipped mappings remain exactly `img → image`, `vid → video`, and `aud → audio`.

Ingestion's audio kind, stored prefix, filename stem, and provenance kind use the existing audio classification row and the asset-prefix projection. Its video kind comes from the registered operation's model metadata. Audio intent grouping uses `AUDIO_RULE["type"]`, loaded by the existing capability-word reader.

### Existing frontend and capability rows

`discovery_metadata.registered_frontend_routes()` caches a projection from the existing `_load_protocol_frontend_records()` loader. It does not introduce another YAML loader. Operation-intent selection reads the existing `workflow_targets.route_intent` property and rejects missing or ambiguous operations.

A1111 catalog filtering and admission, avatar generation, audio default generation, video jobs, and Make's supported-intent set read these operation declarations. The A1111 default alias comes from the existing capability's `recommended_alias`. The engine-protocol image task pair reads the same declared intent and capability, preserving its prior two accepted task words.

The mesh constant uses its MIME-to-modality declaration and the unique explicit execution intent of frontend routes requiring that capability. It does not construct an intent from a modality name. Missing or conflicting declarations raise.

Default discovery guidance reads the canonical and convenience paths of the protocol explicitly marked default. Shipped discovery JSON and complete `llms.txt` remain equal to baseline in the offline check. An absent or ambiguous default receives explicit discovery guidance rather than an invented route.

### Response format: declared adapter contract

`workflow_call.build_request()` no longer tests an intent's text prefix. It supplies automatic `response_format` only when either:

- the selected request mapping explicitly carries that field in the body/form; or
- the exact method/path frontend declaration lists it in `adapter_request_fields`.

The existing generation, edit, and variation declarations now carry `adapter_request_fields: [response_format]`. Existing `route_inputs` lists are unchanged: workflow-input inventory alone does not authorize automatic adapter defaults. Canonical and alias paths use the same declaration; unknown paths and mismatched methods receive no implicit field. Explicit request values still win over the adapter default.

This intentionally changes the old spelling-based behavior as requested: an unfamiliar intent can use a frontend that declares the field, while an image-like name does not grant the field to an undeclared route.

### Receipt usage: exact execution facts

Usage classification reads the receipt's exact execution intent through `INTENT_CAPABILITIES`. If that word has no capability entry, it can use the modality of that exact registered media-job intent and the modality's existing capability mapping. This preserves the internal `image.identity` job, whose modality row exists but whose intent has no separate capability-word entry.

An intent absent from both tables is explicitly classified as `other`; neither its prefix nor its URL decides its task. Declared task mappings outrank broad modality, so speech transcription/translation are now classified by their declared transcription capability rather than the old speech prefix.

Published usage keys `voice` and `embeddings` remain a small compatibility projection from canonical capability ids `speech` and `embed`. Media labels come from asset-kind rows; other matching labels come from capability rows. The distinct published usage labels `Embeddings` and `Rerank` remain literal wire/presentation compatibility values.

### Video identity: explicit record only

`workflow_id_for_video_key()` accepts an explicit registered identity or a key present in the existing compatibility mapping. It returns `None` for an unregistered key; it no longer invents a dotted identity.

The existing test that expected a fabricated identity for an owner-added key was updated to the owner's new contract. The in-scope ingestion caller already returns `str | None`, and its unknown-model provenance path is covered. Other callers outside the allow-list were not inspected; the return type change requires their ordinary repository integration checks before merge.

## Blocked Make source-contract integration

- `gateway/me_make.py:416` `image.edit` — retained comparison; the owner requires the selected workflow input contract to decide source requirements.
- `gateway/me_make.py:418` `image.edit` — retained error-copy selection belonging to the same unresolved condition.

The scoped `MakeRuntime` interface exposes `workflow_id(intent, model)` at lines 105–110, but no callable returning the selected workflow record or input contract. Its root implementation is outside scope and was explicitly forbidden to open. Neither an optional invented method nor a fallback based on the intent spelling would satisfy the owner's requirement.

An owner question requested an existing callable or a narrowly authorized integration seam. No answer was present in the last checked owner notes when this report was prepared. The patch therefore leaves this decision visibly incomplete. Completing it needs the actual selected contract and its source-input requirement, wired through the runtime before enforcing the Make request. No speculative runtime API or policy row was added.

## Retained public contracts

The requested notation is `file:line literal - reason`:

- `gateway/workflow_openapi.py:75 "image" - published contract input name filled by input_reference; it is not a media-family selector and must not be renamed or replaced with a generic kind.`
- `gateway/me_media.py:57 "voice" / "embeddings" - published usage keys, deliberately distinct from canonical capability ids; kept as compatibility projection values.`
- `gateway/me_media.py:2505 "Embeddings" - published usage presentation differs from the capability catalog label Document search.`
- `gateway/me_media.py:2506 "Rerank" - published usage presentation differs from the capability catalog label Reranking.`

The last three entries document retained compatibility vocabulary rather than additional unresolved members of the supplied 53-site list. Other unrequested protocol field names and existing engine-adapter wire values were not swept or renamed.

## Verification

**25 offline checks passed; 0 failures, 0 errors, 0 skips.**

This comprises **22 new regression cases** in seven existing allowed test files plus three structural/equivalence checks:

| Existing test file | Added cases | Coverage |
|---|---:|---|
| `tests/test_discovery_metadata.py` | 7 | Shipped operation values, unfamiliar/missing/ambiguous operations, explicit capability intent, exact method/path fields, separation from workflow input inventory, default-row paths, unchanged shipped copy |
| `tests/test_media_ingestion.py` | 3 | Existing prefix/kind behavior, unfamiliar declared prefix, unknown video provenance identity |
| `tests/test_home_api.py` | 4 | Exact receipt tasks including declared internal jobs, unknown lookalikes/path guesses, unfamiliar declared task, preserved usage names/labels |
| `tests/test_video_limits.py` | 2 | Explicit key/identity mappings and unregistered names |
| `tests/test_a1111_api.py` | 3 | Declared frontend response field for unfamiliar intent, no field from familiar spelling, explicit request-map field and request precedence |
| `tests/test_audio_media_jobs.py` | 2 | Existing audio defaults/aliases and row-selected default intent |
| `tests/test_mesh_profile.py` | 1 | Explicit mesh capability/intent linkage |

The additional checks parsed all **26 copied Python files** and **5 scoped YAML documents**, and compared full default discovery JSON and `llms.txt` with baseline. Script runners in existing unittest files were moved after the added classes so direct execution can discover them.

### What the offline run did and did not execute

The offline harness imported the actual pure asset-types and discovery modules against the scoped rows. It executed the new test cases against selected functions/constants compiled from patched source for ingestion, usage classification, audio shaping, request construction, capability loading, and video identity. External boundaries were explicit fixtures; the video compatibility map was an empty fixture, with identity tests injecting their own declarations. No engine, provider, model, network execution path, root gateway import, or T3 thread was launched.

This is **function-isolation verification**, not a repository pytest run. The local Python environment has no pytest, and the scoped scratch tree intentionally omits the rest of the repository. Existing tests that import the root gateway or other unlisted modules could not be run as full test modules here.

The bridge lists check ids but does not disclose their command definitions. No opaque `default`, benchmark, paid, credential, or provider-related check was launched. No approval denial occurred; no denied action was retried.

The full `tests/test_domain_literal_ratchet.py` script reads the prohibited root gateway, budgets/allowances, and out-of-scope manifest/protocol trees. It was not run. Instead, the bounded AST comparison counted the exact 53 assignment-listed sites: **50 removed, 3 retained, 0 baseline mismatches**. This does not certify the global ratchet, allowance freshness, module import graph, strict downstream frontend-row validators, or full OpenAPI integration.

## Independent review

A second source review found no confirmed added defect. Its isolated checks covered all 56 declared capability-word/intent mappings, the internal identity intent and five unknown names, four operation intents, mesh intent, all six generation/edit/variation canonical and alias paths, method/unknown-route rejection, and one YAML read across repeated cached helper calls. These remain isolated checks, not full repository test receipts.

Root also checked the L2 and L5 edits to their shared pinned protocol_frontends.yml with a three-way merge: no conflicts, preserving four adapter-field declarations and the separate MCP/job-profile changes. No combined file was applied to either assignment workspace.

## Review and completion requirements

The patch touches only allowed source/test paths and this notes output. No budget, allowance, manifest, engine/model record, or root gateway change is included.

Before merge, run the normal authorized repository module tests and ratchet, validate the new adapter-field row property through downstream registry consumers, and check callers of the optional video-identity result. Make source-contract enforcement remains an explicit outstanding requirement, so this patch must not be represented as full completion of the owner's four semantic fixes.

**Expect it to work because:** the replacements read the same existing asset, operation, modality, capability, and alias rows that already own the shipped values. Focused tests preserve those values and public presentation, while unfamiliar-name cases demonstrate that the three completed semantic decisions follow declarations instead of spelling. The unresolved Make integration is kept visible rather than hidden behind another guessed selector.
