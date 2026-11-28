"""Service capability metadata is ordered shipped data, not a Python table."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from gateway import model_metadata as mm

ROWS = Path(__file__).resolve().parents[1] / "registry" / "service_capability_metadata.yml"

# Literal old table, independent of the production loader and shipped file.
EXPECTED = (
    ("chat", "language", ("text",), ("text",)),
    ("completions", "language", ("text",), ("text",)),
    ("embeddings", "embedding", ("text",), ("embedding",)),
    ("rerank", "reranking", ("text",), ("text",)),
    ("decisions", "decision", ("text",), ("text",)),
)


def _document():
    return {
        "schema_version": 1,
        "capabilities": [
            {"capability": "chat", "operation": "chat"},
            {"capability": "completions", "operation": "chat"},
            {"capability": "embeddings", "operation": "embeddings"},
            {"capability": "rerank", "operation": "rerank"},
            {"capability": "decisions", "operation": "decisions"},
        ],
        "augmentation": {
            "capability": "vision", "match_type": "language", "input_modalities": ["image"],
        },
    }


def _write(tmp_path, document):
    path = tmp_path / "service-rows.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _view(index):
    return tuple((name, md.type, md.input_modalities, md.output_modalities)
                 for name, md in index.rows.items())


def _loader():
    # The base fails this assertion rather than failing test collection.
    loader = getattr(mm, "load_service_capability_metadata", None)
    assert callable(loader), "service capability metadata needs a data loader"
    return loader


def test_shipped_service_rows_are_the_literal_old_table():
    index = _loader()(ROWS)
    assert _view(index) == EXPECTED
    assert _view(mm.SERVICE_CAPABILITY_METADATA) == EXPECTED
    assert (index.augmentation.capability, index.augmentation.match_type,
            index.augmentation.input_modalities) == ("vision", "language", ("image",))
    for name, md in index.rows.items():
        assert mm.from_service_capabilities([name]) == md


def test_service_index_and_nested_values_are_read_only(tmp_path):
    index = _loader()(_write(tmp_path, _document()))
    with pytest.raises(TypeError):
        index.rows["chat"] = index.rows["embeddings"]
    with pytest.raises(FrozenInstanceError):
        index.rows = {}
    with pytest.raises(FrozenInstanceError):
        index.rows["chat"].type = "image"
    with pytest.raises(FrozenInstanceError):
        index.augmentation.capability = "other"
    assert isinstance(index.augmentation.input_modalities, tuple)


def test_file_order_and_operation_reference_choose_the_answer(tmp_path, monkeypatch):
    document = _document()
    rows = document["capabilities"]
    rows.insert(0, rows.pop(2))
    rows[0]["operation"] = "rerank"
    index = _loader()(_write(tmp_path, document))
    monkeypatch.setattr(mm, "SERVICE_CAPABILITY_METADATA", index)
    # Reordering alone would return embedding; changing the operation returns reranking.
    answer = mm.from_service_capabilities([" CHAT ", "EMBEDDINGS", "vision", None])
    assert (answer.type, answer.input_modalities, answer.output_modalities) == (
        "reranking", ("text",), ("text",),
    )
    assert mm.from_service_capabilities(["vision"]) is None
    assert mm.from_service_capabilities(["unknown", 4]) is None


def test_augmentation_is_data_and_only_changes_a_matching_type(tmp_path, monkeypatch):
    document = _document()
    document["augmentation"] = {
        "capability": "pictures", "match_type": "embedding", "input_modalities": ["image"],
    }
    index = _loader()(_write(tmp_path, document))
    monkeypatch.setattr(mm, "SERVICE_CAPABILITY_METADATA", index)
    assert mm.from_service_capabilities(["pictures"]) is None
    assert mm.from_service_capabilities(["chat", "pictures"]).input_modalities == ("text",)
    assert mm.from_service_capabilities(["embeddings", "vision"]).input_modalities == ("text",)
    answer = mm.from_service_capabilities(["EMBEDDINGS", " pictures "])
    assert (answer.type, answer.input_modalities, answer.output_modalities, answer.tags) == (
        "embedding", ("text", "image"), ("embedding",), (),
    )


def test_loaded_index_is_a_snapshot_not_a_per_call_file_read(tmp_path, monkeypatch):
    path = _write(tmp_path, _document())
    index = _loader()(path)
    monkeypatch.setattr(mm, "SERVICE_CAPABILITY_METADATA", index)
    path.write_text("", encoding="utf-8")
    assert mm.from_service_capabilities(["chat", "vision"]).input_modalities == ("text", "image")
    assert mm.from_service_capabilities(["embeddings", "vision"]).input_modalities == ("text",)
    with pytest.raises(ValueError, match="mapping"):
        _loader()(path)


@pytest.mark.parametrize(("change", "message"), (
    (lambda d: d["capabilities"][0].update(operation="unknown"), r"capabilities\[0\].operation"),
    (lambda d: d["capabilities"].append(dict(d["capabilities"][0])), r"capabilities\[5\].*duplicate"),
    (lambda d: d["capabilities"].append({"capability": " CHAT ", "operation": "chat"}), r"capabilities\[5\].*duplicate"),
    (lambda d: d["capabilities"][0].update(capability="CHAT"), r"capabilities\[0\].*lowercase"),
    (lambda d: d["capabilities"][0].update(capability=" chat "), r"capabilities\[0\].*lowercase"),
    (lambda d: d["capabilities"][0].update(capability=7), r"capabilities\[0\].*string"),
    (lambda d: d["capabilities"][0].update(capability=""), r"capabilities\[0\].*nonempty"),
    (lambda d: d["capabilities"][0].pop("capability"), r"capabilities\[0\].capability"),
    (lambda d: d["capabilities"][0].pop("operation"), r"capabilities\[0\].operation"),
    (lambda d: d["capabilities"][0].update(operation=[]), r"capabilities\[0\].operation"),
    (lambda d: d.update(unknown=True), "unknown keys"),
    (lambda d: d["capabilities"][0].update(unknown=True), r"capabilities\[0\].*unknown keys"),
    (lambda d: d["augmentation"].update(capability="chat"), "augmentation.*matching row"),
    (lambda d: d["augmentation"].update(unknown=True), "augmentation.*unknown keys"),
    (lambda d: d["augmentation"].update(capability=" VISION "), "augmentation.*lowercase"),
    (lambda d: d["augmentation"].update(capability=4), "augmentation.*string"),
    (lambda d: d["augmentation"].update(match_type="unknown"), "augmentation.match_type"),
    (lambda d: d["augmentation"].update(input_modalities=["unknown"]), "augmentation.input_modalities"),
    (lambda d: d["augmentation"].update(input_modalities="image"), "augmentation.input_modalities"),
    (lambda d: d.update(capabilities=[]), "capabilities.*nonempty"),
    (lambda d: d.update(capabilities={}), "capabilities.*list"),
    (lambda d: d["capabilities"].append("broken"), r"capabilities\[5\].*mapping"),
    (lambda d: d.update(schema_version=True), "schema_version"),
))
def test_invalid_service_rows_fail_at_load_with_the_row_named(tmp_path, change, message):
    document = _document()
    change(document)
    path = _write(tmp_path, document)
    with pytest.raises(ValueError, match=message) as caught:
        _loader()(path)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("text", ("", "[]", "schema_version: ["))
def test_empty_or_malformed_file_fails_loudly(tmp_path, text):
    path = tmp_path / "service-rows.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        _loader()(path)
    assert str(path) in str(caught.value)


def test_missing_file_fails_loudly(tmp_path):
    path = tmp_path / "missing.yml"
    with pytest.raises(ValueError, match="cannot read") as caught:
        _loader()(path)
    assert str(path) in str(caught.value)
