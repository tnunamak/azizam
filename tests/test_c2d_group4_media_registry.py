"""Version-one media metadata contract; golden captured before implementation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
import yaml

from gateway import openai_media_schemas as schemas


REGISTRY_PATH = Path(schemas.__file__).resolve().parent.parent / "registry/openai_media_schemas.yml"
MAPS = {"identity_scene": {"reference_photos": "image"}, "segment_chain": {"source_clip": "video"}}


def _document():
    return {"schema_version": 1, "endpoints": {"GET /v1/probe": {
        "responses": {"200": {"description": "Probe.", "content": {
            "application/json": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
        }}},
    }}, "wire_error_params": deepcopy(MAPS)}


def _write(tmp_path, document):
    path = tmp_path / "media.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _fresh_module(monkeypatch):
    name = "_c2d_media_registry_probe"
    spec = importlib.util.spec_from_file_location(name, schemas.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_media_openapi_exports_do_not_publish_error_maps():
    actual = {"endpoints": schemas._SCHEMA_FRAGMENTS, "exports": {
        name: value for name, value in vars(schemas).items() if name.endswith("_OPENAPI")
    }}
    assert "wire_error_params" not in json.dumps(actual)


def test_media_registry_has_one_runtime_read(monkeypatch, tmp_path):
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == REGISTRY_PATH:
            reads.append(path)
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", read):
        module = _fresh_module(monkeypatch)
        assert reads == [REGISTRY_PATH]
        for _ in range(2):
            assert module.wire_error_param("identity_scene", "reference_photos") == "image"
            module._endpoint_fragment("POST", "/v1/images/edits")
        assert reads == [REGISTRY_PATH]
    path = _write(tmp_path, _document())
    registry = module.load_media_schema_registry(path)
    with patch.object(module, "load_media_schema_registry", return_value=registry) as loader:
        fragments = module._load_schema_fragments(path)
    loader.assert_called_once_with(path)
    assert type(fragments) is dict
    fragments["GET /v1/probe"]["responses"].clear()
    assert registry.endpoints["GET /v1/probe"]["responses"]


def test_wire_error_param_lookup_is_exact_and_closed(tmp_path):
    assert schemas.wire_error_param("identity_scene", "reference_photos") == "image"
    assert schemas.wire_error_param("segment_chain", "source_clip") == "video"
    for domain, key in (("unknown", "source_clip"), ("segment_chain", "unknown"),
                        ("Segment_chain", "source_clip"), ("segment_chain", "Source_clip"),
                        ("segment_chain.extra", "source_clip"), ("segment_chain", "source")):
        with pytest.raises(KeyError) as failure:
            schemas.wire_error_param(domain, key)
        assert domain in str(failure.value) and key in str(failure.value)
    document = _document()
    document["wire_error_params"] = {"unfamiliar-domain": {"payload": "Envelope.parts[0]-id"}}
    registry = schemas.load_media_schema_registry(_write(tmp_path, document))
    assert schemas.wire_error_param("unfamiliar-domain", "payload", registry=registry) == "Envelope.parts[0]-id"
    with pytest.raises(KeyError):
        schemas.wire_error_param("identity_scene", "reference_photos", registry=registry)


def test_media_registry_rejects_malformed_error_maps(tmp_path):
    bad_maps = [None, [], True, {}, {"": {"x": "field"}}, {3: {"x": "field"}},
                {"domain": {}}, {"domain": []}, {"domain": False}, {"domain": {7: "field"}},
                {"domain": {"": "field"}}, {"domain": {"Upper": "field"}},
                {"domain": {"a" * 65: "field"}}, {"domain": {"key": ""}},
                {"domain": {"key": True}}, {"domain": {"key": 1}},
                {"domain": {"key": {"$ref": "other"}}}, {"domain": {"key": "bad field"}},
                {"domain": {"key": "1field"}}, {"domain": {"key": "a" * 129}},
                {"domain": {"key": "os.system()"}}, {"domain": {"key": "field\n"}}]
    documents = []
    for value in bad_maps:
        document = _document()
        document["wire_error_params"] = value
        documents.append(document)
    for version in (True, 2, "1", None):
        document = _document()
        document["schema_version"] = version
        documents.append(document)
    for missing in ("schema_version", "endpoints"):
        document = _document()
        del document[missing]
        documents.append(document)
    document = _document()
    document["python_import"] = "os.system"
    documents.append(document)
    for document in documents:
        path = _write(tmp_path, document)
        with pytest.raises(ValueError) as failure:
            schemas.load_media_schema_registry(path)
        assert str(path) in str(failure.value)


def test_media_registry_rejects_duplicate_merged_and_executable_yaml(tmp_path):
    document = _document()
    del document["wire_error_params"]
    prefix = yaml.safe_dump(document)
    additions = [
        "wire_error_params:\n  domain:\n    key: first\n    key: second\n",
        "wire_error_params:\n  domain: {key: first}\n  domain: {key: second}\n",
        "wire_error_params: {domain: {key: first}}\nwire_error_params: {domain: {key: second}}\n",
        "wire_error_params:\n  domain:\n    <<: {key: first}\n    other: second\n",
        "wire_error_params: !!python/object/apply:os.system ['false']\n",
    ]
    for addition in additions:
        path = tmp_path / "invalid.yml"
        path.write_text(prefix + addition, encoding="utf-8")
        with pytest.raises(ValueError) as failure:
            schemas.load_media_schema_registry(path)
        assert str(path) in str(failure.value)


def test_legacy_fragment_api_remains_compatible(tmp_path):
    document = _document()
    del document["wire_error_params"]
    path = _write(tmp_path, document)
    registry = schemas.load_media_schema_registry(path)
    assert dict(registry.wire_error_params) == {}
    assert dict(registry.endpoints) == document["endpoints"]
    fragments = schemas._load_schema_fragments(path)
    assert type(fragments) is dict
    fragment = schemas._endpoint_fragment("GET", "/v1/probe", fragments=fragments)
    fragment["responses"]["200"]["content"].clear()
    assert fragments == document["endpoints"]
    fragments["GET /v1/probe"]["responses"].clear()
    assert dict(registry.endpoints) == document["endpoints"]
    with pytest.raises(KeyError):
        schemas.wire_error_param("identity_scene", "reference_photos", registry=registry)
    with pytest.raises(FrozenInstanceError):
        registry.endpoints = {}
    with pytest.raises(FileNotFoundError):
        schemas.load_media_schema_registry(tmp_path / "missing.yml")
    with pytest.raises(FileNotFoundError):
        schemas._load_schema_fragments(tmp_path / "missing.yml")
    with patch.object(Path, "read_text", side_effect=PermissionError("unreadable")):
        with pytest.raises(PermissionError, match="unreadable"):
            schemas.load_media_schema_registry(path)
        with pytest.raises(PermissionError, match="unreadable"):
            schemas._load_schema_fragments(path)


def test_registry_owns_validated_mappings(tmp_path):
    document = _document()
    path = _write(tmp_path, document)
    with patch.object(schemas.yaml, "load", return_value=document):
        registry = schemas.load_media_schema_registry(path)
    document["endpoints"]["GET /v1/probe"]["responses"].clear()
    document["wire_error_params"]["identity_scene"]["reference_photos"] = "changed"
    assert registry.endpoints["GET /v1/probe"]["responses"]
    assert schemas.wire_error_param("identity_scene", "reference_photos", registry=registry) == "image"
    with pytest.raises(TypeError):
        registry.wire_error_params["identity_scene"]["reference_photos"] = "changed"


def test_missing_error_mapping_fails_startup(monkeypatch):
    production = yaml.safe_load(REGISTRY_PATH.read_text())
    missing_maps = [None, {"segment_chain": {"source_clip": "video"}},
                    {"identity_scene": {"reference_photos": "image"}},
                    {"identity_scene": {"other": "image"}, "segment_chain": {"source_clip": "video"}},
                    {"identity_scene": {"reference_photos": "image"}, "segment_chain": {"other": "video"}}]
    original = Path.read_text
    for missing in missing_maps:
        document = deepcopy(production)
        if missing is None:
            document.pop("wire_error_params", None)
        else:
            document["wire_error_params"] = missing

        def read(path, *args, **kwargs):
            return yaml.safe_dump(document) if path == REGISTRY_PATH else original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            with pytest.raises(KeyError) as failure:
                _fresh_module(monkeypatch)
        assert "identity_scene" in str(failure.value) or "segment_chain" in str(failure.value)
