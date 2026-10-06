"""Unit tests for gateway/model_metadata.py (the /v1/models metadata contract)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

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
    entry = {"id": model_id, "object": "model", "vivid_fish_intent": intent, "vivid_fish_workflow_id": f"{intent}.{model_id}"}
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
        fields = self._workflow_fields("image.edit", {"inputs": {"source_image": {"type": "asset"}}, "outputs": {"image": {"media_type": "image/png"}}})
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text", "image"], ["image"]))

    def test_image_analyze_uses_manifest_output(self) -> None:
        fields = self._workflow_fields("image.analyze", {"inputs": {"candidate": {"type": "asset"}}, "outputs": {"report": {"media_type": "application/json"}}})
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["image"], ["text"]))

    def test_video_generate_text_only(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"prompt": {"type": "string"}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(fields["type"], "video")
        self.assertEqual(_arch(fields), (["text"], ["video"]))

    def test_video_generate_with_input_image(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"prompt": {"type": "string"}, "image": {"type": "asset"}}, "outputs": {"video": {"media_type": "video/mp4"}}})
        self.assertEqual(_arch(fields), (["text", "image"], ["video"]))

    def test_video_generate_asset_media_type_is_respected(self) -> None:
        fields = self._workflow_fields("video.generate", {"inputs": {"clip": {"type": "asset", "media_type": "audio/wav"}}, "outputs": {}})
        self.assertEqual(_arch(fields), (["text", "audio"], ["video"]))

    def test_audio_generate(self) -> None:
        fields = self._workflow_fields("audio.generate", {"inputs": {}, "outputs": {"audio": {"media_type": "audio/wav"}}})
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
                fields = _annotated([{"id": "a", "vivid_fish_resolves_to": {op: "unlisted-target"}}])["a"]
                self.assertEqual(fields["type"], type_)
                self.assertEqual(_arch(fields), (inputs, outputs))

    def test_alias_chat_gets_image_input_only_if_target_declares_vision(self) -> None:
        declared = {"vlm": {"tags": ["vision"]}}
        sources = mm.MetadataSources(declared=declared.get, origins={"vlm": mm.ORIGIN_LLM_CATALOG, "llm": mm.ORIGIN_LLM_CATALOG})
        out = _annotated([
            {"id": "vlm"}, {"id": "llm"},
            {"id": "vision", "vivid_fish_resolves_to": {"chat": "vlm"}},
            {"id": "plain", "vivid_fish_resolves_to": {"chat": "llm"}},
        ], sources)
        self.assertEqual(_arch(out["vision"])[0], ["text", "image"])
        self.assertIn("vision", out["vision"]["tags"])
        self.assertEqual(_arch(out["plain"])[0], ["text"])
        self.assertNotIn("tags", out["plain"])

    def test_alias_with_two_operations_takes_shared_type(self) -> None:
        fields = _annotated([{"id": "image", "vivid_fish_resolves_to": {"images": "g", "image_edits": "e"}}])["image"]
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text", "image"], ["image"]))

    def test_alias_with_no_shared_type_is_untyped(self) -> None:
        fields = _annotated([{"id": "mixed", "vivid_fish_resolves_to": {"chat": "x", "speech": "y"}}])["mixed"]
        self.assertNotIn("type", fields)
        self.assertNotIn("architecture", fields)

    def test_alias_target_listed_in_catalog_wins_over_operation_default(self) -> None:
        entry, manifests = _workflow("i2v", "video.generate", {"inputs": {"image": {"type": "asset"}}, "outputs": {}})
        sources = mm.MetadataSources(manifest=manifests.get)
        out = _annotated([{"id": "video", "vivid_fish_resolves_to": {"videos": "i2v"}}], sources, catalog=[entry])
        self.assertEqual(_arch(out["video"]), (["text", "image"], ["video"]))

    def test_alias_target_resolves_through_workflow_registry_when_unlisted(self) -> None:
        manifest = {"intent": "video.edit", "inputs": {"source_video": {"type": "asset"}}, "outputs": {"video": {"media_type": "video/mp4"}}}
        sources = mm.MetadataSources(workflow_for_model={"remix-wf": manifest}.get)
        out = _annotated([{"id": "video-remix", "vivid_fish_resolves_to": {"videos": "remix-wf"}}], sources)
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
            fields = _annotated([{"id": model_id, "vivid_fish_current_loaded": True}])[model_id]
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
        fields = _annotated([{"id": "e", "vivid_fish_intent": "text.embeddings"}])["e"]
        self.assertEqual(fields["type"], "embedding")


class OverrideTests(unittest.TestCase):
    def test_declared_values_win_over_derived(self) -> None:
        declared = {"m": {"type": "image", "input_modalities": ["text"], "output_modalities": ["image"], "max_tokens": 4096, "tags": ["reasoning"]}}
        sources = mm.MetadataSources(declared=declared.get, origins={"m": mm.ORIGIN_LLM_CATALOG})
        fields = _annotated([{"id": "m"}], sources)["m"]
        self.assertEqual(fields["type"], "image")
        self.assertEqual(_arch(fields), (["text"], ["image"]))
        self.assertEqual(fields["max_tokens"], 4096)
        self.assertEqual(fields["tags"], ["reasoning"])

    def test_declaration_alone_classifies_an_unknown_entry(self) -> None:
        declared = {"mystery": {"type": "reranking", "input_modalities": ["text"], "output_modalities": ["text"]}}
        fields = _annotated([{"id": "mystery"}], mm.MetadataSources(declared=declared.get))["mystery"]
        self.assertEqual(fields["type"], "reranking")

    def test_declaration_on_alias_wins_over_target(self) -> None:
        declared = {"t": {"tags": ["reasoning"]}, "a": {"tags": []}}
        sources = mm.MetadataSources(declared=declared.get, origins={"t": mm.ORIGIN_LLM_CATALOG})
        out = _annotated([{"id": "t"}, {"id": "a", "vivid_fish_resolves_to": {"chat": "t"}}], sources)
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
        catalog = [{"id": "vlm"}, {"id": "vision", "vivid_fish_resolves_to": {"chat": "vlm"}}]
        visible = _annotated([catalog[1]], sources, catalog=catalog)
        self.assertIn("image", _arch(visible["vision"])[0])


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
        entries = [{"id": f"a-{op}", "vivid_fish_resolves_to": {op: "t"}} for op in ("chat", "images", "image_edits", "videos", "speech", "transcriptions")]
        for fields in _annotated(entries).values():
            self.assertEqual("vision" in fields.get("tags", []), "image" in _arch(fields)[0], fields["id"])

    def test_unknown_entry_gets_no_fields(self) -> None:
        fields = _annotated([{"id": "who-knows", "object": "model"}])["who-knows"]
        for key in mm.OUTPUT_FIELDS:
            self.assertNotIn(key, fields)

    def test_max_tokens_and_tags_omitted_unless_known(self) -> None:
        fields = _annotated([{"id": "c", "vivid_fish_current_loaded": True}])["c"]
        self.assertNotIn("max_tokens", fields)
        self.assertNotIn("tags", fields)
        self.assertNotIn("context_window", fields)

    def test_context_window_passes_through_untouched(self) -> None:
        fields = _annotated([{"id": "c", "vivid_fish_current_loaded": True, "context_window": 1234}])["c"]
        self.assertEqual(fields["context_window"], 1234)

    def test_existing_vivid_fish_fields_are_untouched(self) -> None:
        entry = {"id": "image", "vivid_fish_alias": True, "vivid_fish_resolves_to": {"images": "x"}, "owned_by": "vivid-fish"}
        fields = _annotated([entry])["image"]
        for key, value in entry.items():
            self.assertEqual(fields[key], value)

    def test_no_new_field_uses_a_vivid_fish_name(self) -> None:
        for name in (*mm.OUTPUT_FIELDS, *mm.DECLARABLE_FIELDS, mm.DECLARATION_KEY):
            self.assertNotIn("vivid_fish", name)
        entry = {"id": "a", "vivid_fish_resolves_to": {"chat": "t"}}
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
        entries = [{"id": "embed", "vivid_fish_resolves_to": {"embeddings": "e"}}]
        index = mm.metadata_index(entries, mm.MetadataSources())
        self.assertEqual(index["embed"]["type"], "embedding")
        self.assertNotIn("vivid_fish_resolves_to", index["embed"])


class EngineAgnosticTests(unittest.TestCase):
    def test_module_names_no_engine(self) -> None:
        source = (ROOT / "gateway" / "model_metadata.py").read_text(encoding="utf-8").lower()
        for engine in ("vllm", "ollama", "llama", "kobold", "tabby", "qwen", "comfy", "litellm"):
            self.assertNotIn(engine, source)


if __name__ == "__main__":
    unittest.main()
