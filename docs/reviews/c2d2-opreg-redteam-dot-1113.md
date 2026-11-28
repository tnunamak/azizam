# Operation registry draft 1: independent red-team review

Base: `tnunamak/azizam@2f6bcfeff3faa3cde47c605e196c819477860f78`.
Method: static review of the assignment's allowed registry files and bounded gateway source windows. No code execution, provider calls, model APIs, tests, publication or deployment. Only this notes file is changed.

## Scope and evidence limitations

The requested exhaustive search of registry, gateway and web/src exceeds the explicit file allowlist. No web/src or unlisted repository files were read. Dalan returned **file not readable** for the two draft-cited proxy.py windows (14960 and 34210); its dynamic read-tool registry was empty, so no scoped search was available. The proxy claims and exhaustive conflict inventory remain unverified. Several source lines were automatically redacted by the connector; no attempt was made to recover them. The notes below are sufficient to reject building draft 1 unchanged, but not a substitute for W0's complete measured inventory.

## Ranked findings

### P0: One context-free resolver cannot preserve operation, presentation and authorization semantics

The design calls every mapping an alias, but several mappings are intentionally not equivalences. Replacing them with a single operation row can change access decisions, not just labels.

Evidence:
- `registry/protocol_frontends.yml:435-447` explicitly says rerank shares the text.embeddings admission intent so a key allowed to embed may rerank; the route intent is rerank.
- `registry/capability_words.yml:138-139` maps rerank and text.rerank to the rerank family, not embeddings.
- `registry/models_catalog_operations.yml:94-102` advertises text.rerank and /v1/rerank.
- `registry/local_capability_rows.yml:48-54` deliberately has a non-publishing rerank row with null metadata; `gateway/local_capabilities.py:212-231` preserves unknowns and omits it from published aliases.
- `registry/protocol_frontends.yml:428-433` has videos.edit, admission video.edit, but runtime-policy video.remix. Lines 450-457 similarly separate video.extend from policy video.generate.
- `gateway/capability_ids.py:318-328` separates endpoint support from admission: both completion paths admit chat.generate.
- `gateway/engine_protocol/dispatch.py:85-91` treats an offering task as an execution restriction, not display metadata. A family match must not implicitly grant every child operation.

**Change:** canonical operation identity and capability membership may be unified as data, but retain typed relations: display classification, wire spelling, catalog grouping, policy/admission intent, receipt intent, and execution support. Use explicit resolver context/expected type. Freeze current policy decisions until separately approved. No alias expansion may widen an offering's executable operations. The migration agreement oracle must compare each legacy consumer's projection, not assert all legacy tables return the same value.

### P1: Rule 2 merges three different type systems and is not their union

`registry/asset_kinds.yml:1-5,14-20` describes stored-file MIME top-level kinds, allows unknown kinds, and includes **model**, which the proposed closed list omits. `registry/capability_words.yml:4-7` explicitly distinguishes wire modalities from MIME and maps model/gltf-binary to mesh. `gateway/model_metadata.py:50-61,131-142,188-193` has separate model types, input modalities, output modalities and tags; `registry/capability_intent_metadata.yml:45-67` has ordered derivation and language/vision implications.

**Change:** keep separate fields for semantic value schema, modality, asset media type/storage handling and parameter role. Preserve model-to-mesh mapping rather than renaming persisted assets. Embedding is a meaningful public metadata modality today (`capability_intent_metadata.yml:15-16`), but internally its value is a vector/tensor schema with shape/dtype, not a MIME file kind. Do not erase the published embedding word in this refactor.

A mask is an image with role/constraints; a texture is an image related to a mesh; subtitles are timed structured text or a file with a specific media type. None requires a new storage primitive just to name a role. A voice reference is a typed identifier, not an audio blob: `protocol_frontends.yml:128-131` describes a voice name or custom voice id, and :84-104 declares distinct voice and consent resources. Current paths requiring masks/textures/subtitles were not verifiable outside the allowlist; do not invent support.

### P1: Typed modalities are not enough to replace input/output contracts or operation handlers

`models_catalog_operations.yml:17-20` uses prompt for completions, :10-13 uses messages for chat, :25-28 uses input for embeddings, and :98-102 uses query/documents for rerank. All can involve text/JSON but need different shapes. `protocol_frontends.yml:119-163` includes constraints, required fields and additionalProperties behavior for speech. `capability_intent_metadata.yml:7-8` classifies image.analyze as image despite text output. Output kind cannot determine operation semantics, policy, parser or public response envelope.

