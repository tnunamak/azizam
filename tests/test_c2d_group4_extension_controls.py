"""Golden compatibility and closed-schema contracts for gateway extension controls."""

from __future__ import annotations

import builtins
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
from types import MappingProxyType

import pytest
import yaml

from gateway import video_controls

FIXTURE = Path(__file__).parent / "fixtures" / "c2d_group4" / "video_controls_v1.json"


def _golden():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _outcome(body):
    try:
        return {"result": video_controls.parse_video_controls(body)}
    except video_controls.VideoControlError as exc:
        return {"error": {
            "class": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "param": exc.param,
        }}


def _document():
    return {
        "schema_version": 1,
        "groups": {
            "clip_delivery": {
                "namespace": "com.azizam.video",
                "controls": {
                    "retain_sound": {"field": "audio", "type": "boolean", "default": True},
                },
                "errors": {
                    "block_not_object": "extensions.{namespace} must be an object",
                    "unknown_control": "{field} is not a video control",
                    "invalid_value": "{field} must be true or false",
                },
            },
        },
    }


def _at(document, path):
    for part in path:
        document = document[part]
    return document


def _replaced(path, value):
    if not path:
        return value
    document = _document()
    _at(document, path[:-1])[path[-1]] = value
    return document


def test_video_control_adapter_matches_prechange_matrix():
    for case in _golden()["cases"]:
        actual = _outcome(case["body"])
        assert actual == case["expected"], case["id"]
        if "result" in actual:
            assert type(actual["result"]) is dict, case["id"]
            for name, value in actual["result"].items():
                assert type(value) is type(case["expected"]["result"][name]), case["id"]

    # Mapping, not only dict, remains accepted at every envelope level.
    body = MappingProxyType({"extensions": MappingProxyType({
        "com.azizam.video": MappingProxyType({"audio": False}),
    })})
    assert video_controls.parse_video_controls(body) == {"audio": False}
    # Python mappings can have non-string keys; preserve their public error path.
    assert _outcome({"extensions": {"com.azizam.video": {7: True}}}) == {
        "error": {
            "class": "gateway.video_controls.VideoControlError",
            "message": "7 is not a video control",
            "param": "extensions.com.azizam.video.7",
        },
    }


