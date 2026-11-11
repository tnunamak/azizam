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
