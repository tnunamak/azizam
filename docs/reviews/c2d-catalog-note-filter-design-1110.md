# Catalog note-filter leaf design

Base: `tnunamak/azizam@6e7673175dadb8142695f67ec645456ad0538705`  
Assignment: `c2d-catalog-note-filter-design-1110`  
Outcome: **Blocked on the shipped-manifest inventory/proof; useful scoped analysis completed.** No production or test file was changed.

## 1. Scope mismatch and checked inventory

The requested shipped workflow contracts are not under the allow-listed root `workflows/`. The existing catalog test loads `manifests/workflows` (`tests/test_workflows_catalog.py:373-377`; also `:346-350`). The domain-literal vocabulary likewise reads YAML contracts from `manifests/` (`tests/test_domain_literal_ratchet.py:13-15,37,45-54`). That directory is **not** in this assignment's read allow-list. I did not read it, infer its contents from graph names, or open the excluded root gateway module.

All four subtrees returned non-truncated tree listings at the pinned base. I read all 52 JSON files: 4 audio, 1 identity, 32 images, 15 videos, including 14 `.ui.json` editor exports. None has top-level `summary`, `purpose`, or `schema_version`. They are backend graph/chain/editor documents, not the contract documents required by `gateway/manifests.py:460-469`. Top-level `id` on an editor/chain document does not make it a contract. Some graph files have `_meta.description` (for example `workflows/images/moss-image.json:2-4`), but the reviewed catalog uses a WorkflowRecord contract and `summarize_workflow`, not graph metadata (`gateway/workflows_catalog.py:140-168`).

The following is the complete **allowed graph-file inventory**, not a fabricated shipped-contract inventory. “No” means the top-level key is absent throughout the cited complete file. The corresponding contract's summary, purpose, exact description, and technical notes are **unchecked**, not absent.

| Allowed source and complete-file evidence | summary | purpose | Member-facing text today |
| --- | --- | --- | --- |
| `workflows/audio/ace-step-1.5-xl-turbo.json:1-92` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/audio/sa3-small-sfx.json:1-93` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/audio/tangoflux.json:1-42` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/audio/yue2-song.json:1-159` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/identity/qwen-image-2.1-identity.json:1-200` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/anima-turbo.json:1-138` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/fibo-2-edit-turbo.json:1-138` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b-edit.json:1-211` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b-edit.ui.json:1-1880` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b-inpaint.json:1-281` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b-inpaint.ui.json:1-2602` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b.json:1-147` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-4b.ui.json:1-1318` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-edit.json:1-201` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-edit.ui.json:1-1774` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein-snofs.json:1-159` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein.json:1-147` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/flux2-klein.ui.json:1-1318` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/krea2-turbo-snofs.json:1-150` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/krea2-turbo.json:1-138` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/krea2-turbo.ui.json:1-784` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image-fast.json:1-185` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image-fast.ui.json:1-1026` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image-quality.json:1-189` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image-quality.ui.json:1-1042` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image.json:1-182` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/moss-image.ui.json:1-874` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-edit-multiref.json:1-169` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-edit-multiref.ui.json:1-962` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-edit.json:1-156` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-edit.ui.json:1-910` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-inpaint.json:1-210` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/qwen-image-2.1-inpaint.ui.json:1-1243` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/sdxl-seamless-tile.json:1-149` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/sdxl-seamless-tile.ui.json:1-1364` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/seedvr2-upscale.json:1-176` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/images/seedvr2-upscale.ui.json:1-1021` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/chains/h3_chain.json:1-720` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/chains/ltx25_chain.json:1-662` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/rife-interpolate.json:1-73` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-ltx-2.5-i2v.json:1-533` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-ltx-2.5-keyframes-mid.json:1-868` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-ltx-2.5-keyframes.json:1-708` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-ltx-2.5-long.json:1-223` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-ltx-2.5-t2v.json:1-411` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-minimax-h3-i2v.json:1-240` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-minimax-h3-keyframes-mid.json:1-340` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-minimax-h3-keyframes.json:1-273` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-minimax-h3-long.json:1-187` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-minimax-h3-t2v.json:1-207` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-mux.json:1-51` | No | No | Not a workflow contract; catalog text not derivable from this file. |
| `workflows/videos/video-scail-2-replace.json:1-475` | No | No | Not a workflow contract; catalog text not derivable from this file. |

