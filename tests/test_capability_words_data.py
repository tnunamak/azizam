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
    assert list(document) == ["schema_version", "capabilities", "protocol_words", "route_intents"]
    assert document["schema_version"] == 1
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
