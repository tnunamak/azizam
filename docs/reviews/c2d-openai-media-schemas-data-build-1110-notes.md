# OpenAI media schema extraction

Base: `638af7b7f7790fc958d7bb68dd9c0643d83d2e74`. Assignment: `c2d-openai-media-schemas-data-build-1110`.

## Status and acceptance blocker

The scoped extraction is supplied, but the assignment is **blocked for acceptance**, not verified complete. The brief simultaneously requires byte-identical golden exports and a verified upstream deletion response. The published deletion discriminator is `video.deleted`; the base export says `video`. The excluded root route owner was not read, so changing advertised output cannot establish runtime consistency. A question was sent through bridge progress; no answer was received before sealing. This patch preserves compatibility and explicitly records the discrepancy rather than claiming it is correct. No permission, billing, destination, deployment or runtime changes were made.

The base discrepancy is `gateway/openai_media_schemas.py:696`. In this patch it is preserved at `registry/openai_media_schemas.yml:231-235`; the export reads that data at `gateway/openai_media_schemas.py:795-800`. The independent published fixture is `tests/test_openai_media_schemas_data.py:67-74`; comparison explicitly records nonconformance at lines 119-124. It is not a passing upstream-conformance assertion.

## Independent protocol evidence

Retrieved 2026-11-10. These sources are independent of the repository dictionary and production registry:

