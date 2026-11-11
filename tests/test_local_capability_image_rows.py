"""Image/default rows: literal compatibility pins and data-only extensions."""

from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from gateway import local_capabilities as local


ROWS = Path(__file__).resolve().parents[1] / "registry" / "local_capability_rows.yml"
IMAGE_DEFAULTS = {
    "section": "images", "enabled_flag": "images", "section_required": False,
    "backend_presence": "nonempty_mapping", "backend_capability": "images",
    "output_modalities": ("image",), "execution": "job", "streaming": False,
    "resource_tier": "batch", "gpu_required": True,
}
IMAGE_ROWS = (
    {"intent": "image.generate", "input_modalities": ("text",),
     "model_sources": ("backend.default_model", "config.model_compatibility.defaults.images")},
    {"intent": "image.edit", "input_modalities": ("text", "image"),
     "model_sources": ("config.model_compatibility.defaults.image_edits",)},
)
WORKFLOW_DEFAULTS = {
    "section_required": True, "backend_presence": "declared",
    "backend_capabilities": {"videos": "videos", "audio": "audio.generate"}, "execution": "job",
    "streaming": False, "gpu_required": True,
}


def document():
    # Lists are the serialized format; the expected loaded values above are immutable.
    return {
        "schema_version": 1, "alias_operations": [],
        "workflow_families": yaml.safe_load(ROWS.read_text(encoding="utf-8"))["workflow_families"],
        "image_defaults": {**IMAGE_DEFAULTS, "output_modalities": ["image"]},
        "image_rows": [
            {**row, "input_modalities": list(row["input_modalities"]),
             "model_sources": list(row["model_sources"])}
            for row in IMAGE_ROWS
        ],
        "workflow_defaults": dict(WORKFLOW_DEFAULTS),
    }


def write_rows(tmp_path, value):
    path = tmp_path / "test-image-rows.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def install_rows(monkeypatch, path):
    defaults, rows, workflows = local.load_local_capability_image_rows(path)
    monkeypatch.setattr(local, "_IMAGE_DEFAULTS", defaults)
    monkeypatch.setattr(local, "_IMAGE_ROWS", rows)
    monkeypatch.setattr(local, "_WORKFLOW_DEFAULTS", workflows)
    return defaults, rows, workflows


def models(config, **options):
    return {
        cap.intent: cap.model for cap in local.build_local_capabilities(config, **options)
        if cap.intent.startswith("image.")
    }


@pytest.mark.parametrize("backend_default, generate", [
    ("backend-model", "backend-model"),
    ("", "compat-model"),
    (None, "compat-model"),
    (0, "compat-model"),
    (7, None),
    (["not-a-model"], None),
])
def test_first_truthy_generate_then_string_check_and_independent_edit(backend_default, generate):
    config = {
        "backends": {"comfyui": {"capabilities": ["images"], "default_model": backend_default}},
        "model_compatibility": {"defaults": {
            "images": "compat-model", "image_edits": "edit-model",
        }},
    }
    expected = {"image.edit": "edit-model"}
    if generate is not None:
        expected["image.generate"] = generate
    assert models(config) == expected


@pytest.mark.parametrize("edit_default", [None, "", 7, ["not-a-model"]])
def test_edit_does_not_fall_back_to_generate_defaults(edit_default):
    assert models({
        "backends": {"comfyui": {"capabilities": ["images"], "default_model": "backend-model"}},
        "model_compatibility": {"defaults": {
            "images": "compat-model", "image_edits": edit_default,
        }},
    }) == {"image.generate": "backend-model"}


def test_explicit_backend_omitting_default_uses_compatibility_not_other_backend():
    config = {
        "images": {"backend": "renderer"},
        "backends": {
            "comfyui": {"capabilities": ["images"], "default_model": "wrong"},
            "renderer": {"workflows_dir": "/not-probed"},
        },
        "model_compatibility": {"defaults": {
            "images": "compat-model", "image_edits": "edit-model",
        }},
    }
    caps = local.build_local_capabilities(config)
    assert [(cap.intent, cap.backend, cap.model, cap.caveats) for cap in caps] == [
        ("image.generate", "renderer", "compat-model", ()),
        ("image.edit", "renderer", "edit-model", ()),
    ]
    # A declared but empty image backend remains ineligible.
    config["backends"]["renderer"] = {}
    assert local.build_local_capabilities(config) == []


