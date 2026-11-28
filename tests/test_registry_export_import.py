"""The service registry's owner layer in the config export and import (docs/design/CONFIG_HOMES.md, 6).

Export carries only what the owner holds: the records the owner added, the patches of release records, the tombstones.
Import is judged with the config sections and written in one transaction. The registry has its own revision, ``os-N``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from gateway import admin_store, config_api, service_registry
from tests.test_config_api import API, OWNER, SECTION, Gateway, export_of, setting

SENTINEL = "never-exported-release-text"


def _record(record_id: str, port: int = 9300) -> dict[str, Any]:
    return service_registry.normalize_service_record(record_id, {
        "name": record_id, "source": "ui", "kind": "provider", "enabled": True, "capabilities": ["chat"],
        "config": {"base_url": f"http://127.0.0.1:{port}/v1"},
    })


def _gateway(tmp_path, name: str) -> Gateway:
    gw = Gateway(tmp_path, name)
    service_registry.ensure_service_registry(path=gw.db, actor="test")  # a fresh database: the empty owner document
    return gw


def _generation(gw: Gateway) -> int:
    return admin_store.get_service_registry_with_generation(path=gw.db)["operator_settings_generation"]


def _edit(gw: Gateway, change) -> None:
    """Write the registry as the registry routes do: the whole effective document, read, changed, written back."""
    current = admin_store.get_service_registry_with_generation(path=gw.db)
    document = copy.deepcopy(current["value"])
    change(document)
    admin_store.write_service_registry(
        document, updated_by="tester", expected_generation=current["operator_settings_generation"], path=gw.db)


def _owner_changes(gw: Gateway) -> None:
    def change(document: dict[str, Any]) -> None:
        document["services"]["litellm"]["name"] = "Renamed LiteLLM"
        document["services"]["mine"] = _record("mine")
        del document["services"]["openrouter"]

    _edit(gw, change)


def _effective(gw: Gateway) -> dict[str, Any]:
    # The registry's updated_at is the second its own database last wrote it, not content an import carries,
    # so two gateways written a second apart differ there and nowhere else.
    effective = service_registry.normalize_service_registry(
        admin_store.get_service_registry_with_generation(path=gw.db)["value"])
    effective.pop("updated_at", None)
    return effective


def _body(source: dict[str, Any], gw: Gateway, **extra: Any) -> dict[str, Any]:
    return {"document": source, "if_match": {SECTION: gw.revision(), "registry": f"os-{_generation(gw)}"}, **extra}


def test_the_export_holds_the_owner_layer_and_no_release_record(tmp_path):
    gw = _gateway(tmp_path, "a.db")
    _owner_changes(gw)

    exported = export_of(gw)["registry"]

    assert exported["records"]["services"]["litellm"] == {"name": "Renamed LiteLLM"}  # a patch, not the record
    assert set(exported["records"]["services"]) == {"litellm", "mine"}
    assert exported["tombstones"] == {"services": ["openrouter"]}
    assert "protocol_frontends" not in exported["records"], "builtin frontends are the release's"


def test_an_install_that_changed_nothing_exports_an_empty_owner_layer(tmp_path):
    assert export_of(_gateway(tmp_path, "a.db"))["registry"] == {"records": {}, "tombstones": {}}


def test_export_then_import_gives_the_same_effective_registry_and_a_second_import_is_a_no_op(tmp_path):
    source, target = _gateway(tmp_path, "a.db"), _gateway(tmp_path, "b.db")
    _owner_changes(source)
    assert _effective(target) != _effective(source)

    response = target.client.post(f"{API}/import", json=_body(export_of(source), target), headers=OWNER)

    assert response.status_code == 200, response.text
    assert _effective(target) == _effective(source)
    assert export_of(target)["registry"] == export_of(source)["registry"]
    assert target.reloads >= 1, "the import reloaded"
    generation = _generation(target)
    again = target.client.post(f"{API}/import", json=_body(export_of(source), target), headers=OWNER)
    assert again.status_code == 200 and again.json()["registry"]["changed"] == []
    assert _generation(target) == generation, "a file that changes nothing writes nothing"


def test_a_dry_run_names_the_records_and_the_revision_and_writes_nothing(tmp_path):
    source, target = _gateway(tmp_path, "a.db"), _gateway(tmp_path, "b.db")
    _owner_changes(source)
    before = (_generation(target), export_of(target)["registry"])

    response = target.client.post(f"{API}/import?dry_run=1", json={"document": export_of(source)}, headers=OWNER)

    body = response.json()
    assert response.status_code == 200 and body["dry_run"], response.text
    assert sorted(body["registry"]["changed"]) == ["services/litellm", "services/mine", "services/openrouter"]
    assert body["revisions"]["registry"] == f"os-{before[0]}"
    assert (_generation(target), export_of(target)["registry"]) == before and target.reloads == 0


def test_a_registry_import_needs_its_revision_and_refuses_a_stale_one(tmp_path):
    source, target = _gateway(tmp_path, "a.db"), _gateway(tmp_path, "b.db")
    _owner_changes(source)
    document = export_of(source)

    missing = target.client.post(f"{API}/import", json={"document": document, "if_match": {SECTION: target.revision()}},
                                 headers=OWNER)
    stale = target.client.post(f"{API}/import", json={
        "document": document, "if_match": {SECTION: target.revision(), "registry": f"os-{_generation(target) + 7}"}},
        headers=OWNER)

    assert missing.status_code == 428 and stale.status_code == 412
    assert export_of(target)["registry"] == {"records": {}, "tombstones": {}}


def test_replace_drops_the_owner_records_the_file_does_not_name_and_merge_keeps_them(tmp_path):
    source = _gateway(tmp_path, "a.db")
    _edit(source, lambda document: document["services"].update({"theirs": _record("theirs", 9400)}))
    replace, merge = _gateway(tmp_path, "r.db"), _gateway(tmp_path, "m.db")
    for gw in (replace, merge):
        _edit(gw, lambda document: document["services"].update({"mine": _record("mine")}))

    for gw, mode in ((replace, "replace"), (merge, "merge")):
        response = gw.client.post(f"{API}/import", json=_body(export_of(source), gw, mode=mode), headers=OWNER)
        assert response.status_code == 200, response.text

    assert set(export_of(replace)["registry"]["records"]["services"]) == {"theirs"}
    assert set(export_of(merge)["registry"]["records"]["services"]) == {"theirs", "mine"}


def test_a_record_that_holds_a_secret_value_is_refused_and_nothing_is_written(tmp_path):
    target = _gateway(tmp_path, "b.db")
    leaky = {"records": {"services": {"leaky": {
        "id": "leaky", "name": "leaky", "source": "ui", "kind": "provider", "enabled": True, "capabilities": ["openai-compatible"],
        "config": {"provider_type": "paid-remote", "protocol": "openai-compatible", "base_url": "https://api.example.test/v1",
                   "api_key": SENTINEL}}}}, "tombstones": {}}
    document = {**export_of(target), "registry": leaky}
    generation = _generation(target)

    response = target.client.post(f"{API}/import", json=_body(document, target), headers=OWNER)

    assert response.status_code == 422, response.text
    assert SENTINEL not in response.text
    assert _generation(target) == generation


def test_a_malformed_registry_member_is_refused(tmp_path):
    target = _gateway(tmp_path, "b.db")
    for bad in ("text", {"records": {"nonsense": {}}}, {"tombstones": {"services": "openrouter"}}):
        response = target.client.post(f"{API}/import", json=_body({**export_of(target), "registry": bad}, target),
                                      headers=OWNER)
        assert response.status_code == 422, (bad, response.text)


def test_the_registry_and_the_sections_commit_together_or_not_at_all(tmp_path):
    source, target = _gateway(tmp_path, "a.db"), _gateway(tmp_path, "b.db")
    _owner_changes(source)
    source.patch(setting("/timeout", 60))
    document = export_of(source)
    document["sections"][SECTION][0]["value"] = "not a valid value for this entry"  # breaks the schema
    generation = _generation(target)

    refused = target.client.post(f"{API}/import", json=_body(document, target), headers=OWNER)

    assert refused.status_code == 422, refused.text
    assert _generation(target) == generation, "the registry write went with the refused change"
    accepted = target.client.post(f"{API}/import", json=_body(export_of(source), target), headers=OWNER)
    assert accepted.status_code == 200 and _generation(target) == generation + 1
    assert _effective(target) == _effective(source) and target.digest() == source.digest()


def test_the_import_audit_names_the_action_and_no_record_content(tmp_path):
    source, target = _gateway(tmp_path, "a.db"), _gateway(tmp_path, "b.db")
    _owner_changes(source)

    target.client.post(f"{API}/import", json=_body(export_of(source), target), headers=OWNER)

    events = admin_store.list_audit(action_prefix="operator.", path=target.db)
    assert any(event["action"] in ("operator.service_registry.import", "operator.service_registry.update") for event in events)
    assert "Renamed LiteLLM" not in json.dumps(events)
