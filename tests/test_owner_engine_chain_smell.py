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


def _install_description(overlay: Path) -> None:
    (overlay / "d0-smell.yml").write_text(SMELL.read_text(encoding="utf-8"), encoding="utf-8")


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _add_engine(client, **extra):
    """The call the web UI's add-engine dialog makes for a protocol engine (web/src/app/engines/add-engine)."""
    return client.post("/admin/api/operator/services", headers=_write_headers(client), json={
        "definition": "openai-compatible", "name": "smell-box", "disabled": True,
        "values": {"address": f"http://{HOST}", "dispatch": "protocol", "protocol_description": "d0-smell"}, **extra})


def test_step1_the_owner_installs_the_description_in_the_overlay_directory(owner):
    # GAP (not a break): no route writes a description. The owner path today is a file in the overlay directory
    # (description.user_directory(), gateway/engine_protocol/description.py:689); the engine editor only lists it.
    client, overlay, *_ = owner
    before = client.get("/admin/api/engine-protocols")
    assert before.status_code == 200, before.text
    assert "d0-smell" not in {row["id"] for row in before.json()["protocols"]}
    _install_description(overlay)
    after = client.get("/admin/api/engine-protocols")
    row = {r["id"]: r for r in after.json()["protocols"]}["d0-smell"]
    assert row["source"] == "user" and row["dispatch_supported"] is True
    assert row["operations"] == ["smell.infer"]
    assert after.json()["problems"] == []


def test_step2_the_owner_adds_an_engine_record_that_names_the_description(owner):
    client, overlay, *_ = owner
    _install_description(overlay)
    created = _add_engine(client)
    assert created.status_code == 201, created.text
    record = created.json()["service_registry"]["services"]["smell-box"]
    assert record["enabled"] is False
    assert record["config"]["connection"] == {
        "dispatch": "protocol", "protocol": "d0-smell", "address": f"http://{HOST}", "transport": "http"}
    # Only a loopback address is accepted for a protocol engine (service_registry.validate_protocol_dispatch_local_only);
    # a LAN host name is refused, so the fake sits on a loopback address.
    assert record["capabilities"] == []


@pytest.mark.xfail(strict=True, reason=(
    "Step 3 breaks: the owner cannot declare the smell.infer task on the engine. The add-engine route accepts only the "
    "capabilities of the engine definition (gateway/engine_admin.py:511 raises 'does not offer the work' for any other id) "
    "and registry/engine_definitions.json:4-26 offers only chat and embeddings. With no declared task the stored engine "
    "has no offering (service_registry.py:902-918), so dispatch.resolve (engine_protocol/dispatch.py:44-84) never finds it."))
def test_step3_the_owner_declares_the_smell_task_and_turns_the_engine_on(owner):
    client, overlay, *_ = owner
    _install_description(overlay)
    created = _add_engine(client, capabilities=["smell.infer"], models=[{"id": "smell-1", "public_name": "smell-1"}])
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text


def _app_key(client, owner_id, plan_id) -> str:
    app = client.post("/admin/api/apps", headers=csrf_for(client),
                      json={"name": "smell-app", "kind": "service", "access_plan": plan_id, "owner_user_id": owner_id})
    assert app.status_code == 201, app.text
    key = client.post(f"/admin/api/apps/{app.json()['app']['id']}/keys", headers=csrf_for(client), json={"display_name": "k"})
    assert key.status_code == 201, key.text
    return key.json()["credential"]["api_key"]


@pytest.mark.xfail(strict=True, reason=(
    "Step 4 breaks even when step 3 is seeded: the public job route admits only the intents of a closed table "
    "(registry/media_job_intents.yml:10-16, read by gateway/media_job_intents.py; refusal at proxy.py:25940), and the "
    "protocol dispatch callers pass a fixed operation (proxy.py:26073 'images', proxy.py:33788 intents for "
    "chat/embeddings/images/speech). The engine layer itself accepts smell.infer once the record holds a model row and "
    "the task (dispatch.resolve returned a Target for it in a probe). The generic workflow run route needs a "
    "description that names a binding (proxy.py:26829-26843), and an http-v1 description cannot serve a record by "
    "capability (description.py:392)."))
def test_step4_a_public_job_runs_on_the_engine_and_returns_the_molecule(owner, smell_engine, gateway):
    from gateway import admin_store

    client, overlay, owner_id, plan_id = owner
    _install_description(overlay)
    assert _add_engine(client).status_code == 201
    # Stand-in for step 3, which the owner route cannot do: write the record the route would have stored.
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    registry = record["value"]
    service = registry["services"]["smell-box"]
    service["enabled"] = True
    service["capabilities"] = ["smell.infer"]
    service["config"]["capabilities"] = ["smell.infer"]
    service["config"]["models"] = [{"id": "smell-1", "upstream_model": "smell-1"}]
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=record["operator_settings_generation"], updated_by="test", audit_action="test",
        audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None, audit_target_kind=None,
        audit_target_id=None, path=gateway.path)
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