**Could not check:** the number and identities of shipped contract manifests, each contract's summary/purpose presence, every current member-facing description/technical note, or exact replacement summary copy. The graph inventory above cannot substitute for those facts. The permitted test expresses an invariant about all builtin purposes; it is evidence of the intended invariant, not evidence that the current shipped data passes it (`tests/test_workflows_catalog.py:390-410`).

## 2. Current member-facing trace

1. Policy filtering precedes presentation: only capabilities with a workflow and `policy_available is not False` contribute rows; missing records are skipped (`gateway/workflows_catalog.py:84-108`).
2. `_workflow_row` first calls `summarize_workflow(record, "client")`. It obtains purpose from that projection, strips surrounding whitespace, and obtains summary directly from `record.contract`, collapsing all summary whitespace (`gateway/workflows_catalog.py:149,162-166`).
3. Description precedence is **nonempty normalized summary, then nonempty stripped purpose, then `_workflow_description(projected_description, capability)`** (`gateway/workflows_catalog.py:167`). Summary/purpose bypass the word filter entirely, and their chosen strings do not receive sentence punctuation from this helper.
4. Only the last branch takes the first sentence, limits it to eight whitespace-separated words, and rejects it via the technical-word/punctuation patterns. An accepted sentence gets terminal punctuation if missing; otherwise the helper returns the capability description or “Run this configured workflow.” (`gateway/workflows_catalog.py:219-258`).
5. A nonempty summary **suppresses technical_notes**. Without summary, the entire stripped projected description is included as technical_notes unless empty or exactly equal to the selected public description (`gateway/workflows_catalog.py:168,214-215,226-230`). Thus the filter does not erase technical notes from the catalog.
6. Purpose is exposed separately as the stripped value or null, independently of description (`gateway/workflows_catalog.py:176-177`). The test directly covers summary preference, purpose preservation and research-note omission (`tests/test_workflows_catalog.py:265-281`).

The implementation of `summarize_workflow` and the complete capability-description mapping are outside the allowed files; their internals were not read. The permitted tests establish the exact image fallback “Generate or edit images from your prompts.” and sound-effect fallback “Sound effects from a description.” (`tests/test_workflows_catalog.py:323-326,341-344`).

### Reproduced defect

An isolated execution of the exact allowed `_workflow_description` and helper bodies, with the image fallback supplied from the permitted test assertion, produced:

| Description input | Current result |
| --- | --- |
| Make pictures with ComfyUI. | Generate or edit images from your prompts. |
| Make pictures with ZephyraNova. | Make pictures with ZephyraNova. |
| Make a picture from a description. | Make a picture from a description. |

Both engine-bearing sentences have four words. The difference is the explicit `comfyui` token at `gateway/workflows_catalog.py:250`, not data about the engine. This was an isolated leaf reproduction, **not** a repository test run or a claim about deployed UI behavior.

## 3. Recommendation: explicit presentation fields, never classify a research note

Choose a generic rule rather than another vocabulary filter:

- Keep summary and purpose precedence and normalization unchanged.
- If both are absent, return the existing capability fallback without reading or classifying the description text.
- Keep existing technical_notes behavior unchanged.
- Remove `_is_technical_workflow_note`, `_first_sentence`, and `_sentence` once references are checked within the eventual implementation scope. Their only calls in this file are within the old fallback path (`gateway/workflows_catalog.py:219-258`).
- Retain the two-argument helper signature initially so callers and the existing test import need not change (`tests/test_workflows_catalog.py:24-29`).

Proposed leaf implementation, **not applied**:

```python
def _workflow_description(value: Any, capability: str) -> str:
    # Free-form descriptions are technical notes; public copy is summary/purpose.
    return CAPABILITY_DESCRIPTIONS.get(capability, "Run this configured workflow.")
```