**Change:** typed named ports should reference full request/result schemas (constraints, cardinality, identifier/asset roles), with reviewed handler or adapter contracts. Keep serialization and semantic validation at the appropriate frontend/adapter boundary. One serializer per output shape can be shared; one executor per output kind cannot replace operations.

### P1: Public route presence does not mean job-submittable, nor does one operation have one route/mode

`media_job_intents.yml:10-20` separately declares client submission and internal jobs. image.edit is non-submittable (:11), yet has a public image edits route in `protocol_frontends.yml:328-341`. Thus the proposed image.edit example with a public route plus the rule “submittable = has public” widens the job submission contract. image.identity has an internal execution_route and no client submission (:12). Multiple audio intents share one route/component (:14-16). `gateway/media_job_intents.py:46-58,67-82` preserves explicit eligibility, ordering and grouping.

Speech is sync in `local_capability_rows.yml:29-35`, while its MCP frontend accepts auto/sync/async (`protocol_frontends.yml:170-177`). Route aliases and different HTTP methods share paths, e.g. voice routes (:70-104), and video create/list/delete (:375-396,512-530). A scalar public.route and mode cannot capture these bindings.

**Change:** retain explicit media-job submission eligibility; describe transport bindings as ordered method/path/representation records with allowed execution modes, independent of logical operation identity.

### P1: “Byte-identical each train” is false as written; step 2 is an intentional wire change, step 3 hides larger risks

Step 2 introduces a new 422 unknown_operation and rejects previously accepted inputs. `gateway/capability_ids.py:331-334` deliberately passes unknown words through; :350-355 documents an openai compatibility marker that is not refused as a capability. Converting every word to an operation reference will reject markers unless separately modeled. Read-before-rewrite migration reports cannot establish that all future old clients are compatible.

Step 3 can additionally change:
- authorization and receipt intents (P0 above);
- published schema/description ordering, derived directly from media-job rows (`gateway/media_job_intents.py:47-58,67-90`);
- model metadata enum order and operator override precedence (`gateway/model_metadata.py:10-15,131-142`);
- catalog prefix grouping, normalized synonyms, and example order (`gateway/models_catalog.py:113-147`);
- published usage keys voice/embeddings, deliberately distinct and explicitly not task selectors (`gateway/me_media.py:56-58`).

**Change:** separate compatibility refactors from semantic migrations. Start shadow validation; measure unknowns and every mapping disagreement. Offer extension registration before requiring every existing owner word to resolve. Preserve old error behavior and serialized fields in compatibility trains, and version deliberate new validation. Golden tests must include denied and allowed policy cases, absent/null metadata, historical receipts, unsupported routes, alias normalization and ordering, not just happy-path examples.

### P2: Owner-extensible names make the literal ratchet unstable

`gateway/models_catalog.py:123-127` normalizes case/underscores, while `gateway/capability_ids.py:331-334` only strips whitespace. Exact alias freedom is not enough: aliases must be collision-checked under each legacy resolver's normalization. `models_catalog_operations.yml:15` even includes text, a ubiquitous schema word; `capability_words.yml:5,68-76` similarly overlaps modality audio and capability audio. A guard rejecting any code literal that resolves through a mutable owner registry will flag unrelated schema/storage words and can change behavior with owner data.

**Change:** lint canonical-ID uses in known semantic positions against a frozen shipped registry. Keep domain spellings in data and reviewed wire adapters; do not globally outlaw strings. Owner namespaces need stable identities, normalized collision rules, reserved namespaces, immutable meaning/version policy and atomic validation of all references. Display categories must never become implicit policy grants.

## Problem table fact check

