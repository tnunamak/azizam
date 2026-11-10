"""Unit tests for gateway/model_metadata.py (the /v1/models metadata contract)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import model_metadata as mm  # noqa: E402


def _arch(fields: dict) -> tuple[list[str], list[str]]:
    return fields["architecture"]["input_modalities"], fields["architecture"]["output_modalities"]


def _annotated(entries: list[dict], sources: mm.MetadataSources | None = None, **kwargs) -> dict[str, dict]:
    entries = [dict(e) for e in entries]
    mm.annotate_entries(entries, sources or mm.MetadataSources(), **kwargs)
    return {e["id"]: e for e in entries}


def _workflow(model_id: str, intent: str, manifest: dict | None = None) -> tuple[dict, dict]:
    entry = {"id": model_id, "object": "model", "azizam_intent": intent, "azizam_workflow_id": f"{intent}.{model_id}"}
    return entry, {f"{intent}.{model_id}": manifest or {}}


class DerivationTableTests(unittest.TestCase):
    """One test per row of the contract's derivation table."""

    def _workflow_fields(self, intent: str, manifest: dict) -> dict:
        entry, manifests = _workflow("wf", intent, manifest)
        sources = mm.MetadataSources(manifest=manifests.get)
        return _annotated([entry], sources)["wf"]

    def test_image_generate(self) -> None:
        fields = self._workflow_fields("image.generate", {"inputs": {"prompt": {"type": "string"}}, "outputs": {"image": {"media_type": "image/png"}}})
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text"], ["image"]))

    def test_image_edit(self) -> None:
        fields = self._workflow_fields("image.edit", {"inputs": {"prompt": {"type": "string"}, "source_image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"image": {"media_type": "image/png"}}})
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text", "image"], ["image"]))

    def test_image_analyze_uses_manifest_output(self) -> None:
        fields = self._workflow_fields("image.analyze", {"inputs": {"candidate": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"report": {"media_type": "application/json"}}})
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["image"], ["text"]))

    def test_video_generate_text_only(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"prompt": {"type": "string"}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(fields["type"], "video")
        self.assertEqual(_arch(fields), (["text"], ["video"]))

    def test_video_generate_with_input_image(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"prompt": {"type": "string"}, "image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(_arch(fields), (["text", "image"], ["video"]))

    def test_video_generate_asset_media_type_is_respected(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"prompt": {"type": "string"}, "clip": {"type": "asset", "media_type": "audio/wav"}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(_arch(fields), (["text", "audio"], ["video"]))

    def test_video_edit_with_reference_image_and_source_video(self) -> None:
        fields = self._workflow_fields("video.edit", {
            "inputs": {
                "reference_image": {"type": "asset", "media_type": "image/*"},
                "source_video": {"type": "asset", "media_type": "video/*"},
                "prompt": {"type": "string"},
            },
            "outputs": {"video": {"media_type": "video/mp4"}},
        })
        self.assertEqual(fields["type"], "video")
        self.assertEqual(_arch(fields), (["text", "image", "video"], ["video"]))

    def test_video_edit_asset_without_media_type_is_the_edited_video(self) -> None:
        fields = self._workflow_fields("video.edit", {"inputs": {"prompt": {"type": "string"}, "source_video": {"type": "asset", "accepts": ["video/mp4"]}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(_arch(fields), (["text", "video"], ["video"]))

    def test_image_edit_asset_media_type_is_respected(self) -> None:
        fields = self._workflow_fields("image.edit", {"inputs": {"prompt": {"type": "string"}, "image": {"type": "asset", "accepts": ["image/png"]}, "clip": {"type": "asset", "media_type": "video/mp4"}}, "outputs": {"image": {"media_type": "image/png"}}})
        self.assertEqual(_arch(fields), (["text", "image", "video"], ["image"]))

    def test_repository_video_edit_manifests_declare_their_inputs(self) -> None:
        import yaml

        expected = {
            "video-scail-2-replace": ["text", "image", "video"],
        }
        for workflow_id, inputs in expected.items():
            with self.subTest(workflow=workflow_id):
                manifest = yaml.safe_load((ROOT / "manifests" / "workflows" / f"{workflow_id}.yml").read_text(encoding="utf-8"))
                derived = mm.from_contract(manifest)
                assert derived is not None
                self.assertEqual(list(derived.input_modalities), inputs)
                for name, spec in manifest["inputs"].items():
                    if spec.get("type") == "asset":
                        # Schema 2 asset inputs declare the types they take in `accepts`.
                        self.assertIn("accepts", spec, f"{workflow_id}.{name}")

    def test_audio_generate(self) -> None:
        fields = self._workflow_fields("audio.generate", {"inputs": {"prompt": {"type": "string"}}, "outputs": {"audio": {"media_type": "audio/wav"}}})
        self.assertEqual(fields["type"], "audio")
        self.assertEqual(_arch(fields), (["text"], ["audio"]))

    def test_alias_operations(self) -> None:
        expected = {
            "chat": ("language", ["text"], ["text"]),
            "embeddings": ("embedding", ["text"], ["embedding"]),
            "images": ("image", ["text"], ["image"]),
            "image_edits": ("image", ["text", "image"], ["image"]),
            "videos": ("video", ["text"], ["video"]),
            "speech": ("audio", ["text"], ["audio"]),
            "transcriptions": ("audio", ["audio"], ["text"]),
            "audio_generate": ("audio", ["text"], ["audio"]),
            "rerank": ("reranking", ["text"], ["text"]),
        }
        for op, (type_, inputs, outputs) in expected.items():
            with self.subTest(op=op):
                fields = _annotated([{"id": "a", "azizam_resolves_to": {op: "unlisted-target"}}])["a"]
                self.assertEqual(fields["type"], type_)
                self.assertEqual(_arch(fields), (inputs, outputs))

    def test_alias_chat_gets_image_input_only_if_target_declares_vision(self) -> None:
        declared = {"vlm": {"tags": ["vision"]}}
        sources = mm.MetadataSources(declared=declared.get, origins={"vlm": mm.ORIGIN_LLM_CATALOG, "llm": mm.ORIGIN_LLM_CATALOG})
        out = _annotated([
            {"id": "vlm"}, {"id": "llm"},
            {"id": "vision", "azizam_resolves_to": {"chat": "vlm"}},
            {"id": "plain", "azizam_resolves_to": {"chat": "llm"}},
        ], sources)
        self.assertEqual(_arch(out["vision"])[0], ["text", "image"])
        self.assertIn("vision", out["vision"]["tags"])
        self.assertEqual(_arch(out["plain"])[0], ["text"])
        self.assertNotIn("tags", out["plain"])

    def test_alias_with_two_operations_takes_shared_type(self) -> None:
        fields = _annotated([{"id": "image", "azizam_resolves_to": {"images": "g", "image_edits": "e"}}])["image"]
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text", "image"], ["image"]))

    def test_alias_with_no_shared_type_is_untyped(self) -> None:
        fields = _annotated([{"id": "mixed", "azizam_resolves_to": {"chat": "x", "speech": "y"}}])["mixed"]
        self.assertNotIn("type", fields)
        self.assertNotIn("architecture", fields)

    def test_alias_target_listed_in_catalog_wins_over_operation_default(self) -> None:
        entry, manifests = _workflow("i2v", "video.generate", {"inputs": {"prompt": {"type": "string"}, "image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        sources = mm.MetadataSources(manifest=manifests.get)
        out = _annotated([{"id": "video", "azizam_resolves_to": {"videos": "i2v"}}], sources, catalog=[entry])
        self.assertEqual(_arch(out["video"]), (["text", "image"], ["video"]))

    def test_alias_target_resolves_through_workflow_registry_when_unlisted(self) -> None:
        manifest = {"intent": "video.edit", "inputs": {"prompt": {"type": "string"}, "source_video": {"type": "asset", "accepts": ["video/mp4"]}}, "outputs": {"video": {"media_type": "video/mp4"}}}
        sources = mm.MetadataSources(workflow_for_model={"remix-wf": manifest}.get)
        out = _annotated([{"id": "video-remix", "azizam_resolves_to": {"videos": "remix-wf"}}], sources)
        self.assertEqual(_arch(out["video-remix"]), (["text", "video"], ["video"]))

    def test_decision_models(self) -> None:
        sources = mm.MetadataSources(origins={"decide": mm.ORIGIN_DECISION})
        fields = _annotated([{"id": "decide", "capabilities": {"backend": "decisions"}}], sources)["decide"]
        self.assertEqual(fields["type"], "decision")
        self.assertEqual(_arch(fields), (["text"], ["text"]))

    def test_llm_catalog_entries_are_language(self) -> None:
        sources = mm.MetadataSources(origins={"remote/x": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "remote/x", "owned_by": "openai"}], sources)["remote/x"]
        self.assertEqual(fields["type"], "language")
        self.assertEqual(_arch(fields), (["text"], ["text"]))

    def test_current_loaded_entries_are_language(self) -> None:
        for model_id in ("current", "current-fast"):
            fields = _annotated([{"id": model_id, "azizam_current_loaded": True}])[model_id]
            self.assertEqual(fields["type"], "language")

    def test_registry_service_capabilities_override_llm_catalog_origin(self) -> None:
        caps = {"embed-model": ["openai", "embeddings"], "chat-model": ["openai", "chat", "completions"], "vlm": ["chat", "vision"]}
        origins = {model_id: mm.ORIGIN_LLM_CATALOG for model_id in caps}
        sources = mm.MetadataSources(service_capabilities=caps.get, origins=origins)
        out = _annotated([{"id": "embed-model"}, {"id": "chat-model"}, {"id": "vlm"}], sources)
        self.assertEqual(out["embed-model"]["type"], "embedding")
        self.assertEqual(_arch(out["embed-model"]), (["text"], ["embedding"]))
        self.assertEqual(out["chat-model"]["type"], "language")
        self.assertEqual(_arch(out["vlm"])[0], ["text", "image"])
        self.assertEqual(out["vlm"]["tags"], ["vision"])

    def test_embeddings_intent_entry(self) -> None:
        fields = _annotated([{"id": "e", "azizam_intent": "text.embeddings"}])["e"]
        self.assertEqual(fields["type"], "embedding")


class AssetModalityTests(unittest.TestCase):
    def test_scalar_mixed_generic_text_and_schema_one_assets(self) -> None:
        cases = (
            ({"accepts": "image/png"}, "image", "image/png"),
            ({"accepts": ["image/png", "image/jpeg"]}, "image", "image/*"),
            ({"accepts": ["audio/wav", "image/png", "audio/mpeg"]}, None, None),
            ({"accepts": ["*/*"]}, None, "*/*"),
            ({"accepts": ["text/plain", "application/json"]}, "text", "text/*"),
            ({"accepts": "application/json"}, "text", "application/json"),
            ({"accepts": ["application/pdf", "application/octet-stream"]}, None, None),
            ({"media_type": "audio/wav"}, "audio", "audio/wav"),
            ({"accepts": [], "media_type": "video/mp4"}, "video", "video/mp4"),
            ({}, None, None),
        )
        for declaration, modality, media_type in cases:
            with self.subTest(declaration=declaration):
                spec = {"type": "asset", **declaration}
                self.assertEqual(mm._asset_input_modality(spec), modality)
                self.assertEqual(mm.asset_media_type(spec), media_type)

    def test_no_accepts_never_adds_an_intent_named_input(self) -> None:
        for intent in ("image.edit", "audio.transcribe", "speech.recognize",
                       "sound.analyze", "music.analyze", "video.edit", "smell.infer"):
            for accepts in (None, []):
                with self.subTest(intent=intent, accepts=accepts):
                    spec = {"type": "asset"}
                    if accepts is not None:
                        spec["accepts"] = accepts
                    result = mm.from_contract({
                        "intent": intent, "inputs": {"source": spec},
                        "outputs": {"result": {"media_type": "text/plain"}},
                    })
                    self.assertIsNotNone(result)
                    self.assertEqual(result.input_modalities, ())

    def test_nested_leaves_keep_single_kind_rule_per_leaf(self) -> None:
        manifest = {"intent": "smell.infer", "inputs": {
            "payload": {"type": "object", "properties": {
                "frames": {"type": "array", "items": {"type": "asset", "accepts": "image/png"}},
                "captions": {"type": "asset", "accepts": ["text/plain", "application/json"]},
                "mixed": {"type": "asset", "accepts": ["audio/wav", "video/mp4"]},
                "opaque": {"type": "asset", "accepts": ["*/*"]},
            }},
        }, "outputs": {"result": {"media_type": "text/plain"}}}
        result = mm.from_contract(manifest)
        self.assertIsNotNone(result)
        self.assertEqual(result.input_modalities, ("text", "image"))
        self.assertEqual(result.to_fields()["architecture"]["input_modalities"], ["text", "image"])


# Frozen asset derivations from 1d749a9c9a2a4452e6771a166c6d8c6b4594039b.
def _base_asset_input_modality(spec: Mapping[str, Any]) -> str | None:
    """The modality an asset input accepts: the shared kind of its ``accepts`` list (a schema 1 ``media_type`` counts as a
    one-item list). None when it names no single kind."""
    accepted = spec.get("accepts") or ([spec["media_type"]] if spec.get("media_type") else ())
    majors = {str(item).split("/", 1)[0].strip().lower() for item in accepted if str(item).strip()}
    major = next(iter(majors)) if len(majors) == 1 else ""
    return major if major in mm.INPUT_MODALITIES else None


def _base_asset_media_type(spec: Mapping[str, Any]) -> str | None:
    """Media kind of one asset input, as ``image/*``. The document says it by what the input accepts: one type as it is,
    several as their shared kind; an input that accepts nothing it can name has no media type."""
    accepted = [str(item).strip() for item in spec.get("accepts") or () if str(item).strip()]
    if len(accepted) == 1:
        return accepted[0]
    majors = {item.split("/", 1)[0].lower() for item in accepted}
    if len(majors) == 1 and next(iter(majors)) in mm.INPUT_MODALITIES:
        return f"{next(iter(majors))}/*"
    return None


class ShippedModelMetadataBaseParityTests(unittest.TestCase):
    def test_all_shipped_model_metadata_and_asset_media_types_match_base(self) -> None:
        import yaml

        folder = ROOT / "manifests" / "workflows"
        paths = sorted(path for path in folder.rglob("*") if path.suffix in {".yml", ".yaml"})
        self.assertTrue(paths, "No shipped manifests found; parity must not pass vacuously")
        for path in paths:
            with self.subTest(manifest=str(path.relative_to(ROOT))):
                manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(manifest, dict)
                current = mm.from_contract(manifest)
                with patch.object(mm, "_asset_input_modality", _base_asset_input_modality):
                    base = mm.from_contract(manifest)
                self.assertEqual(current, base)
                self.assertEqual(current.to_fields() if current else None,
                                 base.to_fields() if base else None)
                for leaf in mm._asset_inputs(manifest):
                    with self.subTest(asset=leaf):
                        self.assertEqual(mm.asset_media_type(leaf), _base_asset_media_type(leaf))


class OverrideTests(unittest.TestCase):
    def test_declared_values_win_over_derived(self) -> None:
        declared = {"m": {"type": "image", "input_modalities": ["text"], "output_modalities": ["image"], "max_tokens": 4096, "tags": ["reasoning"]}}
        sources = mm.MetadataSources(declared=declared.get, origins={"m": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "m"}], sources)["m"]
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text"], ["image"]))
        self.assertEqual(fields["max_tokens"], 4096)
        # Tags are chat-model flags; a declared non-language type drops them.
        self.assertNotIn("tags", fields)

    def test_declaration_alone_classifies_an_unknown_entry(self) -> None:
        declared = {"mystery": {"type": "reranking", "input_modalities": ["text"], "output_modalities": ["text"]}}
        fields = _annotated([{"id": "mystery"}], mm.MetadataSources(declared=declared.get))["mystery"]
        self.assertEqual(fields["type"], "reranking")

    def test_declaration_on_alias_wins_over_target(self) -> None:
        declared = {"t": {"tags": ["reasoning"]}, "a": {"tags": []}}
        sources = mm.MetadataSources(declared=declared.get, origins={"t": mm.ORIGIN_LLM_CATALOG})
        out = _annotated([{"id": "t"}, {"id": "a", "azizam_resolves_to": {"chat": "t"}}], sources)
        self.assertEqual(out["t"]["tags"], ["reasoning"])
        self.assertNotIn("tags", out["a"])

    def test_invalid_declared_values_are_ignored(self) -> None:
        declared = {"m": {"type": "chat", "input_modalities": ["text", "smell"], "tags": ["magic"], "max_tokens": -1}}
        sources = mm.MetadataSources(declared=declared.get, origins={"m": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "m"}], sources)["m"]
        self.assertEqual(fields["type"], "language")
        self.assertEqual(_arch(fields)[0], ["text"])
        self.assertNotIn("tags", fields)
        self.assertNotIn("max_tokens", fields)

    def test_profile_filtered_catalog_does_not_change_alias_values(self) -> None:
        declared = {"vlm": {"tags": ["vision"]}}
        sources = mm.MetadataSources(declared=declared.get, origins={"vlm": mm.ORIGIN_LLM_CATALOG})
        catalog = [{"id": "vlm"}, {"id": "vision", "azizam_resolves_to": {"chat": "vlm"}}]
        visible = _annotated([catalog[1]], sources, catalog=catalog)
        self.assertIn("image", _arch(visible["vision"])[0])


class LoadedModelInheritanceTests(unittest.TestCase):
    """Dynamic aliases (`current`, `current-fast`) take the loaded model's declared metadata."""

    DECLARED = {
        "big": {"input_modalities": ["text", "image"], "tags": ["reasoning", "tool-use"], "max_tokens": 8192},
        "big-alt": {"input_modalities": ["text", "image"], "tags": ["reasoning"], "max_tokens": 8192},
        "current": {"input_modalities": ["text"], "tags": []},
    }

    def _sources(self, **kwargs) -> mm.MetadataSources:
        return mm.MetadataSources(declared=self.DECLARED.get, **kwargs)

    def test_loaded_metadata_comes_from_declared_ids_only(self) -> None:
        loaded = mm.loaded_model_metadata(["big", "undeclared.gguf", "/path/to/big.gguf"], self._sources())
        self.assertEqual(loaded.to_fields(), {
            "type": "language",
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "max_tokens": 8192,
            "tags": ["tool-use", "reasoning", "vision"],
        })

    def test_several_declared_ids_claim_only_what_they_share(self) -> None:
        loaded = mm.loaded_model_metadata(["big", "big-alt"], self._sources())
        self.assertEqual(loaded.tags, ("reasoning", "vision"))
        self.assertEqual(loaded.max_tokens, 8192)

    def test_nothing_declared_loaded_is_none(self) -> None:
        self.assertIsNone(mm.loaded_model_metadata([], self._sources()))
        self.assertIsNone(mm.loaded_model_metadata(["undeclared.gguf"], self._sources()))

    def test_common_needs_a_shared_type(self) -> None:
        self.assertIsNone(mm.common([mm.from_operation("chat"), mm.from_operation("embeddings")]))
        caps = mm.common([mm.ModelMetadata("language", ("text",), ("text",), 10), mm.ModelMetadata("language", ("text",), ("text",), 20)])
        self.assertIsNone(caps.max_tokens)

    def test_inherited_wins_over_the_alias_declaration(self) -> None:
        loaded = mm.loaded_model_metadata(["big"], self._sources())
        inherited = {"current": loaded, "current-fast": mm.without_reasoning(loaded)}
        entries = [{"id": "current", "azizam_current_loaded": True}, {"id": "current-fast", "azizam_current_loaded": True}]
        out = _annotated(entries, self._sources(inherited=inherited))
        self.assertEqual(out["current"]["tags"], ["tool-use", "reasoning", "vision"])
        self.assertEqual(out["current-fast"]["tags"], ["tool-use", "vision"])
        self.assertEqual(_arch(out["current-fast"]), (["text", "image"], ["text"]))

    def test_without_inheritance_the_alias_declaration_is_the_fallback(self) -> None:
        out = _annotated([{"id": "current", "azizam_current_loaded": True}], self._sources())
        self.assertEqual(_arch(out["current"]), (["text"], ["text"]))
        self.assertNotIn("tags", out["current"])

    def test_alias_resolving_to_a_dynamic_alias_inherits_too(self) -> None:
        loaded = mm.loaded_model_metadata(["big"], self._sources())
        out = _annotated(
            [{"id": "current"}, {"id": "here", "azizam_resolves_to": {"chat": "current"}}],
            self._sources(inherited={"current": loaded}),
        )
        self.assertIn("reasoning", out["here"]["tags"])


class ConsistencyAndOmissionTests(unittest.TestCase):
    def test_vision_tag_implies_image_input(self) -> None:
        sources = mm.MetadataSources(declared={"m": {"tags": ["vision"]}}.get, origins={"m": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "m"}], sources)["m"]
        self.assertIn("image", _arch(fields)[0])

    def test_image_input_implies_vision_tag(self) -> None:
        sources = mm.MetadataSources(declared={"m": {"input_modalities": ["text", "image"]}}.get, origins={"m": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "m"}], sources)["m"]
        self.assertIn("vision", fields["tags"])

    def test_every_typed_entry_is_consistent(self) -> None:
        entries = [{"id": f"a-{op}", "azizam_resolves_to": {op: "t"}} for op in ("chat", "images", "image_edits", "videos", "speech", "transcriptions")]
        for fields in _annotated(entries).values():
            if fields["type"] == "language":
                self.assertEqual("vision" in fields.get("tags", []), "image" in _arch(fields)[0], fields["id"])
            else:
                self.assertNotIn("tags", fields, fields["id"])

    def test_media_models_take_image_input_without_tags(self) -> None:
        edit, edit_manifests = _workflow("edit", "image.edit", {"inputs": {"image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"image": {"media_type": "image/png"}}})
        i2v, i2v_manifests = _workflow("i2v", "video.generate", {"inputs": {"image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        sources = mm.MetadataSources(manifest={**edit_manifests, **i2v_manifests}.get)
        out = _annotated([edit, i2v, {"id": "image-edit", "azizam_resolves_to": {"image_edits": "edit"}}], sources)
        for model_id in ("edit", "i2v", "image-edit"):
            with self.subTest(model=model_id):
                self.assertIn("image", _arch(out[model_id])[0])
                self.assertNotIn("tags", out[model_id])

    def test_declared_tags_on_media_models_are_dropped(self) -> None:
        entry, manifests = _workflow("edit", "image.edit", {"inputs": {"prompt": {"type": "string"}, "image": {"type": "asset", "accepts": ["image/png"]}}, "outputs": {"image": {"media_type": "image/png"}}})
        sources = mm.MetadataSources(manifest=manifests.get, declared={"edit": {"tags": ["vision", "tool-use"]}}.get)
        fields = _annotated([entry], sources)["edit"]
        self.assertNotIn("tags", fields)
        self.assertEqual(_arch(fields), (["text", "image"], ["image"]))

    def test_unknown_entry_gets_no_fields(self) -> None:
        fields = _annotated([{"id": "who-knows", "object": "model"}])["who-knows"]
        for key in mm.OUTPUT_FIELDS:
            self.assertNotIn(key, fields)

    def test_max_tokens_and_tags_omitted_unless_known(self) -> None:
        fields = _annotated([{"id": "c", "azizam_current_loaded": True}])["c"]
        self.assertNotIn("max_tokens", fields)
        self.assertNotIn("tags", fields)
        self.assertNotIn("context_window", fields)

    def test_context_window_passes_through_untouched(self) -> None:
        fields = _annotated([{"id": "c", "azizam_current_loaded": True, "context_window": 1234}])["c"]
        self.assertEqual(fields["context_window"], 1234)

    def test_existing_azizam_fields_are_untouched(self) -> None:
        entry = {"id": "image", "azizam_alias": True, "azizam_resolves_to": {"images": "x"}, "owned_by": "azizam"}
        fields = _annotated([entry])["image"]
        for key, value in entry.items():
            self.assertEqual(fields[key], value)

    def test_no_new_field_uses_a_legacy_name(self) -> None:
        for name in (*mm.OUTPUT_FIELDS, *mm.DECLARABLE_FIELDS, mm.DECLARATION_KEY):
            self.assertNotIn("vivid_fish", name)
        entry = {"id": "a", "azizam_resolves_to": {"chat": "t"}}
        fields = _annotated([entry])["a"]
        self.assertEqual(
            {k for k in fields if "vivid_fish" in k},
            {k for k in entry if "vivid_fish" in k},
        )

    def test_vocabulary_matches_contract(self) -> None:
        self.assertEqual(set(mm.MODEL_TYPES), {"language", "embedding", "reranking", "image", "video", "audio", "decision"})
        self.assertEqual(set(mm.INPUT_MODALITIES), {"text", "image", "audio", "video", "file"})
        self.assertEqual(set(mm.OUTPUT_MODALITIES), {"text", "image", "audio", "video", "embedding"})
        self.assertEqual(set(mm.TAGS), {"tool-use", "reasoning", "vision", "file-input"})

    def test_metadata_index_matches_annotation(self) -> None:
        entries = [{"id": "embed", "azizam_resolves_to": {"embeddings": "e"}}]
        index = mm.metadata_index(entries, mm.MetadataSources())
        self.assertEqual(index["embed"]["type"], "embedding")
        self.assertNotIn("azizam_resolves_to", index["embed"])


class EngineAgnosticTests(unittest.TestCase):
    def test_module_names_no_engine(self) -> None:
        source = (ROOT / "gateway" / "model_metadata.py").read_text(encoding="utf-8").lower()
        for engine in ("vllm", "ollama", "llama", "kobold", "tabby", "qwen", "comfy", "litellm"):
            self.assertNotIn(engine, source)


if __name__ == "__main__":
    unittest.main()


def test_operations_of_matches_the_operation_metadata_by_type_and_inputs():
    from gateway import stable_aliases
    engines = stable_aliases.ENGINE_OPERATIONS
    of = mm.operations_of
    assert of("decision", ["text"], among=engines) == ("decisions",)
    assert of("reranking", ["text"], among=engines) == ("rerank",)
    assert of("audio", ["text"], among=engines) == ("speech",)
    assert of("audio", ["audio"], among=engines) == ("transcriptions",)
    # Unknown inputs, or inputs no operation takes, keep every operation of the type.
    assert of("audio", None, among=engines) == ("speech", "transcriptions")
    assert of("decision", ["text", "image"], among=engines) == ("decisions",)
    # An ordinary entry belongs to no engine operation.
    assert of("language", ["text"], among=engines) == ()
    assert of(None, ["text"], among=engines) == ()
    assert of("language", ["text"]) == ("chat",)


# Pin all existing exact/family rows both with their declared I/O and with no I/O.
_CAPABILITY_ROWS = [
    *[(intent, metadata) for intent, metadata in mm._CAPABILITY_INTENTS.items()],
    *[(prefix + "unfamiliar", metadata) for prefix, metadata in mm._CAPABILITY_INTENT_FAMILIES.items()],
]


@pytest.mark.parametrize("intent,expected", _CAPABILITY_ROWS, ids=[row[0] for row in _CAPABILITY_ROWS])
@pytest.mark.parametrize("declared", [True, False], ids=["declared-io", "empty-io"])
def test_capability_table_rows_keep_exact_metadata(intent, expected, declared):
    inputs = expected.input_modalities if declared else ()
    outputs = expected.output_modalities if declared else ()
    actual = mm.from_capability(intent, inputs, outputs)
    assert actual == expected


def _capability_pair_types():
    pairs = {}
    for _, metadata in _CAPABILITY_ROWS:
        pair = (metadata.input_modalities, metadata.output_modalities)
        pairs.setdefault(pair, set()).add(metadata.type)
    return pairs


@pytest.mark.parametrize(
    "inputs,outputs,expected",
    [(inputs, outputs, next(iter(types)))
     for (inputs, outputs), types in _capability_pair_types().items() if len(types) == 1],
)
def test_unfamiliar_capability_uses_each_unambiguous_pair(inputs, outputs, expected):
    actual = mm.from_capability("smell.infer", inputs, outputs)
    assert actual == mm.ModelMetadata(expected, inputs, outputs)


@pytest.mark.parametrize("intent,inputs,outputs,expected", [
    ("image.sonify", ("text",), ("audio",), "audio"),
    ("image.sonify", ("audio",), ("text",), "audio"),
    ("audio.describe", ("image",), ("text",), "image"),
    ("video.rank", ("text",), ("embedding",), "embedding"),
    ("chat.completions", ("text",), ("audio",), "audio"),
    ("decision.classify", ("text",), ("image",), "image"),
])
def test_unambiguous_declared_pair_overrides_exact_or_family_intent(intent, inputs, outputs, expected):
    assert mm.from_capability(intent, inputs, outputs) == mm.ModelMetadata(expected, inputs, outputs)


@pytest.mark.parametrize("intent,expected", [
    ("chat.completions", "language"),
    ("text.rerank", "reranking"),
    ("decision.choose", "decision"),
    ("smell.infer", "language"),
])
def test_ambiguous_text_pair_uses_intent_then_type_of(intent, expected):
    assert mm.from_capability(intent, ["text"], ["text"]) == mm.ModelMetadata(
        expected, ("text",), ("text",),
    )


@pytest.mark.parametrize("intent,inputs,outputs,expected", [
    ("image.generate", (), ("audio",), mm.ModelMetadata("image", ("text",), ("audio",))),
    ("image.generate", ("audio",), (), mm.ModelMetadata("image", ("audio",), ("image",))),
    ("smell.infer", (), (), None),
    (None, (), (), None),
])
def test_empty_modalities_keep_intent_defaults_or_unknown(intent, inputs, outputs, expected):
    assert mm.from_capability(intent, inputs, outputs) == expected


@pytest.mark.parametrize("intent,expected", [
    ("chat.completions", "language"),
    ("smell.infer", "video"),
])
def test_unlisted_pair_uses_intent_then_type_of(intent, expected):
    assert mm.from_capability(intent, ["video"], ["text"]) == mm.ModelMetadata(
        expected, ("video",), ("text",),
    )


def test_declared_pair_is_a_set_and_one_shot_inputs_are_consumed_once():
    assert mm.from_capability(
        "smell.infer", iter([" IMAGE ", "text", "image"]), iter([" IMAGE ", "image"]),
    ) == mm.ModelMetadata("image", ("text", "image"), ("image",))


def test_io_index_is_derived_from_both_tables_without_ambiguous_pairs(monkeypatch):
    first = mm.ModelMetadata("audio", ("image",), ("audio",))
    conflict = mm.ModelMetadata("video", ("image",), ("audio",))
    family_only = mm.ModelMetadata("embedding", ("audio",), ("embedding",))
    monkeypatch.setattr(mm, "_CAPABILITY_INTENTS", {"unfamiliar.exact": first})
    monkeypatch.setattr(mm, "_CAPABILITY_INTENT_FAMILIES", {
        "unfamiliar.conflict.": conflict, "unfamiliar.family.": family_only,
    })
    assert mm._capability_types_by_io() == {
        (frozenset({"audio"}), frozenset({"embedding"})): "embedding",
    }


def test_manifest_output_modality_is_the_media_major_named_by_the_contract(monkeypatch):
    outputs = {"outputs": {
        "a": {"media_type": "IMAGE/PNG"}, "b": {"media_type": "video/mp4"},
        "c": {"media_type": "audio/wav"}, "d": {"media_type": "text/plain"},
        "e": {"media_type": "application/json"}, "f": {"media_type": "model/gltf-binary"},
    }}
    assert mm._manifest_output_modalities(outputs) == ["image", "video", "audio", "text", "text"]
    # A modality the contract gains in data is read from a declared media type with no code edit.
    monkeypatch.setattr(mm, "OUTPUT_MODALITIES", (*mm.OUTPUT_MODALITIES, "model"))
    assert mm._manifest_output_modalities(outputs)[-1] == "model"
