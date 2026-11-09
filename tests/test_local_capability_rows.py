"""Literal pins and test-only data extension for local capability rows."""

from copy import deepcopy
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from gateway import local_capabilities as local


ROWS = Path(__file__).resolve().parents[1] / "registry" / "local_capability_rows.yml"
EXPECTED_ALIASES = {
    "chat": ("chat.completions", ("text",), ("text",), "sync"),
    "embeddings": ("text.embeddings", ("text",), ("embedding",), "sync"),
    "images": ("image.generate", ("text",), ("image",), "job"),
    "image_edits": ("image.edit", ("text", "image"), ("image",), "job"),
    "videos": ("video.generate", ("text",), ("video",), "job"),
    "speech": ("speech.synthesis", ("text",), ("audio",), "sync"),
    "transcriptions": ("speech.transcription", ("audio",), ("text",), "sync"),
    "audio_generate": ("audio.generate", ("text",), ("audio",), "job"),
}
EXPECTED_FAMILIES = (
    ("videos", "videos", "video.generate", ("text",), ("video",), "expensive"),
    ("audio", "audio", "audio.generate", ("text",), ("audio",), "batch"),
)


def document():
    return {
        **yaml.safe_load(ROWS.read_text(encoding="utf-8")),
        "schema_version": 1,
        "alias_operations": [{
            "name": "chat", "intent": "chat.completions",
            "input_modalities": ["text"], "output_modalities": ["text"],
            "execution": "sync",
        }],
        "workflow_families": [{
            "section": "videos", "enabled_flag": "videos", "intent": "video.generate",
            "input_modalities": ["text"], "output_modalities": ["video"],
            "resource_tier": "expensive",
        }],
    }


def write_rows(tmp_path, value):
    path = tmp_path / "custom-local-capabilities.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_shipped_alias_rows_keep_literal_contents_and_order():
    aliases, _ = local.load_local_capability_rows(ROWS)
    assert list(aliases.items()) == list(EXPECTED_ALIASES.items())
    assert list(local._ALIAS_INTENTS.items()) == list(EXPECTED_ALIASES.items())


def test_shipped_workflow_rows_keep_literal_contents_and_order():
    _, families = local.load_local_capability_rows(ROWS)
    assert families == EXPECTED_FAMILIES
    assert local._WORKFLOW_FAMILIES == EXPECTED_FAMILIES


def test_new_alias_operation_advertises_from_test_only_rows(tmp_path, monkeypatch):
    value = document()
    value["alias_operations"].append({
        "name": "sketches", "intent": "image.generate",
        "input_modalities": ["text", "image"], "output_modalities": ["image"],
        "execution": "job",
    })
    aliases, _ = local.load_local_capability_rows(write_rows(tmp_path, value))
    monkeypatch.setattr(local, "_ALIAS_INTENTS", aliases)
    caps = local.build_alias_capabilities({
        "sketches": {"stable": [{"alias": "sketch-default", "resolves_to": "canvas"}]},
    })
    assert len(caps) == 1
    cap = caps[0]
    assert (
        cap.intent, cap.input_modalities, cap.output_modalities, cap.execution,
        cap.backend, cap.model, cap.tags,
    ) == (
        "image.generate", ("text", "image"), ("image",), "job",
        "gateway", "sketch-default", ("alias", "resolves_to:canvas"),
    )


def test_new_workflow_family_uses_its_rows_flag_and_tier(tmp_path, monkeypatch):
    value = document()
    value["workflow_families"].append({
        "section": "scenes", "enabled_flag": "scene_enabled", "intent": "video.generate",
        "input_modalities": ["text", "image"], "output_modalities": ["video"],
        "resource_tier": "batch",
    })
    _, families = local.load_local_capability_rows(write_rows(tmp_path, value))
    monkeypatch.setattr(local, "_WORKFLOW_FAMILIES", families)
    config = {
        "backends": {"renderer": {}},
        "scenes": {"backend": "renderer", "workflows": {"scene-one": {"file": "scene.json"}}},
    }
    caps = local.build_local_capabilities(config)
    assert len(caps) == 1
    cap = caps[0]
    assert (
        cap.workflow, cap.backend, cap.intent, cap.input_modalities,
        cap.output_modalities, cap.execution, cap.resource_tier,
    ) == (
        "scene-one", "renderer", "video.generate", ("text", "image"),
        ("video",), "job", "batch",
    )
    assert local.build_local_capabilities(config, enabled_flags={"scene_enabled": False}) == []


