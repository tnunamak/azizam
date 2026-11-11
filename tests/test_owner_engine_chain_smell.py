"""The owner chain for a new kind of engine, driven through the real routes (an evidence experiment).

The direct test (tests/test_smell_infer_offering.py) builds the connection and the adapter by hand. This module starts
where an owner starts: install the d0-smell description, add an engine record that names it, list it in the catalog,
then submit through a public route. Each step is its own test, so the first step that cannot work today is the one
marked as an expected failure, with the missing piece named in its reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401 - gateway is a fixture

ROOT = Path(__file__).resolve().parent.parent
SMELL = ROOT / "tests" / "fixtures" / "engine_protocol" / "d0" / "smell.yml"
HOST = "127.0.0.1:18301"
MOLECULE = json.dumps({"smiles": "CCO", "odour": ["alcohol", "sweet"]}, sort_keys=True).encode("utf-8")


class SmellEngine:
    """A plain ASGI app that speaks the d0-smell wire: submit, poll, fetch."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        path, method = scope["path"], scope["method"]
        status, content, ctype = 404, b"{}", "application/json"
        if method == "POST" and path == "/v1/infer":
            self.posts.append(json.loads(body))
            status, content = 200, json.dumps({"job": {"id": "smell-1"}}).encode()
        elif method == "GET" and path == "/v1/infer/smell-1":
            status, content = 200, json.dumps({"state": "ready"}).encode()
        elif method == "GET" and path == "/v1/infer/smell-1/molecule":
            status, content, ctype = 200, MOLECULE, "application/octet-stream"
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(content)).encode())]})
        await send({"type": "http.response.body", "body": content})


@pytest.fixture
def smell_engine(fake_upstreams, monkeypatch):  # noqa: F811 - fixture
    use_fake_engine_dns(monkeypatch)
    engine = SmellEngine()
    fake_upstreams.router.add(HOST, engine)
    return engine


@pytest.fixture
def owner(gateway, smell_engine, tmp_path, monkeypatch):  # noqa: F811 - fixture
    """A signed-in owner on a fresh gateway whose protocol overlay directory is empty and under the test's state."""
    overlay = tmp_path / "protocols"
    overlay.mkdir()
    monkeypatch.setenv("AZIZAM_PROTOCOLS_DIR", str(overlay))
    client, owner_id, plan_id, _ = owner_and_admin(gateway)
    return client, overlay, owner_id, plan_id


def _install_description(client):
    """The owner route: PUT the raw document, create-only. Nothing is written to a folder."""
    return client.put("/admin/api/engine-protocols/d0-smell", content=SMELL.read_text(encoding="utf-8"),
                      headers={**csrf_for(client), "Content-Type": "application/yaml", "If-None-Match": "*"})


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _add_engine(client, **extra):
    """The call the web UI's add-engine dialog makes for a protocol engine (web/src/app/engines/add-engine)."""
    return client.post("/admin/api/operator/services", headers=_write_headers(client), json={
        "definition": "openai-compatible", "name": "smell-box", "disabled": True,
        "values": {"address": f"http://{HOST}", "dispatch": "protocol", "protocol_description": "d0-smell"}, **extra})


def test_step1_the_owner_installs_the_description_with_put(owner):
    # PUT /admin/api/engine-protocols/{id} stores the raw document as an owner row; the overlay folder stays empty.
    client, overlay, *_ = owner
    before = client.get("/admin/api/engine-protocols")
    assert before.status_code == 200, before.text
    assert "d0-smell" not in {row["id"] for row in before.json()["protocols"]}
    created = _install_description(client)
    assert created.status_code == 201, created.text
    assert created.headers["ETag"] and list(overlay.iterdir()) == []
    stored = client.get("/admin/api/engine-protocols/d0-smell")
    assert stored.status_code == 200 and stored.headers["ETag"] == created.headers["ETag"]
    assert stored.json()["text"] == SMELL.read_text(encoding="utf-8")
    after = client.get("/admin/api/engine-protocols")
    row = {r["id"]: r for r in after.json()["protocols"]}["d0-smell"]
    assert row["source"] == "user" and row["dispatch_supported"] is True
    assert row["operations"] == ["smell.infer"]
    assert after.json()["problems"] == []