def test_shipped_image_and_workflow_defaults_are_literal():
    defaults, rows, workflows = local.load_local_capability_image_rows(ROWS)
    assert defaults == IMAGE_DEFAULTS
    assert rows == IMAGE_ROWS
    assert workflows == WORKFLOW_DEFAULTS
    assert local._IMAGE_DEFAULTS == IMAGE_DEFAULTS
    assert local._IMAGE_ROWS == IMAGE_ROWS
    assert local._WORKFLOW_DEFAULTS == WORKFLOW_DEFAULTS


def test_new_image_intent_and_ordered_model_sources_need_only_rows(tmp_path, monkeypatch):
    data = document()
    data["image_rows"].append({
        "intent": "image.upscale", "input_modalities": ["image"],
        "model_sources": ["config.custom.upscale", "backend.default_model"],
    })
    install_rows(monkeypatch, write_rows(tmp_path, data))
    caps = local.build_local_capabilities({
        "backends": {"comfyui": {"capabilities": ["images"], "default_model": "backend-model"}},
        "custom": {"upscale": "upscale-model"},
    })
    assert [(cap.intent, cap.model) for cap in caps] == [
        ("image.generate", "backend-model"), ("image.upscale", "upscale-model"),
    ]
    added = caps[-1]
    assert (
        added.backend, added.input_modalities, added.output_modalities,
        added.execution, added.streaming, added.resource_tier, added.resources.gpu_required,
    ) == ("comfyui", ("image",), ("image",), "job", False, "batch", True)
    assert models({
        "backends": {"comfyui": {"capabilities": ["images"], "default_model": "backend-model"}},
        "custom": {"upscale": False},
    })["image.upscale"] == "backend-model"


def test_image_defaults_drive_section_flag_backend_and_resources(tmp_path, monkeypatch):
    data = document()
    data["image_defaults"].update({
        "section": "pictures", "enabled_flag": "pictures_enabled",
        "section_required": True, "backend_capability": "custom.render",
        "backend_presence": "declared", "output_modalities": ["audio"],
        "execution": "sync", "streaming": True, "resource_tier": "expensive",
        "gpu_required": False,
    })
    install_rows(monkeypatch, write_rows(tmp_path, data))
    config = {
        "backends": {"renderer": {"capabilities": ["custom.render"]}},
        "model_compatibility": {"defaults": {"images": "compat-model"}},
    }
    assert local.build_local_capabilities(config) == []
    config["pictures"] = {"enabled": True}
    cap, = local.build_local_capabilities(config)
    assert (
        cap.backend, cap.output_modalities, cap.execution, cap.streaming,
        cap.resource_tier, cap.resources.gpu_required,
    ) == ("renderer", ("audio",), "sync", True, "expensive", False)
    assert local.build_local_capabilities(config, enabled_flags={"pictures_enabled": False}) == []


def test_workflow_defaults_drive_presence_backend_and_resources(tmp_path, monkeypatch):
    data = document()
    data["workflow_defaults"].update({
        "backend_capabilities": {"videos": "custom.render", "audio": "custom.audio"},
        "backend_presence": "nonempty_mapping",
        "execution": "sync", "streaming": True, "gpu_required": False,
    })
    install_rows(monkeypatch, write_rows(tmp_path, data))
    config = {"backends": {"renderer": {}}, "videos": {"workflows": {"clip": {}}}}
    assert local.build_local_capabilities(config) == []
    config["backends"]["renderer"] = {"name": "Local", "capabilities": ["custom.render"]}
    cap, = local.build_local_capabilities(config)
    assert (
        cap.intent, cap.backend, cap.workflow, cap.execution, cap.streaming,
        cap.resource_tier, cap.resources.gpu_required,
    ) == ("video.generate", "renderer", "clip", "sync", True, "expensive", False)


