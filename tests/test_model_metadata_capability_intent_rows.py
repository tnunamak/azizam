"""Literal capability-intent answers and strict, immutable shipped row loading."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from gateway import model_metadata as mm

ROWS = Path(__file__).resolve().parents[1] / "registry" / "capability_intent_metadata.yml"

# All expectations are literal, independent of the shipped file and production tables.
EXACT = (
    ("image.generate", "image", ("text",), ("image",)),
    ("image.edit", "image", ("text", "image"), ("image",)),
    ("image.analyze", "image", ("image",), ("text",)),
    ("video.generate", "video", ("text",), ("video",)),
    ("video.edit", "video", ("text", "video"), ("video",)),
    ("audio.generate", "audio", ("text",), ("audio",)),
    ("text.embeddings", "embedding", ("text",), ("embedding",)),
    ("chat.completions", "language", ("text",), ("text",)),
    ("text.completions", "language", ("text",), ("text",)),
    ("text.rerank", "reranking", ("text",), ("text",)),
    ("speech.synthesis", "audio", ("text",), ("audio",)),
    ("speech.transcription", "audio", ("audio",), ("text",)),
    ("speech.translation", "audio", ("audio",), ("text",)),
)
FAMILIES = (
    ("chat.", "language", ("text",), ("text",)),
    ("responses.", "language", ("text",), ("text",)),
    ("decision.", "decision", ("text",), ("text",)),
    ("image.", "image", ("text",), ("image",)),
    ("video.", "video", ("text",), ("video",)),
    ("speech.", "audio", ("text",), ("audio",)),
    ("audio.", "audio", ("text",), ("audio",)),
)
OTHER = (
    ("image.other", "image", ("text",), ("image",)),
    ("video.other", "video", ("text",), ("video",)),
    ("speech.other", "audio", ("text",), ("audio",)),
    ("audio.other", "audio", ("text",), ("audio",)),
    ("responses.create", "language", ("text",), ("text",)),
    ("decision.systemone", "decision", ("text",), ("text",)),
    ("unknown.other", None, (), ()),
    ("", None, (), ()),
)


def _view(answer):
    return None if answer is None else (answer.type, answer.input_modalities, answer.output_modalities)


@pytest.mark.parametrize("intent, type_, inputs, outputs", EXACT + FAMILIES + OTHER)
def test_from_capability_matches_literal_baseline(intent, type_, inputs, outputs):
    expected = None if type_ is None else (type_, inputs, outputs)
    assert _view(mm.from_capability(intent)) == expected


def test_declared_modalities_still_refine_defaults_without_changing_type():
    assert _view(mm.from_capability("speech.transcription", ["video", "text"], ["audio"])) == (
        "audio", ("text", "video"), ("audio",),
    )
    assert _view(mm.from_capability("image.edit", ["unknown"], [])) == (
        "image", ("text", "image"), ("image",),
    )
    # dot-2289's declared-I/O typing: a capability that declares text in and text out is typed language, even for an unknown intent.
    assert _view(mm.from_capability("unknown.other", ["text"], ["text"])) == ("language", ("text",), ("text",))
    assert mm.from_capability(None) is None


def _document():
    return {
        "schema_version": 1,
        "exact": [{"intent": "test.exact", "operation": "chat"}],
        "families": [{"prefix": "test.", "operation": "speech"}],
    }


def _metadata(**overrides):
    return {"type": "image", "input_modalities": ["text"], "output_modalities": ["image"], **overrides}


def _set_metadata(rows, metadata):
    rows[0].pop("operation", None)
    rows[0]["model_metadata"] = metadata


def _write(tmp_path, document):
    path = tmp_path / "intent-rows.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _loader():
    # Missing API is a test failure on base, not a collection-time import error.
    loader = getattr(mm, "load_capability_intent_metadata", None)
    assert callable(loader), "capability intent metadata needs a data loader"
    return loader


def _activate(monkeypatch, index):
    monkeypatch.setattr(mm, "_CAPABILITY_INTENTS", index.exact)
    monkeypatch.setattr(mm, "_CAPABILITY_INTENT_FAMILIES", index.families)


def test_shipped_rows_pin_both_complete_tables():
    index = _loader()(ROWS)
    assert tuple((name, *_view(value)) for name, value in index.exact.items()) == EXACT
    assert tuple((name, *_view(value)) for name, value in index.families.items()) == FAMILIES
    assert mm._CAPABILITY_INTENTS == index.exact
    assert mm._CAPABILITY_INTENT_FAMILIES == index.families


def test_index_maps_and_metadata_are_read_only(tmp_path):
    index = _loader()(_write(tmp_path, _document()))
    with pytest.raises(TypeError):
        index.exact["other"] = index.exact["test.exact"]
    with pytest.raises(TypeError):
        index.families["other."] = index.families["test."]
    with pytest.raises(FrozenInstanceError):
        index.exact = {}
    with pytest.raises(FrozenInstanceError):
        index.families["test."].type = "image"
    assert isinstance(index.exact["test.exact"].input_modalities, tuple)
    assert isinstance(index.families["test."].output_modalities, tuple)


def test_test_only_family_first_match_and_exact_precedence_are_data(tmp_path, monkeypatch):
    document = {
        "schema_version": 1,
        "exact": [{"intent": "test.specific", "operation": "decisions"}],
        "families": [
            {"prefix": "test.", "operation": "chat"},
            {"prefix": "test.specific", "model_metadata": _metadata()},
        ],
    }
    _activate(monkeypatch, _loader()(_write(tmp_path, document)))
    assert _view(mm.from_capability("test.new")) == ("language", ("text",), ("text",))
    assert _view(mm.from_capability("test.specific.other")) == ("language", ("text",), ("text",))
    assert _view(mm.from_capability("test.specific")) == ("decision", ("text",), ("text",))
    document["families"].reverse()
    _activate(monkeypatch, _loader()(_write(tmp_path, document)))
    assert _view(mm.from_capability("test.specific.other")) == ("image", ("text",), ("image",))
    assert _view(mm.from_capability("test.specific")) == ("decision", ("text",), ("text",))


def test_operation_rows_are_reused_by_name(tmp_path, monkeypatch):
    document = _document()
    document["exact"][0]["operation"] = "rerank"
    index = _loader()(_write(tmp_path, document))
    _activate(monkeypatch, index)
    assert _view(mm.from_capability("test.exact")) == ("reranking", ("text",), ("text",))
    assert _view(mm.from_capability("test.other")) == ("audio", ("text",), ("audio",))
    assert index.exact["test.exact"] is mm.OPERATION_METADATA["rerank"]
    assert index.families["test."] is mm.OPERATION_METADATA["speech"]


def test_loaded_rows_are_a_snapshot_not_a_per_call_read(tmp_path, monkeypatch):
    path = _write(tmp_path, _document())
    index = _loader()(path)
    _activate(monkeypatch, index)
    path.write_text("", encoding="utf-8")
    assert _view(mm.from_capability("test.exact")) == ("language", ("text",), ("text",))
    assert _view(mm.from_capability("test.other")) == ("audio", ("text",), ("audio",))
    with pytest.raises(ValueError, match="document"):
        _loader()(path)


@pytest.mark.parametrize("section, key", (("exact", "intent"), ("families", "prefix")))
@pytest.mark.parametrize("change, row_number, message", (
    (lambda rows, key: rows[0].update(unexpected=True), 0, "unknown keys"),
    (lambda rows, key: rows.append(dict(rows[0])), 1, "duplicate"),
    (lambda rows, key: rows[0].pop(key), 0, "nonempty string"),
    (lambda rows, key: rows[0].update({key: []}), 0, "nonempty string"),
    (lambda rows, key: rows[0].update({key: ""}), 0, "nonempty string"),
    (lambda rows, key: rows[0].update(operation="unknown"), 0, "unknown operation"),
    (lambda rows, key: rows[0].update(operation=[]), 0, "unknown operation"),
    (lambda rows, key: _set_metadata(rows, _metadata(type="unknown")), 0, "unknown type"),
    (lambda rows, key: _set_metadata(rows, _metadata(input_modalities=["unknown"])), 0, "input_modalities"),
    (lambda rows, key: _set_metadata(rows, _metadata(output_modalities=["file"])), 0, "output_modalities"),
    (lambda rows, key: rows[0].update(model_metadata=_metadata()), 0, "exactly one"),
    (lambda rows, key: rows[0].pop("operation"), 0, "exactly one"),
    (lambda rows, key: _set_metadata(rows, _metadata(unexpected=True)), 0, "unknown keys"),
    (lambda rows, key: _set_metadata(rows, []), 0, "mapping"),
    (lambda rows, key: _set_metadata(rows, {"type": "image", "output_modalities": ["image"]}), 0, "input_modalities"),
    (lambda rows, key: _set_metadata(rows, _metadata(input_modalities="text")), 0, "input_modalities"),
    (lambda rows, key: rows.append("broken"), 1, "mapping"),
))
def test_invalid_intent_rows_name_the_file_and_row(tmp_path, section, key, change, row_number, message):
    document = _document()
    change(document[section], key)
    path = _write(tmp_path, document)
    with pytest.raises(ValueError) as caught:
        _loader()(path)
    error = str(caught.value)
    assert str(path) in error
    assert f"{section}[{row_number}]" in error
    assert message in error


@pytest.mark.parametrize("change, message", (
    (lambda document: document.update(schema_version=2), "schema_version"),
    (lambda document: document.update(schema_version=True), "schema_version"),
    (lambda document: document.pop("schema_version"), "schema_version"),
    (lambda document: document.update(unexpected=True), "unknown keys"),
    (lambda document: document.update(exact={}), "exact"),
    (lambda document: document.update(families=None), "families"),
))
def test_invalid_document_names_the_file(tmp_path, change, message):
    document = _document()
    change(document)
    path = _write(tmp_path, document)
    with pytest.raises(ValueError) as caught:
        _loader()(path)
    assert str(path) in str(caught.value)
    assert message in str(caught.value)


@pytest.mark.parametrize("text", ("", "[]", "schema_version: ["))
def test_malformed_intent_file_names_the_file(tmp_path, text):
    path = tmp_path / "intent-rows.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        _loader()(path)
    assert str(path) in str(caught.value)
    assert "document" in str(caught.value)


def test_missing_intent_file_names_the_file(tmp_path):
    path = tmp_path / "missing.yml"
    with pytest.raises(ValueError, match="cannot read") as caught:
        _loader()(path)
    assert str(path) in str(caught.value)