| Draft row | Assessment at pinned input |
|---|---|
| capability_words.capabilities | Correct presentation fields, but policy_protocol, suppression and classification are not merely labels. 12 rows observed. |
| protocol_words + route_intents + type_capabilities | 35 + 22 + 6 entries observed (63 rows, not 63 necessarily unique names); approximately 60 is fair. One route-intent line is redacted. These map to families, not a reversible operation identity. |
| local_capability_rows.alias_operations | Correct but incomplete: workflow_families, backend_rows, image_rows/defaults also carry intents/modalities; :63-179. Null/non-publishing rows and shared_operations matter. |
| models_catalog_operations | Correct; it has ordered prefix sets and examples, not always one logical operation. videos groups generate and edit (:81-84). |
| media_job_intents | Correct plus execution_route and explicit client eligibility; cannot derive eligibility from route presence. |
| intent/service metadata | Correct filenames: capability_intent_metadata.yml and service_capability_metadata.yml. Includes ordered semantic derivation, augmentation and overrides, not just duplicated kinds. |
| protocol_frontends | Correct, but multiple intent roles, control/discovery/content/unsupported operations and binding-specific modes must survive. |
| protocol descriptions operations/serves | Dispatch confirms operations/class and task checks (:81-91); description definitions not allowlisted, so no exhaustive claim. |
| service capabilities/offerings tasks “free, unchecked” | Overstated: dispatch :85-91 restricts tasks and capability_ids :350-360 imposes transition support checks. Write-schema validation not inspected, so “unchecked at write” remains unverified. |
| code reconciliation sites | dispatch aliases, MODEL_KIND_LABELS, INTENT constants and usage aliases verified. proxy positions and content if/elif not readable, so cannot confirm their present contents. Usage aliases are compatibility output names, not operation selectors. |

Additional inventory sites directly evidenced by allowed source:
- INNER_HOP_ENDPOINTS and INNER_HOP_ADMISSION: `gateway/capability_ids.py:318-328`.
- All local backend/workflow/image row families: `registry/local_capability_rows.yml:63-179`.
- MIME mapping and contextual audio rule: `registry/capability_words.yml:4-7,179-186`.
- `gateway.discovery_metadata.operation_intent` is imported and used at `gateway/engine_protocol/dispatch.py:14,88`; its source needs a scoped follow-up.
- `gateway.intent_families.resolve_intent`, manifest asset typing, product_contracts schema vocabulary, and stable_aliases OPERATIONS: imports at `gateway/model_metadata.py:29-32`. Need follow-up, not assumed implementation details.
- `registry/media_job_intents.schema.json` is a runtime validation dependency at `gateway/media_job_intents.py:20-39`; `defaults/models.schema.json` is named at `registry/capability_intent_metadata.yml:45`.
- Frontend route operation, runtime_policy, execution_intents, azizam_policy, MCP input schema/response_kind/accepted_modes, asset-kind policy and workflow selection predicates all carry distinct semantics; e.g. `protocol_frontends.yml:285-341,480-510,1087-1093,1368-1382`.

## Observed conflict/ambiguity ledger (not exhaustive)

| Spelling/group | Conflicting contexts and evidence | Decision |
|---|---|---|
| chat | capability_words:9,114; local_capability_rows:3-8; catalog_operations:6-13; dispatch:89 | capability context versus chat.completions operation context; retain both typed projections. |
| completions | local_capability_rows:3-5 shares chat metadata; :90-98 backend is text.completions; catalog_operations:14-20 is text.completions; service_capability_metadata:5-6 maps to chat | Explicit legacy context resolver; do not turn text completion into chat completion. |
| speech, tts, audio.speech | capability_words:34-42,118-121; local_capability_rows:29-35; frontend:107-109,185-192 | Speech family versus speech.synthesis; wire aliases separate from policy. |
| image, images | capability_words:16,128-132; catalog_operations:29-36; local_capability_rows:14-18; dispatch:88 | image family versus image.generate; never grant editing through generic generation task. |
| image.edit / images.edit | capability_words:132,164; catalog_operations:37-44; frontend:330-341; media_job_intents:11 | Operation is edit; family projection image; public edit does not enable client job submit. |
| audio / audio.generate | capability_words:68-76,144-145; catalog_operations:45-52; local_capability_rows:43-47,70-76 | Family grouping vs audio generation; preserve music/sfx distinctions and shared route. |
| video, videos | capability_words:26,133-135; local_capability_rows:24-28; catalog_operations:81-93 | Family/group can span generation/edit; single-op alias loses catalog set. |
| transcriptions, translations | local_capability_rows:36-42 shares metadata; :127-148 separate intents; capability_intent_metadata:25-28 shares metadata; catalog_operations:61-80 distinct routes | Share schemas when identical, not operation identity or translation support. |
| rerank / text.rerank | P0 sources above | Canonical rerank operation with explicit legacy embeddings policy projection, not embeddings alias. |
| video.edit / videos.edit | frontend:428-433; catalog_operations:92-93 | Preserve separate edit operation and legacy remix policy mapping. |
| video.extend / videos.extend | frontend:450-457; media_job_intents:18 | Extend operation, independent receipt/admission and generation policy. |
| image.variation / images.variations | capability_words:155; media_job_intents:13; frontend:542-555,1290-1292 | Variation identity/admission vs legacy image.edit policy. |
| embedding / embeddings | capability_words:136-137,161,176; catalog_operations:21-28; local_capability_rows:9-13; me_media:56-58 | Model metadata modality, text.embeddings operation, embed family and usage projection are typed relations. |
| vision | capability_words:117; service_capability_metadata:13-17; local_capability_rows:87-89 | Input augmentation/classification, not a distinct executable operation grant. |
| mesh / model | capability_words:7,103-111,146; asset_kinds:18 | Semantic modality versus MIME/storage classification, not competing IDs to normalize away. |

