"""Regression tests for the narrow policy-editor discovery contract."""

from __future__ import annotations

from gateway import admin_store
from gateway.admin_catalog import policy_editor_contract
from gateway.policy import DEFAULT_PROFILE, PolicyRequest, evaluate
from gateway.profiles import load_profile, serialize_default_profile


def _registry() -> dict[str, object]:
    return {"services": {"extension": {"enabled": True, "config": {"models": [{"id": "extension-public-model"}]}}}, "protocol_frontends": {"extension": {"protocol": "extension-api", "enabled": True, "intents": ["extension.run"], "routes": [{"runtime_policy": {"intent": "extension.route"}}]}, "disabled": {"protocol": "disabled-api", "enabled": False}}, "workflows": {}, "capabilities": {"extension": {"enabled": True, "intent": "extension.run", "workflow": "extension.workflow", "exposure": "default", "trust_tier": "stable", "resource_tier": "interactive", "tags": ["extension-tag"]}}}


def test_default_create_document_is_semantically_equivalent() -> None:
    assert load_profile(serialize_default_profile()) == DEFAULT_PROFILE
    document = serialize_default_profile()
    assert "protocols" not in document
    assert "allow" not in document


def test_omitted_and_empty_sets_have_different_policy_semantics() -> None:
    request = PolicyRequest(protocol="extension-api", intent="extension.run", workflow="extension.workflow")
    assert evaluate(load_profile({}), request).allowed
    assert not evaluate(load_profile({"protocols": []}), request).allowed
    assert not evaluate(load_profile({"workflows": []}), request).allowed
    assert not evaluate(load_profile({"allow": {"intents": []}}), request).allowed


def test_data_only_extension_is_projected_without_code_ids() -> None:
    contract = policy_editor_contract(_registry())
    choices = contract["profile"]["choices"]
    assert {choice["id"] for choice in choices["protocols"]} >= {"extension-api", "disabled-api"}
    assert next(choice for choice in choices["protocols"] if choice["id"] == "disabled-api")["available"] is False
    assert {choice["id"] for choice in choices["intents"]} >= {"extension.run", "extension.route"}
    assert "extension-public-model" in {choice["id"] for choice in choices["models"]}
    assert "extension-tag" in {choice["id"] for choice in choices["tags"]}


def test_source_specs_round_trip_through_real_normalizer() -> None:
    for source in admin_store.RUNTIME_PROFILE_MAPPING_SOURCE_SPECS:
        source_kind = str(source["id"])
        value = " Example@Example.COM " if source["value_mode"] == "required" else "ignored"
        normalized_kind, normalized_value = admin_store._normalize_runtime_mapping(source_kind, value)
        assert normalized_kind == source_kind
        if source["value_mode"] == "fixed":
            assert normalized_value == source["fixed_value"]
        else:
            assert normalized_value