@pytest.mark.parametrize("table, field, value, message", [
    ("image_defaults", "extra", True, "unknown fields"),
    ("image_defaults", "execution", "async", "execution"),
    ("image_defaults", "resource_tier", "unlimited", "resource_tier"),
    ("image_defaults", "section_required", 1, "section_required"),
    ("image_defaults", "streaming", "false", "streaming"),
    ("image_defaults", "gpu_required", None, "gpu_required"),
    ("image_defaults", "backend_presence", "truthy", "backend_presence"),
    ("image_defaults", "backend_capability", "", "backend_capability"),
    ("image_defaults", "output_modalities", [], "output_modalities"),
    ("image_rows", "extra", True, "unknown fields"),
    ("image_rows", "intent", "", "intent"),
    ("image_rows", "input_modalities", ["text", 7], "input_modalities"),
    ("image_rows", "model_sources", ["other.default"], "root"),
    ("image_rows", "model_sources", ["config..model"], "dotted"),
    ("image_rows", "model_sources", [], "model_sources"),
    ("image_rows", "model_sources", ["backend"], "dotted"),
    ("workflow_defaults", "extra", True, "unknown fields"),
    ("workflow_defaults", "execution", "async", "execution"),
    ("workflow_defaults", "section_required", "yes", "section_required"),
    ("workflow_defaults", "backend_presence", "truthy", "backend_presence"),
    ("workflow_defaults", "gpu_required", 1, "gpu_required"),
])
def test_bad_image_rows_name_file_and_logical_row(tmp_path, table, field, value, message):
    data = document()
    target = data[table][0] if table == "image_rows" else data[table]
    target[field] = value
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        local.load_local_capability_image_rows(path)
    assert str(path) in str(error.value)
    assert table + " row 1" in str(error.value)
    assert message in str(error.value)


@pytest.mark.parametrize("table, field", [
    ("image_defaults", "section"), ("image_defaults", "execution"),
    ("image_rows", "intent"), ("image_rows", "model_sources"),
    ("workflow_defaults", "backend_capabilities"), ("workflow_defaults", "execution"),
])
def test_missing_image_row_field_names_file_and_row(tmp_path, table, field):
    data = document()
    target = data[table][0] if table == "image_rows" else data[table]
    del target[field]
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        local.load_local_capability_image_rows(path)
    assert str(path) in str(error.value)
    assert table + " row 1" in str(error.value)
    assert "missing fields" in str(error.value)


@pytest.mark.parametrize("table", ["image_defaults", "image_rows", "workflow_defaults"])
def test_missing_table_names_file_and_document_row(tmp_path, table):
    data = document()
    del data[table]
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as error:
        local.load_local_capability_image_rows(path)
    assert str(path) in str(error.value)
    assert "document row 0" in str(error.value)
    assert "missing fields" in str(error.value)


def test_loaded_media_rows_are_deeply_read_only(tmp_path):
    defaults, rows, workflows = local.load_local_capability_image_rows(write_rows(tmp_path, document()))
    assert isinstance(defaults, MappingProxyType)
    assert isinstance(workflows, MappingProxyType)
    assert isinstance(rows, tuple)
    assert isinstance(rows[0], MappingProxyType)
    with pytest.raises(TypeError):
        defaults["section"] = "changed"
    with pytest.raises(TypeError):
        defaults["output_modalities"][0] = "changed"
    with pytest.raises(TypeError):
        rows[0]["intent"] = "changed"
    with pytest.raises(TypeError):
        rows[0]["model_sources"][0] = "changed"
    with pytest.raises(TypeError):
        rows[0]["input_modalities"][0] = "changed"
    with pytest.raises(TypeError):
        workflows["backend_capabilities"] = {}
    assert isinstance(workflows["backend_capabilities"], MappingProxyType)
    with pytest.raises(TypeError):
        workflows["backend_capabilities"]["videos"] = "changed"


def test_image_and_workflow_builders_do_not_reload_rows(monkeypatch):
    def unexpected_read(*args, **kwargs):
        raise AssertionError("builder read rows again")
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    caps = local.build_local_capabilities({
        "backends": {"comfyui": {"capabilities": ["images", "videos"], "default_model": "backend-model"}},
        "videos": {"workflows": {"clip": {}}},
    })
    assert [cap.intent for cap in caps] == ["image.generate", "video.generate"]