@pytest.mark.parametrize("table, change, field, value, message", [
    ("alias_operations", "set", "extra", True, "unknown fields"),
    ("alias_operations", "delete", "intent", None, "missing fields"),
    ("alias_operations", "set", "input_modalities", [], "input_modalities"),
    ("alias_operations", "set", "output_modalities", [], "output_modalities"),
    ("alias_operations", "set", "execution", "batch", "execution"),
    ("alias_operations", "duplicate", None, None, "duplicate name"),
    ("alias_operations", "row", None, [], "must be a mapping"),
    ("alias_operations", "set", "name", "", "name"),
    ("alias_operations", "set", "intent", 7, "intent"),
    ("alias_operations", "set", "input_modalities", "text", "input_modalities"),
    ("alias_operations", "set", "input_modalities", ["text", 7], "input_modalities"),
    ("alias_operations", "set", "execution", ["sync"], "execution"),
    ("workflow_families", "set", "extra", True, "unknown fields"),
    ("workflow_families", "delete", "enabled_flag", None, "missing fields"),
    ("workflow_families", "set", "input_modalities", [], "input_modalities"),
    ("workflow_families", "set", "output_modalities", [], "output_modalities"),
    ("workflow_families", "set", "resource_tier", "unlimited", "resource_tier"),
    ("workflow_families", "duplicate", None, None, "duplicate name"),
    ("workflow_families", "row", None, "videos", "must be a mapping"),
    ("workflow_families", "set", "section", "", "section"),
    ("workflow_families", "set", "enabled_flag", False, "enabled_flag"),
    ("workflow_families", "set", "resource_tier", ["batch"], "resource_tier"),
    ("workflow_families", "set", "output_modalities", "video", "output_modalities"),
    ("workflow_families", "set", "input_modalities", [" "], "input_modalities"),
])
def test_bad_rows_name_file_table_and_row(tmp_path, table, change, field, value, message):
    data = document()
    row_number = 1
    if change == "duplicate":
        data[table].append(deepcopy(data[table][0]))
        row_number = 2
    elif change == "row":
        data[table][0] = value
    elif change == "delete":
        del data[table][0][field]
    else:
        data[table][0][field] = value
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        local.load_local_capability_rows(path)
    text = str(error.value)
    assert str(path) in text
    assert f"{table} row {row_number}" in text
    assert message in text


@pytest.mark.parametrize("change, field, value, message", [
    ("set", "extra", True, "unknown fields"),
    ("delete", "schema_version", None, "missing fields"),
    ("delete", "alias_operations", None, "missing fields"),
    ("delete", "workflow_families", None, "missing fields"),
    ("set", "schema_version", True, "schema_version"),
    ("set", "schema_version", 2, "schema_version"),
    ("set", "alias_operations", {}, "must be a list"),
    ("set", "workflow_families", {}, "must be a list"),
    ("document", None, [], "must be a mapping"),
])
def test_bad_documents_name_file_and_document_row(tmp_path, change, field, value, message):
    data = document()
    if change == "document":
        data = value
    elif change == "delete":
        del data[field]
    else:
        data[field] = value
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        local.load_local_capability_rows(path)
    text = str(error.value)
    assert str(path) in text
    assert "document row 0" in text
    assert message in text


def test_loaded_tables_are_deeply_read_only(tmp_path):
    aliases, families = local.load_local_capability_rows(write_rows(tmp_path, document()))
    with pytest.raises(TypeError):
        aliases["extra"] = aliases["chat"]
    with pytest.raises(TypeError):
        aliases["chat"][1][0] = "audio"
    with pytest.raises(TypeError):
        families[0] = families[0]
    with pytest.raises(TypeError):
        families[0][3][0] = "audio"


def test_imported_tables_are_read_only():
    assert isinstance(local._ALIAS_INTENTS, MappingProxyType)
    assert isinstance(local._WORKFLOW_FAMILIES, tuple)
    assert all(isinstance(row, tuple) for row in local._WORKFLOW_FAMILIES)


def test_builders_do_not_reload_rows(monkeypatch):
    def unexpected_read(*args, **kwargs):
        raise AssertionError("a capability builder re-read the rows file")
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert len(local.build_alias_capabilities({
        "chat": {"stable": [{"alias": "chat-default", "resolves_to": "model"}]},
    })) == 1
    assert len(local.build_local_capabilities({
        "backends": {"renderer": {}},
        "videos": {"backend": "renderer", "workflows": {"one": {"file": "one.json"}}},
    })) == 1


def test_loader_keeps_file_order_instead_of_sorting(tmp_path):
    value = document()
    value["alias_operations"].append({**value["alias_operations"][0], "name": "aaa"})
    value["workflow_families"].append({
        **value["workflow_families"][0], "section": "aaa", "enabled_flag": "aaa",
    })
    aliases, families = local.load_local_capability_rows(write_rows(tmp_path, value))
    assert list(aliases) == ["chat", "aaa"]
    assert [row[0] for row in families] == ["videos", "aaa"]
