"""The access plan editor reads its choices from the service registry (data, not code)."""

from __future__ import annotations

from gateway.access_plans import plan_from_document
from gateway.admin_catalog import access_plan_editor_contract


def _registry() -> dict[str, object]:
    return {
        "services": {
            "extension-engine": {"enabled": True, "name": "Extension engine", "capabilities": ["chat"],
                                 "config": {"models": [{"id": "extension-public-model"}]}},
            "parked-engine": {"enabled": False, "capabilities": ["image"],
                              "config": {"models": [{"id": "parked-image-model"}]}},
            "extension-cloud": {"enabled": True, "config": {"provider_type": "paid-remote", "api_key_env": "EXTENSION_CLOUD_KEY"}},
        },
        "workflows": {},
    }


def _items(contract, capability):
    return {item["id"]: item for entry in contract["capabilities"] if entry["id"] == capability
            for item in entry["items"]}


def test_data_only_services_become_choices_without_code_ids():
    contract = access_plan_editor_contract(_registry())
    assert _items(contract, "chat")["extension-public-model"] == {
        "kind": "model", "id": "extension-public-model", "available": True}
    assert _items(contract, "image")["parked-image-model"]["available"] is False
    assert {engine["id"] for engine in contract["engines"]} == {"extension-engine", "parked-engine"}
    assert [provider["id"] for provider in contract["paid_providers"]] == ["extension-cloud"]


def test_new_plan_document_is_a_valid_local_only_plan():
    plan = plan_from_document(access_plan_editor_contract(_registry())["new_plan"] | {"name": "New"}, plan_id="new")
    assert plan.paid_providers == frozenset()
    assert not plan.direct_engine_access.granted
    assert all(grant.all_items for grant in plan.can_use.values())
