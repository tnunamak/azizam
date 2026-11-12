"""Capability word data: frozen base tables, import-time validation and real reloads."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gateway" / "capability_ids.py"
DATA = ROOT / "registry" / "capability_words.yml"

# Literal snapshots from 4dc985fc, not derived from the loader or the new YAML.
PROTOCOL = {
    "llm.execution": "chat",
    "typesafe-systemone": "decide",
    "chat": "chat",
    "completions": "chat",
    "chat.completions": "chat",
    "vision": "chat",
    "speech": "speech",
    "tts": "speech",
    "audio.speech": "speech",
    "speech.synthesis": "speech",
    "transcriptions": "transcribe",
    "transcription": "transcribe",
    "stt": "transcribe",
    "audio.transcriptions": "transcribe",
    "speech.transcription": "transcribe",
    "speech.translation": "transcribe",
    "images": "image",
    "image": "image",
    "generations": "image",
    "image.generate": "image",
    "image.edit": "image",
    "videos": "video",
    "video": "video",
    "video.generate": "video",
    "embeddings": "embed",
    "text.embeddings": "embed",
    "rerank": "rerank",
    "text.rerank": "rerank",
    "decisions": "decide",
    "decision.systemone": "decide",
    "music": "music",
    "sfx": "sfx",
    "audio": "audio",
    "audio.generate": "audio",
    "mesh": "mesh",
}
EXTRA_INTENTS = {
    "text.completions": "chat",
    "chat.responses": "chat",
    "chat.generate": "chat",
    "chat.messages": "chat",
    "chat.messages.count_tokens": "chat",
    "music.generate": "music",
    "sfx.generate": "sfx",
    "image.variation": "image",
    "video.remix": "video",
    "video.edit": "video",
    "video.extend": "video",
    "image.analyze": "image",
    "mesh.generate": "mesh",
    "embedding": "embed",
    "images.generate": "image",
    "images.generations": "image",
    "images.edit": "image",
    "images.edits": "image",
    "audio.transcribe": "transcribe",
    "audio.translations": "transcribe",
    "videos.generate": "video",
}
EXPECTED_INTENTS = {**PROTOCOL, **EXTRA_INTENTS}


def _document():
    return {
        "schema_version": 1,
        "capabilities": [
            {"id": "chat", "label": "Chat"}, {"id": "image", "label": "Images"},
            {"id": "video", "label": "Video"}, {"id": "speech", "label": "Voice"},
            {"id": "transcribe", "label": "Transcription"}, {"id": "music", "label": "Music"},
            {"id": "sfx", "label": "Sound effects"}, {"id": "audio", "label": "Audio"},
            {"id": "decide", "label": "Decisions"}, {"id": "embed", "label": "Document search"},
            {"id": "rerank", "label": "Reranking"}, {"id": "mesh", "label": "3D models"},
        ],
        "protocol_words": dict(PROTOCOL),
        "route_intents": dict(EXTRA_INTENTS),
    }


def _tree(tmp_path, document):
    source = tmp_path / "gateway" / "capability_ids.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    data = tmp_path / "registry" / "capability_words.yml"
    data.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    data.write_text(text, encoding="utf-8")
    return source, data


def _import(source):
    # Execute the real leaf module in its own tree, so its __file__-relative
    # data lookup is exercised without reloading or mutating shared imports.
    spec = importlib.util.spec_from_file_location("_capability_words_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loaded_maps_equal_literal_base_tables_including_order(tmp_path):
    source, _ = _tree(tmp_path, DATA.read_text(encoding="utf-8"))
    module = _import(source)
    assert dict(module.PROTOCOL_CAPABILITIES) == PROTOCOL
    assert list(module.PROTOCOL_CAPABILITIES.items()) == list(PROTOCOL.items())
    assert dict(module.INTENT_CAPABILITIES) == EXPECTED_INTENTS
    assert list(module.INTENT_CAPABILITIES.items()) == list(EXPECTED_INTENTS.items())
    assert len(module.PROTOCOL_CAPABILITIES) == 35
    assert len(module.INTENT_CAPABILITIES) == 56
    for word, capability in EXPECTED_INTENTS.items():
        assert module.capability_of_word(word) == capability


def test_shipped_file_has_only_extra_intents_and_no_duplicate_transcription():
    document = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    assert list(document) == [
        "schema_version", "modality_words", "media_type_modalities", "capabilities", "protocol_words", "route_intents", "type_capabilities", "audio_rule",
    ]
    assert document["schema_version"] == 1
    assert document["modality_words"] == ["text", "image", "audio", "video", "mesh"]
    assert document["media_type_modalities"] == {"model/gltf-binary": "mesh"}
    assert list(document["protocol_words"].items()) == list(PROTOCOL.items())
    assert list(document["route_intents"].items()) == list(EXTRA_INTENTS.items())
    assert "audio.transcriptions" not in document["route_intents"]


@pytest.mark.parametrize("name", ["PROTOCOL_CAPABILITIES", "INTENT_CAPABILITIES"])
def test_exported_maps_are_read_only(tmp_path, name):
    source, _ = _tree(tmp_path, _document())
    table = getattr(_import(source), name)
    with pytest.raises(TypeError):
        table["tts"] = "mesh"
    with pytest.raises(TypeError):
        del table["tts"]


@pytest.mark.parametrize("document, word", [
    (None, "<document>"),
    ([], "<document>"),
    ({}, "schema_version"),
    ({"schema_version": 1, "capabilities": _document()["capabilities"]}, "protocol_words"),
    ({"schema_version": 1, "capabilities": _document()["capabilities"], "protocol_words": {}}, "route_intents"),
    ({**_document(), "surprise": {}}, "surprise"),
    ({**_document(), 42: {}}, "42"),
    ({**_document(), "schema_version": 2}, "schema_version"),
    ({**_document(), "schema_version": True}, "schema_version"),
    ({**_document(), "schema_version": "1"}, "schema_version"),
    ({**_document(), "protocol_words": []}, "protocol_words"),
    ({**_document(), "route_intents": None}, "route_intents"),
])
def test_invalid_document_fails_import_with_file_and_key(tmp_path, document, word):
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _import(source)
    assert "capability_words.yml" in str(error.value)
    assert word in str(error.value)


@pytest.mark.parametrize("section", ["protocol_words", "route_intents"])
@pytest.mark.parametrize("word", ["", "   ", 7, None])
def test_invalid_word_fails_import_with_file_and_word(tmp_path, section, word):
    document = _document()
    document[section] = {word: "chat"}
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _import(source)
    assert "capability_words.yml" in str(error.value)
    assert repr(word) in str(error.value)


@pytest.mark.parametrize("section", ["protocol_words", "route_intents"])
@pytest.mark.parametrize("capability", ["unlisted-capability", "", None, 1, []])
def test_invalid_capability_fails_import_with_file_and_word(tmp_path, section, capability):
    document = _document()
    document[section] = {"unfamiliar.word": capability}
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _import(source)
    assert "capability_words.yml" in str(error.value)
    assert "unfamiliar.word" in str(error.value)


def test_conflicting_word_in_both_maps_fails_import(tmp_path):
    document = _document()
    document["route_intents"]["tts"] = "music"
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _import(source)
    assert "capability_words.yml" in str(error.value)
    assert "tts" in str(error.value)


def test_same_capability_in_both_maps_preserves_protocol_position(tmp_path):
    document = _document()
    document["route_intents"]["tts"] = "speech"
    source, _ = _tree(tmp_path, document)
    module = _import(source)
    assert list(module.INTENT_CAPABILITIES.items()) == list(EXPECTED_INTENTS.items())


def test_changed_data_changes_capability_lookup_on_import_only(tmp_path):
    document = _document()
    document["protocol_words"]["tts"] = "music"
    document["route_intents"]["unfamiliar.word"] = "mesh"
    source, data = _tree(tmp_path, document)
    module = _import(source)
    assert module.capability_of_word("tts") == "music"
    assert module.capability_of_word("  unfamiliar.word  ") == "mesh"
    assert module.capability_of_word("unknown.word") == "unknown.word"
    assert module.capability_of_word(None) == ""
    assert module.capability_of_word("  ") == ""
    data.write_text(yaml.safe_dump(_document(), sort_keys=False), encoding="utf-8")
    assert module.capability_of_word("tts") == "music", "the import-time table must not hot-reload"
    assert _import(source).capability_of_word("tts") == "speech"
    data.unlink()
    assert module.capability_of_word("tts") == "music", "lookups must not re-read the data file"


@pytest.mark.parametrize("text", ["protocol_words: [", "!!python/object/apply:os.system ['false']"])
def test_invalid_yaml_fails_import_with_filename(tmp_path, text):
    source, _ = _tree(tmp_path, text)
    with pytest.raises(ValueError, match="capability_words.yml"):
        _import(source)


def test_missing_data_fails_import_with_filename(tmp_path):
    source, data = _tree(tmp_path, _document())
    data.unlink()
    with pytest.raises(ValueError, match="capability_words.yml"):
        _import(source)

# Member words are a separate projection; existing labels and protocol APIs stay unchanged.
TASK_NAMES = {
    "chat": "Chat", "image": "Make a picture", "video": "Make a video",
    "speech": "Read text aloud", "transcribe": "Transcribe a recording",
    "music": "Make music", "sfx": "Make a sound effect", "audio": "Make music or sounds",
    "decide": "Choose an option", "embed": "Turn text into numbers",
    "rerank": "Sort search results", "mesh": "Make a 3D model",
}


def test_member_words_match_literal_golden_and_row_order(tmp_path):
    source, data = _tree(tmp_path, DATA.read_text(encoding="utf-8"))
    module = _import(source)
    assert dict(module.CAPABILITY_TASK_NAMES) == TASK_NAMES
    assert list(module.CAPABILITY_TASK_NAMES) == list(TASK_NAMES)
    labels = {row["id"]: row["label"] for row in _document()["capabilities"]}
    labels.update(decide="Choose an option", embed="Turn text into numbers")
    assert dict(module.CAPABILITY_MEMBER_LABELS) == labels
    assert list(module.CAPABILITY_MEMBER_LABELS) == list(TASK_NAMES)
    assert module.load_capability_member_words(data) == (
        module.CAPABILITY_MEMBER_LABELS, module.CAPABILITY_TASK_NAMES,
    )


@pytest.mark.parametrize("field", ["member_label", "task_name"])
@pytest.mark.parametrize("value", [None, 7, True, [], {}, "", "   ", " padded", "padded ", "\tword\n"])
def test_invalid_member_word_fails_loading(tmp_path, field, value):
    document = _document()
    document["capabilities"][0][field] = value
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError, match=field):
        _import(source)


def test_member_word_fallbacks_and_legacy_loader_shapes(tmp_path):
    document = _document()
    document["capabilities"][1]["member_label"] = "Member image"
    document["capabilities"][2].update(member_label="Member video", task_name="Task video")
    document["capabilities"][3]["task_name"] = "Task speech"
    source, data = _tree(tmp_path, document)
    module = _import(source)
    members, tasks = module.load_capability_member_words(data)
    assert (members["chat"], tasks["chat"]) == ("Chat", "Chat")
    assert (members["image"], tasks["image"]) == ("Member image", "Member image")
    assert (members["video"], tasks["video"]) == ("Member video", "Task video")
    assert (members["speech"], tasks["speech"]) == ("Voice", "Task speech")
    assert len(module._load_capability_data(data)) == 4
    assert len(module.load_capability_presentation(data)) == 3
    assert len(module.load_capability_classification(data)) == 3
    assert len(module.load_capability_words(data)) == 2
    assert dict(module.load_capability_display_order(data)) == {
        row["id"]: index for index, row in enumerate(document["capabilities"])
    }


@pytest.mark.parametrize("name", ["CAPABILITY_MEMBER_LABELS", "CAPABILITY_TASK_NAMES"])
def test_member_word_maps_are_immutable(tmp_path, name):
    source, data = _tree(tmp_path, _document())
    module = _import(source)
    maps = [getattr(module, name), *module.load_capability_member_words(data)]
    for table in maps:
        with pytest.raises(TypeError):
            table["chat"] = "Changed"
        with pytest.raises(TypeError):
            del table["chat"]