def test_step2_the_owner_adds_an_engine_record_that_names_the_description(owner):
    client, overlay, *_ = owner
    assert _install_description(client).status_code == 201
    created = _add_engine(client)
    assert created.status_code == 201, created.text
    record = created.json()["service_registry"]["services"]["smell-box"]
    assert record["enabled"] is False
    assert record["config"]["connection"] == {
        "dispatch": "protocol", "protocol": "d0-smell", "address": f"http://{HOST}", "transport": "http"}
    # Only a loopback address is accepted for a protocol engine (service_registry.validate_protocol_dispatch_local_only);
    # a LAN host name is refused, so the fake sits on a loopback address.
    assert record["capabilities"] == []


def test_step3_the_owner_declares_the_smell_task_and_turns_the_engine_on(owner, gateway):
    client, overlay, *_ = owner
    assert _install_description(client).status_code == 201
    created = _add_engine(client, capabilities=["smell.infer"], models=[{"id": "smell-1", "public_name": "smell-1"}])
    assert created.status_code == 201, created.text
    record = created.json()["service_registry"]["services"]["smell-box"]
    # A described operation is its own record capability; the model row carries the upstream name dispatch resolves.
    assert record["capabilities"] == ["smell.infer"]
    assert [row["upstream_model"] for row in record["config"]["models"]] == ["smell-1"]
    enabled = client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    from gateway import admin_store
    from gateway.engine_protocol import dispatch

    registry = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]
    assert registry["services"]["smell-box"]["enabled"] is True
    target = dispatch.resolve(registry, "smell-1", "smell.infer")
    assert target is not None and target.engine_id == "smell-box"
    assert {row["task"] for row in target.offering["tasks"]} == {"smell.infer"}


def _app_key(client, owner_id, plan_id) -> str:
    app = client.post("/admin/api/apps", headers=csrf_for(client),
                      json={"name": "smell-app", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert app.status_code == 201, app.text
    key = client.post(f"/admin/api/apps/{app.json()['app']['id']}/keys", headers=csrf_for(client), json={"display_name": "k"})
    assert key.status_code == 201, key.text
    return key.json()["credential"]["api_key"]


@pytest.mark.xfail(strict=True, reason=(
    "Step 4 breaks even though step 3 now stores the record: the public job route admits only the intents of a closed table "
    "(registry/media_job_intents.yml:10-16, read by gateway/media_job_intents.py; refusal at proxy.py:25940), and the "
    "protocol dispatch callers pass a fixed operation (proxy.py:26073 'images', proxy.py:33788 intents for "
    "chat/embeddings/images/speech). The engine layer itself accepts smell.infer once the record holds a model row and "
    "the task (dispatch.resolve returned a Target for it in a probe). The generic workflow run route needs a "
    "description that names a binding (proxy.py:26829-26843), and an http-v1 description cannot serve a record by "
    "capability (description.py:392)."))
def test_step4_a_public_job_runs_on_the_engine_and_returns_the_molecule(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    # Step 3 now writes the record: the task and the model row come from the owner route.
    assert _add_engine(client, capabilities=["smell.infer"], models=[{"id": "smell-1", "public_name": "smell-1"}]).status_code == 201
    assert client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True}).status_code == 200
    key = _app_key(client, owner_id, plan_id)
    public = gateway.client()
    submitted = public.post("/gateway/v1/media/jobs", headers={"authorization": f"Bearer {key}"},
                            json={"intent": "smell.infer", "model": "smell-1", "input": {"vector": [0.25, -1.5, 3.0, 0.0]}})
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    for _ in range(100):
        polled = public.get(f"/gateway/v1/media/jobs/{job['id']}", headers={"authorization": f"Bearer {key}"}).json()
        if polled["status"] in ("succeeded", "failed"):
            break
    assert polled["status"] == "succeeded"
    assert smell_engine.posts == [{"input": {"vector": [0.25, -1.5, 3.0, 0.0]}}]
