"""Literal type/audio answers and isolated data-only consumer contracts."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from gateway.capabilities import Capability
from gateway import model_presentation as presentation

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "gateway" / "capability_ids.py"
CONSUMER = ROOT / "gateway" / "model_presentation.py"
DATA = ROOT / "registry" / "capability_words.yml"
TYPE_CAPABILITIES = {
    "language": "chat", "image": "image", "video": "video",
    "embedding": "embed", "reranking": "rerank", "decision": "decide",
}
AUDIO_RULE = {
    "type": "audio", "input": "audio", "absent_output": "audio",
    "result": "transcribe", "subtypes": ("music", "sfx", "speech"), "fallback": "audio",
}


def load(source, name):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(tmp_path, document):
    source = tmp_path / "gateway" / "capability_ids.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    data = tmp_path / "registry" / "capability_words.yml"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return source, data


def document():
    return yaml.safe_load(DATA.read_text(encoding="utf-8"))


def consumer(module, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.capability_ids", module)
    return load(CONSUMER, "_type_audio_consumer")


def test_listed_type_overrides_inferred_contract_and_declared_capability():
    cap = Capability(intent="chat.completions", backend="demo", capability="music")
    listed = {"type": "reranking", "architecture": {
        "input_modalities": ["text"], "output_modalities": ["text"],
    }}
    assert presentation.product_capability(cap) == "chat"
    assert presentation.product_capability(cap, {presentation.LISTED_METADATA_KEY: listed}) == "rerank"


@pytest.mark.parametrize("specific,label", [
    ("speech", "Voice"), ("transcriptions", "Transcription"),
    ("music", "Music"), ("sfx", "Sound effects"),
])
def test_each_specific_audio_label_hides_generic_audio(specific, label):
    assert presentation.service_capability_labels({"capabilities": ["audio", specific, "audio"]}) == [label]
    assert presentation.service_capability_labels({"capabilities": [specific, "audio"]}) == [label]


@pytest.mark.parametrize("metadata,tags,intent,expected", [
    ({"tags": ["capability:music"]}, ("capability:sfx",), "speech.synthesis", "music"),
    ({}, ("capability:sfx",), "speech.synthesis", "sfx"),
    ({}, (), "speech.synthesis", "speech"),
    ({}, (), "audio.speech", "speech"),
    ({}, (), "audio.generate", "audio"),
])
def test_tag_only_then_intent_only_then_fallback_precedence(metadata, tags, intent, expected):
    cap = Capability(intent=intent, backend="demo", tags=tags)
    assert presentation.product_capability(cap, metadata) == expected


def test_audio_to_text_precedes_all_subtype_hints():
    cap = Capability(intent="audio.transcriptions", backend="demo", capability="sfx",
                     input_modalities=("audio",), output_modalities=("text",),
                     tags=("capability:speech",))
    assert presentation.product_capability(cap, {"capability": "music"}) == "transcribe"


def test_capability_subtype_precedes_tags_and_intent():
    cap = Capability(intent="speech.synthesis", backend="demo", capability="sfx", tags=("capability:music",))
    assert presentation.product_capability(cap) == "sfx"


def test_shipped_data_equals_literal_answers(tmp_path):
    source, _ = tree(tmp_path, document())
    rows = load(source, "_type_audio_rows")
    assert dict(rows.TYPE_CAPABILITIES) == TYPE_CAPABILITIES
    assert dict(rows.AUDIO_RULE) == AUDIO_RULE
    assert dict(rows.CAPABILITY_HIDDEN_BY) == {"audio": ("speech", "transcribe", "music", "sfx")}


def test_new_type_is_a_data_decision_after_the_parser_boundary(tmp_path, monkeypatch):
    data = document()
    data["capabilities"].append({"id": "fixture", "label": "Fixture"})
    data.setdefault("type_capabilities", {})["fixture-type"] = "fixture"
    source, _ = tree(tmp_path, data)
    rows = load(source, "_type_audio_rows")
    model = consumer(rows, monkeypatch)
    # The contract parser is deliberately outside this extraction. Supply its
    # already-parsed result; only the type-to-product decision is under test.
    parsed = SimpleNamespace(type="fixture-type", input_modalities=("text",), output_modalities=("asset",))
    monkeypatch.setattr(model.model_metadata, "from_fields", lambda _fields: parsed)
    assert model.product_capability("unlisted.operation", {model.LISTED_METADATA_KEY: {}}) == "fixture"


def test_new_audio_subtype_and_suppression_need_only_rows(tmp_path, monkeypatch):
    data = document()
    data["capabilities"].append({"id": "fixture", "label": "Fixture"})
    data["audio_rule"]["subtypes"].append("fixture")
    next(row for row in data["capabilities"] if row["id"] == "audio")["hidden_by"].append("fixture")
    data["protocol_words"]["fixture-word"] = "fixture"
    source, _ = tree(tmp_path, data)
    model = consumer(load(source, "_type_audio_rows"), monkeypatch)
    assert model.product_capability("audio.generate", {"capability": "fixture"}) == "fixture"
    assert model.service_capability_labels({"capabilities": ["audio", "fixture-word"]}) == ["Fixture"]


@pytest.mark.parametrize("section,key,value,reason", [
    ("document", "unexpected", True, "unknown"),
    ("type_capabilities", None, [], "mapping"),
    ("type_capabilities", "", "chat", "non-empty string"),
    ("type_capabilities", "new", "unknown", "unknown capability"),
    ("audio_rule", None, [], "mapping"),
    ("audio_rule", "extra", True, "unknown"),
    ("audio_rule", "type", "", "non-empty string"),
    ("audio_rule", "input", 3, "non-empty string"),
    ("audio_rule", "absent_output", [], "non-empty string"),
    ("audio_rule", "result", "unknown", "unknown capability"),
    ("audio_rule", "fallback", "unknown", "unknown capability"),
    ("audio_rule", "subtypes", [], "non-empty list"),
    ("audio_rule", "subtypes", ["unknown"], "unknown capability"),
    ("audio_rule", "subtypes", "speech", "list"),
    ("hidden_by", None, "speech", "list"),
    ("hidden_by", None, ["unknown"], "unknown capability"),
])
def test_invalid_rules_name_file_row_and_reason(tmp_path, section, key, value, reason):
    data = document()
    if section == "document":
        data[key] = value
    elif section == "hidden_by":
        next(row for row in data["capabilities"] if row["id"] == "audio")["hidden_by"] = value
    elif key is None:
        data[section] = value
    else:
        data[section][key] = value
    source, _ = tree(tmp_path, data)
    with pytest.raises(ValueError) as error:
        load(source, "_type_audio_rows")
    message = str(error.value)
    assert "capability_words.yml" in message and "row" in message
    assert section in message and reason in message


@pytest.mark.parametrize("key", ["type", "input", "absent_output", "result", "subtypes", "fallback"])
def test_each_audio_rule_field_is_required(tmp_path, key):
    data = document()
    del data["audio_rule"][key]
    source, _ = tree(tmp_path, data)
    with pytest.raises(ValueError) as error:
        load(source, "_type_audio_rows")
    assert "capability_words.yml" in str(error.value) and "row" in str(error.value)
    assert "audio_rule" in str(error.value) and key in str(error.value) and "missing" in str(error.value)


def test_rule_maps_are_deeply_read_only_and_shared(tmp_path, monkeypatch):
    source, data = tree(tmp_path, document())
    rows = load(source, "_type_audio_rows")
    model = consumer(rows, monkeypatch)
    types, audio, hidden = rows.load_capability_classification(data)
    assert model.TYPE_CAPABILITIES is rows.TYPE_CAPABILITIES
    assert model.AUDIO_RULE is rows.AUDIO_RULE
    assert model.CAPABILITY_HIDDEN_BY is rows.CAPABILITY_HIDDEN_BY
    for table, key in [(types, "language"), (audio, "fallback"), (hidden, "audio")]:
        with pytest.raises(TypeError):
            table[key] = "changed"
    with pytest.raises(TypeError):
        audio["subtypes"][0] = "changed"
    with pytest.raises(TypeError):
        hidden["audio"][0] = "changed"


def test_rules_load_once_and_old_loader_shapes_survive(tmp_path, monkeypatch):
    data = document()
    source, path = tree(tmp_path, data)
    rows = load(source, "_type_audio_rows")
    model = consumer(rows, monkeypatch)
    data["type_capabilities"]["language"] = "image"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert rows.TYPE_CAPABILITIES["language"] == "chat"
    assert rows.load_capability_classification(path)[0]["language"] == "image"
    assert len(rows._load_capability_data(path)) == 4
    assert len(rows.load_capability_words(path)) == 2
    assert len(rows.load_capability_presentation(path)) == 3
    path.unlink()
    assert model.product_capability("chat.completions") == "chat"
