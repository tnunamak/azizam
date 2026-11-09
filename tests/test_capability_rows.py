"""Literal capability labels and Home protocols, plus row-loader contracts."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gateway" / "capability_ids.py"
DATA = ROOT / "registry" / "capability_words.yml"

# Frozen from 2a0ee023; do not derive expected answers from the shipped rows.
LABELS = [
    ("chat", "Chat"), ("image", "Images"), ("video", "Video"),
    ("speech", "Voice"), ("transcribe", "Transcription"), ("music", "Music"),
    ("sfx", "Sound effects"), ("audio", "Audio"), ("decide", "Decisions"),
    ("embed", "Document search"), ("rerank", "Reranking"), ("mesh", "3D models"),
]
ORDER = ("chat", "image", "video", "speech", "transcribe", "music",
         "sfx", "audio", "decide", "embed", "rerank", "mesh")
MODEL_LABELS = {"chat": "Chat", "embed": "Document search",
                "speech": "Voice", "transcribe": "Transcription"}
HOME_PROTOCOLS = [
    ("chat", "openai.chat"), ("image", "openai.images"), ("video", "openai.video"),
    ("speech", "openai.audio"), ("transcribe", "openai.audio"), ("music", "openai.audio"),
    ("sfx", "openai.audio"), ("audio", "openai.audio"), ("decide", "openai.chat"),
    ("embed", "openai.embeddings"), ("rerank", "openai.chat"), ("mesh", "openai.mesh"),
    ("other", "openai.chat"), ("unfamiliar.word", "openai.chat"),
]


def _rows():
    protocols = dict(HOME_PROTOCOLS)
    return [{"id": key, "label": label,
             **({"policy_protocol": protocols[key]} if protocols[key] != "openai.chat" else {})}
            for key, label in LABELS]


def _document():
    return {"schema_version": 1, "capabilities": _rows(),
            "protocol_words": {"tts": "speech"}, "route_intents": {"chat.generate": "chat"}}


def _import_tree(tmp_path, document):
    source = tmp_path / "gateway" / "capability_ids.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    data = tmp_path / "registry" / "capability_words.yml"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_capability_rows_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, data


def test_labels_order_and_model_kinds_match_literal_base():
    from gateway.capability_ids import CAPABILITY_LABELS, MODEL_KIND_LABELS, MODEL_KINDS
    from gateway.access_plans import CAPABILITIES

    assert list(CAPABILITY_LABELS.items()) == LABELS
    assert tuple(CAPABILITY_LABELS) == ORDER
    assert CAPABILITIES == ORDER
    assert MODEL_KIND_LABELS == MODEL_LABELS
    assert tuple(MODEL_KIND_LABELS) == ("chat", "embed", "speech", "transcribe")
    assert MODEL_KINDS == ("chat", "embed", "speech", "transcribe")


@pytest.mark.parametrize("capability, protocol", HOME_PROTOCOLS)
def test_home_policy_request_uses_literal_protocol(monkeypatch, capability, protocol):
    from gateway import home_api

    captured = []
    monkeypatch.setattr(home_api, "product_capability", lambda _cap: capability)
    monkeypatch.setattr(home_api, "make_policy_request", lambda **kwargs: kwargs)

    def policy_view(cap, _access, *, request, identity):
        captured.append(request["protocol"])
        return cap

    monkeypatch.setattr(home_api, "capability_with_policy_view", policy_view)
    identity = SimpleNamespace(access=SimpleNamespace(lists_items=True))
    cap = home_api.Capability(intent="fixture.generate", backend="fixture-service",
                              workflow="fixture-workflow")
    result = home_api._resolved_capabilities(identity, lambda _identity: [cap])
    assert len(result) == 1
    assert captured == [protocol]


def test_shipped_rows_are_the_literal_display_order_with_only_protocol_overrides():
    document = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    rows = document.get("capabilities")
    base_fields = {"id", "label", "policy_protocol"}
    allowed_fields = base_fields | {"icon", "recommended_alias", "summary"}
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) and set(row) <= allowed_fields for row in rows)
    assert [{key: value for key, value in row.items() if key in base_fields}
            for row in rows] == _rows()


@pytest.mark.parametrize("name", ["CAPABILITY_LABELS", "POLICY_PROTOCOLS"])
def test_row_tables_are_read_only(tmp_path, name):
    module, _ = _import_tree(tmp_path, _document())
    table = getattr(module, name)
    with pytest.raises(TypeError):
        table["chat"] = "changed"
    with pytest.raises(TypeError):
        del table["chat" if name == "CAPABILITY_LABELS" else "embed"]


@pytest.mark.parametrize("rows, row", [
    (None, "capabilities"),
    ({}, "capabilities"),
    ([], "capabilities"),
    ("chat", "capabilities"),
    ([None], 1),
    ([{"label": "Missing id"}], 1),
    ([{"id": 7, "label": "Bad id"}], 1),
    ([{"id": "", "label": "Bad id"}], 1),
    ([{"id": "   ", "label": "Bad id"}], 1),
    ([{"id": "chat"}], 1),
    ([{"id": "chat", "label": ""}], 1),
    ([{"id": "chat", "label": "   "}], 1),
    ([{"id": "chat", "label": None}], 1),
    ([{"id": "chat", "label": 7}], 1),
    ([{"id": "chat", "label": []}], 1),
    ([{"id": "chat", "label": {}}], 1),
    ([{"id": "chat", "label": True}], 1),
    ([{"id": "chat", "label": "Chat", "surprise": True}], 1),
    ([{"id": "chat", "label": "Chat", "policy_protocol": None}], 1),
    ([{"id": "chat", "label": "Chat", "policy_protocol": 7}], 1),
    ([{"id": "chat", "label": "Chat", "policy_protocol": []}], 1),
    ([{"id": "chat", "label": "Chat", "policy_protocol": {}}], 1),
    ([{"id": "chat", "label": "Chat", "policy_protocol": True}], 1),
    ([{"id": "chat", "label": "Chat"}, {"id": "chat", "label": "Duplicate"}], 2),
])
def test_invalid_capability_row_names_file_and_row(tmp_path, rows, row):
    document = _document()
    document["capabilities"] = rows
    with pytest.raises(ValueError) as error:
        _import_tree(tmp_path, document)
    assert "capability_words.yml" in str(error.value)
    assert f"row {row!r}" in str(error.value)


@pytest.mark.parametrize("section", ["protocol_words", "route_intents"])
def test_word_maps_validate_against_loaded_row_ids(tmp_path, section):
    document = _document()
    document["capabilities"] = [row for row in document["capabilities"] if row["id"] != "mesh"]
    document[section]["fixture.word"] = "mesh"
    with pytest.raises(ValueError) as error:
        _import_tree(tmp_path, document)
    assert "capability_words.yml" in str(error.value)
    assert "fixture.word" in str(error.value)
    assert "unknown capability 'mesh'" in str(error.value)


def test_test_only_row_extends_labels_and_both_word_maps_without_code(tmp_path):
    document = _document()
    document["capabilities"].append({"id": "fixture", "label": "Fixture",
                                     "policy_protocol": "fixture.protocol"})
    document["protocol_words"]["fixture.word"] = "fixture"
    document["route_intents"]["fixture.generate"] = "fixture"
    module, data = _import_tree(tmp_path, document)
    assert list(module.CAPABILITY_LABELS.items()) == [*LABELS, ("fixture", "Fixture")]
    assert tuple(module.CAPABILITY_LABELS) == (*ORDER, "fixture")
    assert module.POLICY_PROTOCOLS["fixture"] == "fixture.protocol"
    assert module.capability_of_word("fixture.word") == "fixture"
    assert module.capability_of_word("fixture.generate") == "fixture"
    assert module.MODEL_KIND_LABELS == MODEL_LABELS
    protocol, intents = module.load_capability_words(data)
    assert protocol["fixture.word"] == intents["fixture.generate"] == "fixture"
    changed = copy.deepcopy(document)
    changed["capabilities"][-1]["label"] = "Changed"
    data.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    assert module.CAPABILITY_LABELS["fixture"] == "Fixture", "tables load once at import"
    data.unlink()
    assert module.POLICY_PROTOCOLS["fixture"] == "fixture.protocol"