This rule names no engine, model, file format, version, or licensing term. It handles an unfamiliar name exactly as a known one. It intentionally changes the fallback for legacy/external contracts that have neither summary nor purpose but currently expose a short, accepted description. That compatibility change must be documented; it cannot be honestly described as unchanged behavior for all possible contracts.

**Shipped-data proof is still required.** The existing builtin test expects all rows to have nonempty purposes and descriptions equal to summary-or-purpose (`tests/test_workflows_catalog.py:390-410`). If the proposed stronger test below passes for the pinned shipped registry, no shipped row evaluates the helper at all. Then this leaf change preserves every shipped description and technical_notes, independently of the contents of every note. Without access to those contracts or an authorized run, that is a conditional proof, not a completed proof against every shipped note.

### Why not blindly add missing summaries?

Adding a summary equal to the old description can preserve the headline while removing technical_notes because of `gateway/workflows_catalog.py:168`. The existing test explicitly allows a no-summary builtin to have technical_notes (`tests/test_workflows_catalog.py:412-420`). Therefore the summary-migration option is not automatically “no visible text changes.” It needs a complete pre/post projection comparison, including technical_notes. Also, the existing validator allows summaries up to 40 words, not a 90-character maximum (`gateway/manifests.py:480-488`); the requested under-90-character editorial constraint needs its own test. No exact per-manifest copy is proposed without reading the actual contracts.

## 4. Proving tests in the existing test file

These are proposed additions to `WorkflowsCatalogTests` in `tests/test_workflows_catalog.py`, not changes made by this review. They use its current imports, fixtures, registry construction and catalog builder (`:4-29,32-65,228-235,373-388`).

### A. All shipped rows bypass the classifier

Run this before and after the change. It cannot pass vacuously: it requires records, a clean registry, complete row coverage and a nonempty explicit presentation field. It replaces the entire helper with a failure, which is stronger than enumerating every engine spelling in the notes.

```python
    def test_every_builtin_description_uses_explicit_public_copy(self) -> None:
        root = Path(__file__).resolve().parents[1] / "manifests" / "workflows"
        registry = WorkflowRegistry.from_directory(root)
        self.assertEqual(registry.errors, [])
        records = list(registry.records())
        self.assertTrue(records)
        capabilities = [
            Capability(
                intent=record.intent,
                backend=record.supported_backends[0],
                workflow=record.id,
            )
            for record in records
        ]

        baseline = build_workflows_catalog(
            records=records, capabilities=capabilities,
        )
        with patch(
            "gateway.workflows_catalog._workflow_description",
            side_effect=AssertionError("A builtin reached free-form note fallback"),
        ):
            without_note_classifier = build_workflows_catalog(
                records=records, capabilities=capabilities,
            )

        self.assertEqual(without_note_classifier, baseline)
        rows = {row["id"]: row for row in baseline["workflows"]}
        self.assertEqual(set(rows), {record.id for record in records})
        for record in records:
            with self.subTest(workflow=record.id):
                summary = record.contract.get("summary")
                summary = " ".join(summary.split()) if isinstance(summary, str) else ""
                purpose = record.contract.get("purpose")
                purpose = purpose.strip() if isinstance(purpose, str) else ""
                self.assertTrue(summary or purpose)
                self.assertEqual(rows[record.id]["description"], summary or purpose)
```

The helper patch ensures no description note is inspected by this leaf for any shipped record. The complete payload comparison includes technical_notes rather than silently ignoring their removal. The existing builtin-purpose test remains in place; this test does not weaken it.

### B. Unknown engine name, known name, ordinary prose, blank notes

This test should fail on the pinned implementation for the unfamiliar engine and the ordinary prose example, then pass with the generic rule. It deliberately tests the description classifier's input, not whether the registry can bind an unfamiliar engine.

```python
    def test_note_fallback_is_independent_of_engine_spelling(self) -> None:
        for note in (
            "Make pictures with ComfyUI.",
            "Make pictures with ZephyraNova.",
            "Make a picture from a description.",
            "",
            None,
        ):
            with self.subTest(note=note):
                self.assertEqual(
                    _workflow_description(note, "image"),
                    "Generate or edit images from your prompts.",
                )
                self.assertEqual(
                    _workflow_description(note, "unfamiliar-capability"),
                    "Run this configured workflow.",
                )
```

