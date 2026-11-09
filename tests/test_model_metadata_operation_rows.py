"""Operation metadata belongs to shipped registered-route rows, not Python branches."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from gateway import model_metadata as mm
from gateway import stable_aliases

ROWS = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"

# Literal pre-migration table: neither the loader nor the shipped rows is this oracle.
EXPECTED = {
    "chat": ("language", ("text",), ("text",)),
    "embeddings": ("embedding", ("text",), ("embedding",)),
    "images": ("image", ("text",), ("image",)),
    "image_edits": ("image", ("text", "image"), ("image",)),
    "videos": ("video", ("text",), ("video",)),
    "speech": ("audio", ("text",), ("audio",)),
    "transcriptions": ("audio", ("audio",), ("text",)),
    "audio_generate": ("audio", ("text",), ("audio",)),
    "rerank": ("reranking", ("text",), ("text",)),
    "decisions": ("decision", ("text",), ("text",)),
}


def _document():
    return {"records": [{"id": "fixture", "registered_routes": [
        {"canonical": f"/fixture/{operation}", "operation": operation,
         "model_metadata": {"type": type_, "input_modalities": list(inputs),
                            "output_modalities": list(outputs)}}
        for operation, (type_, inputs, outputs) in EXPECTED.items()
    ]}]}


def _write(tmp_path, document):
    path = tmp_path / "rows.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _view(index):
    return {operation: (md.type, md.input_modalities, md.output_modalities)
            for operation, md in index.items()}


def test_shipped_operation_index_is_the_literal_ten_entry_table():
    assert _view(mm.load_operation_metadata(ROWS)) == EXPECTED
    assert _view(mm.OPERATION_METADATA) == EXPECTED
    assert tuple(mm.OPERATION_METADATA) == stable_aliases.OPERATIONS == tuple(EXPECTED)
    for operation, expected in EXPECTED.items():
        md = mm.from_operation(operation)
        assert (md.type, md.input_modalities, md.output_modalities) == expected
    assert mm.from_operation("unknown-operation") is None


def test_operation_index_and_its_values_are_read_only():
    with pytest.raises(TypeError):
        mm.OPERATION_METADATA["chat"] = mm.from_operation("images")
    with pytest.raises(FrozenInstanceError):
        mm.OPERATION_METADATA["chat"].type = "image"


def test_rows_choose_metadata_and_roster_chooses_order(tmp_path, monkeypatch):
    document = _document()
    rows = document["records"][0]["registered_routes"]
    rows[0]["model_metadata"] = {
        "type": "video", "input_modalities": ["image"], "output_modalities": ["video"],
    }
    rows.reverse()
    index = mm.load_operation_metadata(_write(tmp_path, document))
    assert tuple(index) == tuple(EXPECTED)
    assert _view(index)["chat"] == ("video", ("image",), ("video",))
    monkeypatch.setattr(mm, "OPERATION_METADATA", index)
    assert mm.from_operation("chat") is index["chat"]
    assert mm.operations_of("video", ["image"]) == ("chat",)


PAIRS = tuple(dict.fromkeys((type_, inputs) for type_, inputs, _ in EXPECTED.values()))


@pytest.mark.parametrize(("type_", "inputs"), PAIRS)
def test_operations_of_preserves_type_input_and_among_parity(type_, inputs):
    for among in (None, (), stable_aliases.ENGINE_OPERATIONS,
                  tuple(reversed(EXPECTED)), ("unknown-operation", "chat", "speech", "speech")):
        candidates = tuple(EXPECTED) if among is None else among
        typed = tuple(op for op in candidates if op in EXPECTED and EXPECTED[op][0] == type_)
        for requested in (list(inputs), tuple(inputs), None, [], ["unknown-modality"]):
            narrowed = (tuple(op for op in typed if set(EXPECTED[op][1]) == set(requested))
                        if isinstance(requested, (list, tuple)) and requested else ())
            assert mm.operations_of(type_, requested, among=among) == (narrowed or typed)
    assert mm.operations_of(None, list(inputs)) == ()
    assert mm.operations_of("unknown-type", list(inputs)) == ()


@pytest.mark.parametrize(("case", "message"), (
    ("duplicate", "duplicate operation"),
    ("unknown_type", "unknown type"),
    ("unknown_input", "unknown input_modalities"),
    ("unknown_output", "unknown output_modalities"),
    ("missing_operation", "missing operations"),
    ("unknown_operation", "unknown operation"),
    ("missing_metadata", "operation and model_metadata"),
    ("missing_key", "operation and model_metadata"),
    ("non_list_input", "input_modalities must be a list"),
    ("non_list_output", "output_modalities must be a list"),
))
def test_invalid_operation_rows_fail_when_loaded(tmp_path, case, message):
    document = _document()
    rows = document["records"][0]["registered_routes"]
    first = rows[0]
    if case == "duplicate":
        rows.append(dict(first))
    elif case == "unknown_type":
        first["model_metadata"]["type"] = "not-a-type"
    elif case == "unknown_input":
        first["model_metadata"]["input_modalities"] = ["embedding"]
    elif case == "unknown_output":
        first["model_metadata"]["output_modalities"] = ["file"]
    elif case == "missing_operation":
        rows.pop()
    elif case == "unknown_operation":
        first["operation"] = "not-an-operation"
    elif case == "missing_metadata":
        del first["model_metadata"]
    elif case == "missing_key":
        del first["operation"]
    elif case == "non_list_input":
        first["model_metadata"]["input_modalities"] = "text"
    elif case == "non_list_output":
        first["model_metadata"]["output_modalities"] = "text"
    with pytest.raises(ValueError, match=message):
        mm.load_operation_metadata(_write(tmp_path, document))