def test_extension_control_registry_is_closed_and_loaded_once(tmp_path, monkeypatch):
    from gateway import extension_controls as controls

    path = tmp_path / "controls.yml"

    def load(document):
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return controls.load_extension_controls(path)

    groups = load(_document())
    group = groups["clip_delivery"]
    descriptor = group.controls["retain_sound"]
    assert group.namespace == "com.azizam.video"
    assert group.public_fields == frozenset({"audio"})
    assert (descriptor.field, descriptor.type, descriptor.default) == ("audio", "boolean", True)
    assert type(descriptor.default) is bool
    assert dict(group.errors) == _document()["groups"]["clip_delivery"]["errors"]

    # Every exposed layer is immutable and owns its mapping, including direct construction.
    with pytest.raises(TypeError):
        groups["other"] = group
    with pytest.raises(FrozenInstanceError):
        group.namespace = "changed"
    with pytest.raises(TypeError):
        group.controls["other"] = descriptor
    with pytest.raises(FrozenInstanceError):
        descriptor.default = False
    with pytest.raises(TypeError):
        group.errors["invalid_value"] = "changed"
    owned_controls, owned_errors = dict(group.controls), dict(group.errors)
    owned = controls.ControlGroup(group.namespace, owned_controls, owned_errors)
    owned_controls.clear()
    owned_errors.clear()
    assert owned.controls == group.controls
    assert owned.errors == group.errors
    assert owned.public_fields == group.public_fields

    group_path = ("groups", "clip_delivery")
    control_path = (*group_path, "controls", "retain_sound")
    errors_path = (*group_path, "errors")
    closed_paths = [(), group_path, control_path, errors_path]
    for location in closed_paths:
        for key in _at(_document(), location):
            document = _document()
            del _at(document, location)[key]
            with pytest.raises(ValueError):
                load(document)
        document = _document()
        _at(document, location)["unexpected"] = True
        with pytest.raises(ValueError):
            load(document)

    mapping_paths = [(), ("groups",), group_path, (*group_path, "controls"), control_path, errors_path]
    for location in mapping_paths:
        for invalid_key in ("", 1):
            document = _document()
            _at(document, location)[invalid_key] = None
            with pytest.raises(ValueError):
                load(document)
        for value in (None, [], "bad", 1, True):
            with pytest.raises(ValueError):
                load(_replaced(location, value))
    for location in (("groups",), (*group_path, "controls")):
        with pytest.raises(ValueError):
            load(_replaced(location, {}))
    for value in (True, False, 0, 2, 1.0, "1", None):
        with pytest.raises(ValueError):
            load(_replaced(("schema_version",), value))
    for location in ((*group_path, "namespace"), (*control_path, "field")):
        for value in ("", None, 0, True, [], {}):
            with pytest.raises(ValueError):
                load(_replaced(location, value))
    for value in ("string", "Boolean", "", None, True, [], {}):
        with pytest.raises(ValueError):
            load(_replaced((*control_path, "type"), value))
    for value in (0, 1, "true", None, [], {}):
        with pytest.raises(ValueError):
            load(_replaced((*control_path, "default"), value))
    for location in (("groups",), (*group_path, "controls")):
        original = _at(_document(), location)
        child = next(iter(original.values()))
        for name in ("", "9first", "Upper", "with-dash", "with.dot", "two words", "é", 1, "a" * 65):
            with pytest.raises(ValueError):
                load(_replaced(location, {name: child}))
    document = _document()
    document["groups"]["clip_delivery"]["controls"]["other"] = deepcopy(
        document["groups"]["clip_delivery"]["controls"]["retain_sound"],
    )
    with pytest.raises(ValueError):
        load(document)

    for key, allowed in (("block_not_object", "namespace"), ("unknown_control", "field"), ("invalid_value", "field")):
        for invalid in (None, 1, [], {}, "{other}", "{}", "{0}", "{", "}",
                        "{" + allowed + ".name}", "{" + allowed + "[0]}",
                        "{" + allowed + "!r}", "{" + allowed + ":>10}",
                        "{" + allowed + ":{other}}"):
            with pytest.raises(ValueError):
                load(_replaced((*errors_path, key), invalid))
        # The other declared error's token is not authorized here.
        with pytest.raises(ValueError):
            load(_replaced((*errors_path, key), "{field}" if allowed == "namespace" else "{namespace}"))
    literal = load(_replaced((*errors_path, "invalid_value"), "{{literal}} {field}"))["clip_delivery"]
    with pytest.raises(controls.ControlValidationError, match=r"^\{literal\} audio$"):
        controls.parse_control_group({"extensions": {"com.azizam.video": {"audio": 0}}}, literal)

    # Raw YAML is necessary: a dict cannot represent duplicate or merge keys.
    raw = yaml.safe_dump(_document(), sort_keys=False)
    duplicate_cases = [
        raw.replace("schema_version: 1", "schema_version: 1\nschema_version: 1", 1),
        raw.replace("  clip_delivery:\n", "  clip_delivery: {}\n  clip_delivery:\n", 1),
        raw.replace("    namespace: com.azizam.video", "    namespace: duplicate\n    namespace: com.azizam.video", 1),
        raw.replace("      retain_sound:\n", "      retain_sound: {}\n      retain_sound:\n", 1),
        raw.replace("        field: audio", "        field: duplicate\n        field: audio", 1),
        raw.replace("    errors:\n", "    errors:\n      invalid_value: duplicate\n", 1),
        raw.replace("  clip_delivery:\n", "  clip_delivery:\n    <<: {namespace: ignored}\n", 1),
        raw.replace("      retain_sound:\n", "      retain_sound:\n        <<: {default: false}\n", 1),
        "schema_version: 1\ngroups: &cycle {again: *cycle}\n",
        "schema_version: 1\ngroups: !!python/object:builtins.object {}\n",
        "[unterminated",
        raw + "---\n{}\n",
    ]
    for malformed in duplicate_cases:
        path.write_text(malformed, encoding="utf-8")
        with pytest.raises(ValueError):
            controls.load_extension_controls(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        controls.load_extension_controls(path)
    with pytest.raises(OSError):
        controls.load_extension_controls(tmp_path / "missing.yml")
    with pytest.raises(KeyError):
        controls.control_group("missing_group")

    # Execute a fresh module instance without reloading the production class identities.
    module_path = Path(controls.__file__)
    registry_path = module_path.resolve().parent.parent / "registry" / "extension_controls.yml"
    reads = []
    read_text = Path.read_text

    def counted_read(self, *args, **kwargs):
        if self.resolve() == registry_path:
            reads.append(self)
        return read_text(self, *args, **kwargs)

    module_name = "_extension_controls_load_once_probe"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, module_name, fresh)
        patch.setattr(Path, "read_text", counted_read)
        spec.loader.exec_module(fresh)
        assert len(reads) == 1

        def no_read(*args, **kwargs):
            raise AssertionError("accessors and wire parsing must not read files")

        patch.setattr(Path, "read_text", no_read)
        patch.setattr(builtins, "open", no_read)
        patch.setattr(io, "open", no_read)
        for _ in range(2):
            cached = fresh.control_group("clip_delivery")
            assert cached is fresh.control_group("clip_delivery")
            assert fresh.parse_control_group({"extensions": {cached.namespace: {}}}, cached) == {"audio": True}
            assert video_controls.parse_video_controls({"extensions": {cached.namespace: {}}}) == {"audio": True}
            assert fresh.parse_control_group(None, cached) == {}
            with pytest.raises(KeyError):
                fresh.control_group("missing_group")