- [OpenAI published DELETE response](https://developers.openai.com/api/reference/resources/videos/methods/delete), Returns `object` field and response example, specifies `video.deleted`. The historical reference currently notes the upstream Videos API shutdown; this review concerns a compatibility gateway, not upstream service availability.
- [OpenAI Python v2.30.0 deletion response](https://raw.githubusercontent.com/openai/openai-python/v2.30.0/src/openai/types/video_delete_response.py), line 18, independently pins the same discriminator.
- [OpenAI Python v2.30.0 edit parameters](https://raw.githubusercontent.com/openai/openai-python/v2.30.0/src/openai/types/video_edit_params.py), lines 13-27, pins required prompt, video, reference id, and file/reference alternatives.
- [OpenAI Python v2.30.0 extension parameters](https://raw.githubusercontent.com/openai/openai-python/v2.30.0/src/openai/types/video_extend_params.py), lines 14-34, pins required prompt, seconds and video plus file/reference alternatives.
- [OpenAI Python v2.26.0 image edit parameters](https://raw.githubusercontent.com/openai/openai-python/v2.26.0/src/openai/types/image_edit_params.py), lines 14 and 25, requires image and prompt; optional fields are at lines 48-104. The gateway requires only prompt, preserved at `registry/openai_media_schemas.yml:49-51` and explicitly noted by the test at lines 99-103.
- [OpenAI legacy image variations reference](https://developers.openai.com/api/reference/resources/images/methods/create_variation), Body Parameters, pins image upload, n range 1-10, response format spellings and optional fields. Its retired status does not justify changing this gateway's compatibility contract.

`test_media_schema_fragments_match_pinned_wire_contract` at `tests/test_openai_media_schemas_data.py:90-124` compares the advertised subset of fields/types and both video media alternatives with handwritten protocol projections. It does **not** claim full upstream schema equality. Besides deletion, the gateway intentionally remains as found: image editing omits required image, extension permits numeric seconds and omits required seconds, exposes context_seconds, and its multipart extension schema omits seconds entirely. See test lines 99-118 and registry lines 154-216. Upstream schemas include other fields, enums, limits and nullability that these permissive gateway schemas never advertised. Review any corrections together with the route owner in a separately scoped task.

## Extraction and validation

- Five HTTP method/path entries, six request content alternatives and one response status are at `registry/openai_media_schemas.yml:8-250`. The requested new registry is the only authorized data output; no other repository data files were read. The allowed source had no schema-fragment reader. Its content is ordinary JSON-compatible YAML with version 1 at line 6, no executable import paths or reference resolution.
- Only the target request dictionaries and deletion response moved. Thin reader sites are `gateway/openai_media_schemas.py:504-505`, `522-523`, `795-800`, `818-819`, and `830-831`. Existing schema-producing function names, all nineteen exported OpenAPI dictionaries, response envelopes and wire spellings remain unchanged by construction. The new data file must ship beside the module's repository parent; the lead must verify packaging includes it.
- The closed loader is at `gateway/openai_media_schemas.py:32-165`. Safe YAML and duplicate-key refusal are at lines 32-48, closed keyword/type validation at lines 51-105, MIME wrappers at lines 108-116, version/method/path/status/envelope validation at lines 119-153, and defensive deep copies at lines 156-160. No registry field can select a Python import, callable or external schema reference. Unsupported schema keywords fail rather than being silently discarded. This is a small supported schema dialect, not a general JSON Schema validator.
- The unrelated root gateway module, manifests, reviewed allowances, package configuration and budget file were not read or edited. `tests/test_domain_literal_ratchet.py` was read only. No neighboring assignment's files were edited.

## Golden-first evidence and cases

The test file was applied through the bridge **before** the production patch. The first receipt was test-only diff SHA `badad2a159a4ceb91841a252250688736ebd1489c438e16d93859e30671eee3f`.

The base-only, inspected schema module was evaluated to materialize literal fixtures; this was fixture generation, not a test or validation run. Golden coverage at `tests/test_openai_media_schemas_data.py:24-47` compares sorted-key JSON bytes for **39 outputs**: nineteen public OpenAPI dictionaries, the error schema, fourteen zero-argument schema/helper returns, the translation variant, error wrapper and three request-wrapper variants. Literal expected JSON starts at line 219. The golden values and assertions were not changed after extraction.

Added case count: **32**, with no existing tests removed or changed:

- 1 golden case (lines 24-47) and 1 independent wire-subset/known-deviation case (90-124). Expected to pass on the base by fixture construction/source reasoning; not executed.
- 1 unfamiliar endpoint case (144-148), 1 mutation-isolation case (151-158), 11 envelope refusal cases (161-181), 12 schema refusal cases (184-197), 4 malformed/unsafe/ambiguous YAML cases (200-210), and 1 missing-file case (213-215). All 30 are expected to fail on the base with missing `_load_schema_fragments`/`_endpoint_fragment`, before reaching their new behavior assertions. They are expected to pass after implementation, but this was not run.

No test, lint, compilation, ratchet, test collection, repository check, live inference API call, model call or metered operation was run. Patch application receipts and source inspection are not a test pass. There is no claim that existing tests remain green until the lead verifies them.

## Ratchet and exact lead checks

Expected raw site reduction: **12**. The three image literals were at base `gateway/openai_media_schemas.py:367,402,404`; the eight video property/required literals at `730,733,741,744,772,775,789,792`; the deletion enum at `696`. All twelve disappear from Python and live in registry data. The reader uses HTTP method/path addresses and derives the matching registry filename from its module, rather than introducing another product-word filename literal.

This is a source-derived expectation, not a measured full-vocabulary count. `tests/test_domain_literal_ratchet.py:47-79` obtains vocabulary from excluded manifests/protocol files; lines 106-148 define matching/scanning. Its test at lines 342-350 requires exact budget equality, and lines 194-195 reject stale reviewed allowances. Therefore the lead must inspect reviewed/unreviewed output, reconcile any affected existing allowance and adjust the budget in the integration change. This worker did not read or edit either file and cannot promise the whole suite is green without those required lead actions.

Run these exact test files on the integrated branch:

1. `python -m pytest tests/test_openai_media_schemas_data.py`
2. `python -m pytest tests/test_domain_literal_ratchet.py`
3. `python tests/test_domain_literal_ratchet.py` to obtain the reviewed/unreviewed site evidence before adjusting the budget.

The submitting lead should then run its normal full repository gate and route/OpenAPI integration coverage, including package-data inclusion. Names of other test files were not guessed because they are outside this assignment's read allow-list. Runtime deletion correction remains blocked until the route owner can be reviewed and the conflicting golden requirement is explicitly resolved.