Unlike adding the new name to a denylist, this test establishes that no free-form note is a source of public fallback copy. Keep the existing internal-token/license test at `tests/test_workflows_catalog.py:465-477`; it still passes with the generic rule.

### C. Public-copy precedence and note visibility stay explicit

The existing summary test covers this for one note; extend it with an unfamiliar engine note and cover the purpose-only case without changing binding registration.

```python
    def test_public_copy_and_notes_do_not_depend_on_engine_names(self) -> None:
        for engine in ("ComfyUI", "ZephyraNova"):
            for has_summary in (False, True):
                with self.subTest(engine=engine, has_summary=has_summary):
                    manifest = yaml.safe_load(IMAGE_MANIFEST)
                    note = f"Make pictures with {engine}."
                    manifest.update(
                        purpose="Make a picture",
                        description=note,
                    )
                    if has_summary:
                        manifest["summary"] = "  Make a picture   for your home.  "
                    registry = WorkflowRegistry()
                    registry.add_manifest(
                        manifest, source_path="public-copy.yml", facts=STANDARD_FACTS,
                    )
                    payload = build_workflows_catalog(
                        records=registry.records(),
                        capabilities=[
                            Capability(
                                intent=manifest["intent"],
                                backend="comfyui",
                                workflow=manifest["id"],
                            )
                        ],
                    )
                    row = payload["workflows"][0]
                    self.assertEqual(row["purpose"], "Make a picture")
                    if has_summary:
                        self.assertEqual(row["description"], "Make a picture for your home.")
                        self.assertNotIn("technical_notes", row)
                    else:
                        self.assertEqual(row["description"], "Make a picture")
                        self.assertEqual(row["technical_notes"], note)
```

The known binding is fixture plumbing, not a rule selecting presentation behavior. The unfamiliar engine spelling is solely in the note under test.

**Not run:** these repository tests, the shipped registry load, full lint/type/aggregate checks, or a UI integration. Their imports load sources outside this assignment's read scope. No bridge workspace/check tool was invoked.

## 5. Counted sites

The engine-bearing pattern is **one counted string-literal site**, at `gateway/workflows_catalog.py:250`. Removing this classifier removes that site; it is not one site for each alternative in the regular expression. The scanner counts each matching AST constant once (`tests/test_domain_literal_ratchet.py:106-147`). An isolated run of that exact scanner against the allowed catalog source with the explicit vocabulary `{"comfyui"}` found exactly this one site.

There is no reviewed allowance for that pattern in the allowed reviewed file; the catalog allowances are the three separate MIME-family literals audio, image and video (`tests/domain_literal_reviewed.yml:108-122`). Those stay untouched.

**Targeted reduction: 1 unreviewed engine-name site. Expected full ratchet delta: -1.** The full vocabulary/total was not recomputed: `vocabulary()` reads excluded `manifests/` and `registry/protocols/`, while `all_sites()` reads the excluded root module and every gateway module (`tests/test_domain_literal_ratchet.py:49-82,150-160`). The current budget value was not read. A future scoped implementation must run the full owner-approved ratchet and lower its actual budget accordingly, as the existing test requires (`tests/test_domain_literal_ratchet.py:19-22`).

## 6. Verification and remaining work

Completed: pinned allowed-file reads, complete 52-file graph inventory, description/notes control-flow trace, reproduced engine-name-dependent leaf behavior, isolated one-site count, and ready-to-copy tests. Only this note was created.

Remaining, not silently assumed: the full shipped **contract** inventory and exact text; proof that all shipped notes bypass the helper; full projected-output equivalence; integration checks; actual repository-wide ratchet delta. These depend on sources outside the immutable read allow-list. No request for broader access was made, in accordance with the brief's instruction to list unchecked work instead of asking.

The artifact is therefore a **blocked partial review**, not approval to remove the filter or a claim that the requested shipped-data proof is complete.

