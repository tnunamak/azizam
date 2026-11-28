"""The engine editor keeps the tasks a protocol description declares honest (follow-ups to the owner chain step 3).

An engine that dispatches by an installed description may offer the description's operations as tasks. Saving the engine
again without such a task drops it, and the engine view lists the tasks it offers, so reopening the engine shows them.
"""
from __future__ import annotations

from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.test_owner_engine_chain_smell import (  # noqa: F401 - owner and smell_engine are fixtures
    HOST, _add_engine, _install_description, _write_headers, owner, smell_engine,
)
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401 - gateway is a fixture

SMELL_MODELS = [{"id": "smell-1", "public_name": "smell-1"}]


def _edit(client, body):
    return client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={
        "definition": "openai-compatible",
        "values": {"address": f"http://{HOST}", "dispatch": "protocol", "protocol_description": "d0-smell"}, **body})


def _view(client):
    return client.get("/admin/api/engines/smell-box").json()["engine"]


def _stored(gateway):
    from gateway import admin_store
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["smell-box"]


def test_the_engine_view_lists_the_described_task_it_offers(owner):
    client, *_ = owner
    assert _install_description(client).status_code == 201
    assert _add_engine(client, capabilities=["smell.infer"], models=SMELL_MODELS).status_code == 201
    assert "smell.infer" in _view(client)["capabilities"]


def test_an_edit_that_leaves_out_a_described_task_drops_it(owner, gateway):
    client, *_ = owner
    assert _install_description(client).status_code == 201
    assert _add_engine(client, capabilities=["smell.infer"], models=SMELL_MODELS).status_code == 201
    assert "smell.infer" in _stored(gateway)["capabilities"]
    chosen = [c for c in _view(client)["capabilities"] if c != "smell.infer"] or ["chat"]
    edited = _edit(client, {"capabilities": chosen, "models": SMELL_MODELS, "disabled": True})
    assert edited.status_code == 200, edited.text
    assert "smell.infer" not in _stored(gateway)["capabilities"]
    assert "smell.infer" not in _view(client)["capabilities"]


def test_an_edit_that_names_the_described_task_keeps_it(owner, gateway):
    client, *_ = owner
    assert _install_description(client).status_code == 201
    assert _add_engine(client, capabilities=["smell.infer"], models=SMELL_MODELS).status_code == 201
    edited = _edit(client, {"capabilities": _view(client)["capabilities"], "models": SMELL_MODELS, "disabled": True})
    assert edited.status_code == 200, edited.text
    assert "smell.infer" in _stored(gateway)["capabilities"] and "smell.infer" in _view(client)["capabilities"]


def test_a_task_alone_with_no_model_row_is_stored_for_an_engine_kept_off(owner, gateway):
    # The add-engine dialog sends the ticked described tasks with the engine turned off; models are added later.
    client, *_ = owner
    assert _install_description(client).status_code == 201
    created = _add_engine(client, capabilities=["smell.infer"])
    assert created.status_code == 201, created.text
    assert _stored(gateway)["capabilities"] == ["smell.infer"] and "smell.infer" in _view(client)["capabilities"]


def test_replacing_a_description_in_use_cannot_orphan_the_engines_task(owner, gateway):
    client, *_ = owner
    etag = _install_description(client).headers["ETag"]
    assert _add_engine(client, capabilities=["smell.infer"], models=SMELL_MODELS).status_code == 201
    from tests.test_owner_engine_chain_smell import SMELL
    import yaml
    doc = yaml.safe_load(SMELL.read_text(encoding="utf-8"))
    only = dict(doc, operations={"smell.infer": doc["operations"]["smell.infer"]})
    # Replacing with a document that keeps the task works; the engine keeps advertising it.
    kept = client.put("/admin/api/engine-protocols/d0-smell", content=yaml.safe_dump(only),
                      headers={**_write_headers(client), "Content-Type": "application/yaml", "If-Match": etag})
    assert kept.status_code == 200, kept.text
    etag = kept.headers["ETag"]
    renamed = {"smell.other": only["operations"]["smell.infer"]}
    refused = client.put("/admin/api/engine-protocols/d0-smell", content=yaml.safe_dump(dict(doc, operations=renamed)),
                         headers={**_write_headers(client), "Content-Type": "application/yaml", "If-Match": etag})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "protocol_in_use", refused.text
    assert "smell-box" in refused.text and "smell.infer" in refused.text
    assert "smell.infer" in _stored(gateway)["capabilities"]
    assert client.get("/admin/api/engine-protocols/d0-smell").headers["ETag"] == etag


def test_a_delete_that_lands_between_an_engine_create_check_and_its_write_leaves_no_dangling_reference(owner, gateway, monkeypatch):
    """Pause the create after it has read the description, delete the description, resume: the write refuses itself."""
    from gateway import admin_runtime, protocol_description_store
    client, *_ = owner
    etag = _install_description(client).headers["ETag"]
    revision = int(etag.strip('"').removeprefix("pd-"))
    original = admin_runtime.AdminSQLiteStore.write_engine
    seen = {}

    def paused(self, **kwargs):
        seen["checked"] = True  # every check of the create has run; only the write is left
        protocol_description_store.delete("d0-smell", expect=revision, path=gateway.path)
        seen["deleted"] = True
        return original(self, **kwargs)

    monkeypatch.setattr(admin_runtime.AdminSQLiteStore, "write_engine", paused)
    created = _add_engine(client, capabilities=["smell.infer"], models=SMELL_MODELS)
    assert seen == {"checked": True, "deleted": True}
    assert created.status_code == 409 and created.json()["error"]["code"] == "protocol_description_changed", created.text
    from gateway import admin_store
    stored = admin_store.get_service_registry_with_generation(path=gateway.path)
    assert "smell-box" not in ((stored or {}).get("value", {}).get("services") or {})
    assert protocol_description_store.get("d0-smell", gateway.path) is None