def test_control_public_name_is_descriptor_owned(tmp_path):
    from gateway import extension_controls as controls

    document = _document()
    source = document["groups"].pop("clip_delivery")
    document["groups"]["general_delivery"] = source
    source["namespace"] = "org.example.options"
    source["controls"] = {
        "keep_option": {"field": "keep_copy", "type": "boolean", "default": False},
        "show_option": {"field": "show_receipt", "type": "boolean", "default": True},
    }
    source["errors"] = {
        "block_not_object": "{namespace} must be an object",
        "unknown_control": "Unknown option {field}",
        "invalid_value": "{field} must be true or false",
    }
    path = tmp_path / "unfamiliar.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    # This synthetic namespace has its own independent public wire contract.
    request_schema = {
        "type": "object",
        "properties": {"extensions": {
            "type": "object",
            "properties": {"org.example.options": {
                "type": "object",
                "properties": {
                    "keep_copy": {"type": "boolean", "default": False},
                    "show_receipt": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            }},
            "additionalProperties": True,
        }},
    }
    group = controls.load_extension_controls(path, request_schema=request_schema)["general_delivery"]
    assert group.public_fields == frozenset({"keep_copy", "show_receipt"})
    assert controls.parse_control_group({}, group) == {}
    assert controls.parse_control_group({"extensions": {group.namespace: {}}}, group) == {
        "keep_copy": False, "show_receipt": True,
    }
    for value in (True, False):
        assert controls.parse_control_group({"extensions": {group.namespace: {"keep_copy": value}}}, group) == {
            "keep_copy": value, "show_receipt": True,
        }
    for bad in (0, 1, "false", None, [], {}):
        with pytest.raises(controls.ControlValidationError) as caught:
            controls.parse_control_group({"extensions": {group.namespace: {"keep_copy": bad}}}, group)
        assert str(caught.value) == "keep_copy must be true or false"
        assert caught.value.param == "extensions.org.example.options.keep_copy"
    with pytest.raises(controls.ControlValidationError) as caught:
        controls.parse_control_group({"extensions": {group.namespace: []}}, group)
    assert str(caught.value) == "org.example.options must be an object"
    assert caught.value.param == "extensions.org.example.options"
    for block, first in (({"keep_copy": 0, "extra": None}, "extra"),
                         ({"second": 0, "first": 0}, "second"),
                         ({"first": 0, "second": 0}, "first")):
        with pytest.raises(controls.ControlValidationError) as caught:
            controls.parse_control_group({"extensions": {group.namespace: block}}, group)
        assert str(caught.value) == f"Unknown option {first}"
        assert caught.value.param == f"extensions.org.example.options.{first}"
    # Mutating the parser result cannot alter defaults or later results.
    result = controls.parse_control_group({"extensions": {group.namespace: {}}}, group)
    result["keep_copy"] = True
    assert controls.parse_control_group({"extensions": {group.namespace: {}}}, group)["keep_copy"] is False


def test_video_control_legacy_exports_are_unchanged():
    exports = _golden()["exports"]
    for name in ("VIDEO_EXTENSION_NAMESPACE", "AUDIO_CONTROL", "VIDEO_CONTROLS"):
        value = getattr(video_controls, name)
        assert type(value).__name__ == exports[name]["type"]
        assert (sorted(value) if isinstance(value, frozenset) else value) == exports[name]["value"]
    assert str(inspect.signature(video_controls.parse_video_controls)) == exports["parse_video_controls"]["signature"]
    error_type = video_controls.VideoControlError
    assert f"{error_type.__module__}.{error_type.__qualname__}" == exports["VideoControlError"]["class"]
    assert error_type.__bases__ == (ValueError,)
    error = error_type("unchanged", param="extensions.com.azizam.video.audio")
    assert str(error) == "unchanged"
    assert error.args == ("unchanged",)
    assert error.param == "extensions.com.azizam.video.audio"
    assert video_controls.parse_video_controls({"extensions": {"com.azizam.video": {}}}) == {"audio": True}
    assert video_controls.parse_video_controls({"extensions": {"com.azizam.video": {"audio": False}}}) == {"audio": False}


@pytest.mark.parametrize(("path", "value", "expected"), [
    (("namespace",), "com.azizam.vidoe", "com.azizam.vidoe"),
    (("namespace",), "org.example.options", "org.example.options"),
    (("controls", "retain_sound", "field"), "aduio", "aduio"),
    (("controls",), {}, "com.azizam.video"),
    (("controls",), {
        "retain_sound": {"field": "audio", "type": "boolean", "default": True},
        "extra_option": {"field": "extra", "type": "boolean", "default": False},
    }, "extra"),
    (("controls", "retain_sound", "type"), "string", "com.azizam.video"),
    (("controls", "retain_sound", "default"), False, "audio"),
], ids=["namespace-typo", "undeclared-namespace", "field-typo", "missing-field",
        "extra-field", "type-mismatch", "default-mismatch"])
def test_production_controls_must_match_published_contract(tmp_path, path, value, expected):
    from gateway import extension_controls as controls

    document = _replaced(("groups", "clip_delivery", *path), value)
    registry = tmp_path / "controls.yml"
    registry.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        controls.load_extension_controls(registry)
    assert expected in str(failure.value)


def test_shipped_extension_controls_match_published_contract():
    from gateway import extension_controls as controls

    registry = Path(controls.__file__).resolve().parent.parent / "registry" / "extension_controls.yml"
    groups = controls.load_extension_controls(registry)
    assert groups["clip_delivery"] == controls.control_group("clip_delivery")
    group = groups["clip_delivery"]
    # Omission and null/non-object envelopes remain absent, not default-filled.
    for body in ({}, {"extensions": None}, {"extensions": []}, {"extensions": "ignored"},
                 {"extensions": 1}, {"extensions": False}, {"extensions": {}},
                 {"extensions": {group.namespace: None}}):
        assert controls.parse_control_group(body, group) == {}
    assert controls.parse_control_group({"extensions": {group.namespace: {}}}, group) == {"audio": True}