Also inventory non-generation route identities before rule 3: models.list/retrieve, voices.list, assets.content/delete, responses.retrieve/delete/cancel, unsupported.* and engine.native. See `protocol_frontends.yml:53-104,350-370,468-478,532-540,588-646,1187-1189`. They cannot all inherit a generate operation's typed I/O or authority.

## Prior art

1. Hugging Face Transformers pipelines have task-specific classes and explicit aliases (e.g. sentiment-analysis for text-classification). Copy stable canonical tasks plus reviewed aliases and task-specific schemas; avoid treating a broad modality as an executable contract. This is an analogy, not evidence that HF solves Azizam authorization. [Official pipeline documentation](https://huggingface.co/docs/transformers/en/main_classes/pipelines).
2. Replicate publishes model input/output schemas separately from its HTTP API schema. Copy full structured value contracts rather than modality-only ports, and preserve model/version-specific shape information. It is not evidence for one universal global task-alias namespace. [Official OpenAPI/model schema documentation](https://replicate.com/docs/reference/openapi/).

## Simpler alternative and migration

Yes. One canonical operation catalog plus typed relationships is simpler than making every legacy table into aliases and then adding exceptions:
1. Define operation IDs, capability membership and request/result schema references. Keep presentation and MIME/storage dictionaries separate.
2. Preserve reviewed, context-keyed compatibility projections: legacy alias, frontend binding, policy intent, receipt intent, catalog group. Generate old reader-shaped maps first.
3. Shadow-compare every projection and policy decision. Record ambiguity decisions as versioned data. No global resolver equivalence assertion.
4. Migrate one reader at a time without changing wires. Delete only redundant source rows, not independent binding/policy facts.
5. Register owner extension operations before enforcing closed references. New validation, public operation IDs and output-contract upgrades get separately versioned changes.

Required acceptance before implementation approval: complete W0 inventory including inaccessible proxy and scoped web/schema consumers; conflict fixtures for the ledger; positive and negative policy matrix; job eligibility and route/mode fixtures; metadata and usage-key snapshots; historical receipt reads; normalized alias collision checks; extension install/update rollback and unknown-ID behavior.

## Open questions

1. **Public IDs:** eventually yes, stable versioned operation IDs, but not by replacing current receipt intents or catalog words. Add explicitly versioned fields/projections only in a deliberate wire change; retain historical intent readability.
2. **Owner kinds:** allow owner-defined schemas and parameter roles over reviewed storage/transport primitives, not arbitrary executable storage kinds or MIME handlers. New primitive handlers require review. Existing unknown MIME asset behavior must not be silently removed.
3. **Categories:** attach presentation categories to typed targets, including models/workflows today and operations/capabilities when useful. Do not infer authorization or execution support from category membership; keep optional display inheritance explicit.

Validation: static cross-reference only; zero tests run, no byte-equivalence claim. Dalan offered no available checks. The requested broad inventory is partially blocked as stated above.

## Verdict

**Build after named changes:** replace the context-free alias rule with typed compatibility projections; separate semantic schemas from MIME/storage kinds; retain explicit submission eligibility and frontend/policy/receipt bindings; split compatibility refactors from behavioral migration; complete W0 coverage before deleting source tables. Do not build draft 1 as written.
