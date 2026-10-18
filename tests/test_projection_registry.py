"""Decision D28: one deny-default projection per domain type; a field nobody classified is refused.

Every field of a type is classified once (``gateway/projection_types.py``). The projector builds a fresh tree from the
registered types only, with the exact JSON types, inside a size budget; a field the type does not name is dropped and
counted; a value that is not the shape the type declares is left out and reported; a secret shows as an entry with a
configured flag, never a value. ``verify`` fails when the code that builds a record writes a field no type classifies.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gateway import extension_packages, projection, projection_types, secret_refs, service_registry

FIXTURE = Path(__file__).parent / "fixtures" / "live_shaped_registry.json"
ALL_CONFIGURED = lambda ref: True  # noqa: E731


def _ref() -> str:
    return secret_refs.new_ref()


def _service(**fields):
    return {"id": "engine-a", "name": "Engine A", "kind": "engine", "enabled": True, "capabilities": ["chat"],
            "config": {"host": "h.invalid", "port": 8080, **fields.pop("config", {})}, **fields}


def test_every_field_a_builder_writes_is_classified():
    projection_types.verify()  # raises, naming the type and field, when one is not


def test_a_field_added_to_the_builder_without_a_classification_fails_verify():
    with pytest.raises(ValueError, match="ServiceRecord.*brand_new_field"):
        projection_types.verify(produced={"ServiceRecord": (*service_registry.SERVICE_RECORD_FIELDS, "brand_new_field")})
    with pytest.raises(ValueError, match="PackageRecord.*another_field"):
        projection_types.verify(produced={"PackageRecord": (*extension_packages.PACKAGE_RECORD_FIELDS, "another_field")})


def test_what_the_builder_really_writes_is_covered():
    """The tuples ``verify`` reads are the fields the builder writes, not a copy that can drift."""
    record = service_registry.normalize_service_record("engine-a", {
        "id": "engine-a", "config": {"host": "h", "port": 1}, "process": {}, "observations": {}, "residency": {}, "resources": {}})
    assert set(record) <= set(service_registry.SERVICE_RECORD_FIELDS)
    package = extension_packages.package_index_record({"package": {"id": "p"}}, source={"type": "git"})
    assert set(package) <= set(extension_packages.PACKAGE_RECORD_FIELDS)


def test_a_field_the_type_does_not_name_is_forbidden_not_shown():
    shown = projection.project("ServiceRecord", _service(surprise="hello", process={"driver": "none", "undeclared": 1}),
                               configured=ALL_CONFIGURED)
    assert "surprise" not in shown.tree and "undeclared" not in shown.tree["process"]
    assert {"surprise", "process.undeclared"} <= set(shown.dropped)


def test_config_is_a_typed_map_whose_other_keys_are_public_json():
    shown = projection.project("ServiceRecord", _service(config={"note": "hello", "nested": {"a": [1, 2.5, None, True]}}), configured=ALL_CONFIGURED)
    assert shown.tree["config"]["note"] == "hello" and shown.tree["config"]["nested"] == {"a": [1, 2.5, None, True]}


def test_only_the_exact_json_types_are_accepted():
    class Text(str):
        pass

    shown = projection.project("ServiceRecord", _service(config={"a": Text("x"), "b": float("nan"), "c": {1: "int key"}, "d": object()}),
                               configured=ALL_CONFIGURED)
    assert "a" not in shown.tree["config"] or shown.tree["config"]["a"] == "x"
    assert "b" not in shown.tree["config"] and "d" not in shown.tree["config"]
    assert shown.shape_errors


def test_a_value_of_the_wrong_shape_is_left_out_and_reported_and_the_rest_is_shown():
    shown = projection.project("ServiceRecord", _service(process="text where a mapping belongs", config={"port": 8080, "headers": "text"}),
                               configured=ALL_CONFIGURED)
    assert "process" not in shown.tree and "headers" not in shown.tree["config"] and shown.tree["config"]["port"] == 8080
    assert {"process", "config.headers"} <= set(shown.shape_errors)


def test_a_secret_shows_as_an_entry_with_a_configured_flag_and_never_a_value():
    ref = _ref()
    record = _service(config={
        "headers": [{"id": "ent_" + "a" * 16, "name": "X-Vendor", "secret_ref": ref}, {"id": "ent_" + "b" * 16, "name": "X-Region", "value": "eu", "public": True}],
        "api_key_secret_id": "provider:x:api_key",
        "address": "http://h.invalid:8080/v1", "address_entries": {"userinfo": {"id": "ent_" + "c" * 16, "secret_ref": ref}}},
        process={"driver": "process", "start": ["run", {"id": "ent_" + "d" * 16, "secret_ref": ref, "prefix": "--key="}],
                 "env": {"TOKEN": {"id": "ent_" + "e" * 16, "secret_ref": ref}}},
        lifecycle={"admin_key": {"id": "ent_" + "f" * 16, "secret_ref": ref}})

    shown = projection.project("ServiceRecord", record, configured=lambda value: value == ref).tree

    assert shown["config"]["headers"] == [{"id": "ent_" + "a" * 16, "name": "X-Vendor", "configured": True},
                                          {"id": "ent_" + "b" * 16, "name": "X-Region", "value": "eu", "public": True}]
    assert shown["config"]["api_key_secret_id"] == {"configured": False}
    assert shown["config"]["address_entries"] == {"userinfo": {"id": "ent_" + "c" * 16, "configured": True}}
    assert shown["process"]["start"] == ["run", {"id": "ent_" + "d" * 16, "prefix": "--key=", "configured": True}]
    assert shown["process"]["env"] == {"TOKEN": {"id": "ent_" + "e" * 16, "configured": True}}
    assert shown["lifecycle"]["admin_key"] == {"id": "ent_" + "f" * 16, "configured": True}
    assert ref not in json.dumps(shown)  # not even the reference id of a secret is shown


def test_a_header_or_env_value_written_before_decision_d26_is_never_shown():
    shown = projection.project("ServiceRecord", _service(config={"headers": [{"name": "X-Vendor", "value": "SEED-legacy-1"}], "env": {"A": "SEED-legacy-2"}}),
                               configured=ALL_CONFIGURED).tree
    assert "SEED-legacy" not in json.dumps(shown)
    assert shown["config"]["headers"] == [{"name": "X-Vendor", "configured": False, "legacy": True}]
    assert shown["config"]["env"] == {"A": {"configured": False, "legacy": True}}


def test_url_user_info_in_a_text_is_removed_as_a_second_guard():
    shown = projection.project("ServiceRecord", _service(config={"note": "see http://u:SEED-pass-3@h.invalid/x"}), configured=ALL_CONFIGURED).tree
    assert "SEED-pass-3" not in json.dumps(shown) and "http://h.invalid/x" in json.dumps(shown)


def test_the_projection_is_a_fresh_tree_that_shares_nothing_with_the_stored_value():
    stored = _service(config={"models": [{"id": "m"}]})
    before = copy.deepcopy(stored)
    shown = projection.project("ServiceRecord", stored, configured=ALL_CONFIGURED).tree
    shown["config"]["models"][0]["id"] = "changed"
    assert stored == before


def test_a_registry_shaped_like_the_live_one_projects_faithfully_but_for_its_secret_store_ids():
    registry = json.loads(FIXTURE.read_text())
    shown = projection.project("RegistryDoc", registry, configured=ALL_CONFIGURED)
    assert shown.dropped == [] and shown.shape_errors == []

    def diff(a, b, path=""):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in set(a) | set(b):
                yield from diff(a.get(key, "<missing>"), b.get(key, "<missing>"), f"{path}.{key}")
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for index, (x, y) in enumerate(zip(a, b)):
                yield from diff(x, y, f"{path}[{index}]")
        elif a != b:
            yield path

    changed = sorted(diff(registry["services"], shown.tree["services"], "services"))
    assert changed == ["services.openrouter.config.api_key_secret_id", "services.openrouter.secret_refs.credential_origin",
                       "services.typesafe.config.api_key_secret_id"]


def test_a_body_with_a_key_nobody_classified_is_refused_whole():
    with pytest.raises(projection.ProjectionViolation):
        projection_types.project_body({"service_registry": {}, "mystery": 1})
    with pytest.raises(projection.ProjectionViolation):
        projection.project("NoSuchType", {})


def test_a_response_larger_than_the_budget_is_refused(monkeypatch):
    monkeypatch.setattr(projection, "MAX_NODES", 50)
    with pytest.raises(projection.ProjectionViolation):
        projection.project("ServiceRecord", _service(config={"big": list(range(500))}), configured=ALL_CONFIGURED)


def test_a_left_out_record_that_parses_is_shown_through_its_type_and_one_that_does_not_only_as_its_error():
    parsed = projection_types.excluded_view("services", "held", "bad models", _service(config={"models": ["x"]}), configured=ALL_CONFIGURED)
    assert parsed["parsed"] is True and parsed["record"]["config"]["models"] == ["x"] and "failed_fields" not in parsed

    unparsed = projection_types.excluded_view("services", "held", "config must be an object", {"id": "held", "config": "password=SEED-x-4"})
    assert unparsed["parsed"] is False and "record" not in unparsed and unparsed["failed_fields"] == ["config"]
    assert "SEED-x-4" not in json.dumps(unparsed)

    string = projection_types.excluded_view("services", "junk", "not an object", "password=SEED-x-5")
    assert string["parsed"] is False and "SEED-x-5" not in json.dumps(string)


def test_the_error_of_a_left_out_record_loses_a_secret_it_still_holds_inline():
    record = _service(config={"headers": [{"name": "X-Vendor", "value": "SEED-inline-6"}], "models": ["x"]})
    shown = projection_types.excluded_view("services", "held", "bad: 'SEED-inline-6' at http://u:SEED-pw-7@h/x", record)
    assert "SEED-inline-6" not in json.dumps(shown) and "SEED-pw-7" not in json.dumps(shown)
