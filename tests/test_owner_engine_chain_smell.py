"""The owner chain for a new kind of engine, driven through the real routes (an evidence experiment).

The direct test (tests/test_smell_infer_offering.py) builds the connection and the adapter by hand. This module starts
where an owner starts: install the d0-smell description, add an engine record that names it, list it in the catalog,
then submit through a public route. Each step is its own test, so the first step that cannot work today is the one
marked as an expected failure, with the missing piece named in its reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
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


class EmptyModelCatalog:
    """The LLM substrate's model list, empty: the gateway asks it for one when an engine is enabled."""

    async def __call__(self, scope, receive, send):
        body = b'{"object": "list", "data": []}'
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


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


def test_step4_a_public_job_runs_on_the_engine_and_returns_the_molecule(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    # Step 3 now writes the record: the task and the model row come from the owner route.
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:  # one event loop for the whole exchange, so the background job outlives the submit
        _submit_and_read(public, key, smell_engine)


def test_step6_the_job_leaves_a_receipt_and_shows_in_the_owner_activity(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        job = _submit_and_read(public, key, smell_engine)
    records = client.get("/admin/api/activity/requests")
    assert records.status_code == 200, records.text
    receipts = [r["receipt"] for r in records.json()["records"] if r["receipt"].get("job_id") == job["id"]]
    assert len(receipts) == 1, records.json()["records"]
    receipt = receipts[0]
    assert receipt["status"] == "succeeded" and receipt["backend"] == "smell-box" and receipt["intent"] == "smell.infer"
    assert receipt["path"] == "/gateway/v1/media/jobs"
    assert "CCO" not in json.dumps(records.json())  # the receipt names the work, not its input or its result


def _heard(process) -> dict:
    """The next message of the second gateway; its own boot lines carry no prefix."""
    for line in process.stdout:
        if line.startswith("SECOND "):
            return json.loads(line.removeprefix("SECOND "))
    raise AssertionError("the second gateway ended without answering")


def test_step7_a_second_gateway_runs_the_job_for_an_engine_the_first_enabled_without_a_restart(owner, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    # The second gateway is a separate process over the same admin database (as a second worker would be). It starts
    # before the engine exists, so nothing in its own configuration can know the engine.
    second = subprocess.Popen(
        [sys.executable, "-m", "tests.support.second_gateway_smell", str(gateway.path)], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        ready = _heard(second)
        assert ready == {"event": "ready", "knows_engine": False}
        _enabled_engine(client)  # the first gateway enables it; the second is not told
        key = _app_key(client, owner_id, plan_id)
        second.stdin.write(json.dumps({"key": key}) + "\n")
        second.stdin.flush()
        done = _heard(second)
    finally:
        second.stdin.close()  # a second gateway still waiting for its key ends on this
        second.wait(timeout=30)
    assert done["status"] == "succeeded" and done["content"] == MOLECULE.decode()
    assert done["posts"] == [{"input": {"vector": [0.25, -1.5, 3.0, 0.0]}}]
    # Its receipt is in the owner's Activity, which the first gateway reads from the shared database.
    records = client.get("/admin/api/activity/requests")
    assert records.status_code == 200, records.text
    receipts = [r["receipt"] for r in records.json()["records"] if r["receipt"].get("job_id") == done["job_id"]]
    assert [r["status"] for r in receipts] == ["succeeded"]


def test_step8_a_declared_job_left_queued_or_running_by_a_restart_ends_failed_with_its_receipt_closed(gateway):
    """The startup recovery (proxy._recover_media_jobs) cannot resume a declared job: its task died with the gateway, and
    the input it ran on is not kept for a second run. It fails the job with the restart reason and closes the receipt."""
    import proxy
    from gateway import declared_operation_job, media_jobs, receipt_store
    from gateway.receipts import RouteReceipt

    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db()
    left = {}
    for status in (media_jobs.STATUS_QUEUED, media_jobs.STATUS_RUNNING):
        job = media_jobs.create(media_jobs.MediaJob(
            intent="smell.infer", backend="smell-box", status=status, request_id=f"decljob_restart_{status}",
            metadata={"mode": declared_operation_job.MODE, "model": "smell-1"}), input_data={"vector": [1.0]}, path=media_jobs.DB_PATH)
        receipt_store.begin(RouteReceipt(protocol="gateway.media", intent="smell.infer", path="/gateway/v1/media/jobs",
                                         request_id=job.request_id, job_id=job.job_id, backend="smell-box"))
        left[status] = job
    proxy._recover_media_jobs()
    for status, job in left.items():
        settled = media_jobs.get(job.job_id, path=media_jobs.DB_PATH)
        assert settled["status"] == "failed", status
        assert "gateway restarted" in settled["error"], status
        assert not media_jobs.holds_resources(settled["status"])
        receipt = receipt_store.get(job.request_id, audience="admin")
        assert receipt["status"] == "failed", status


def _submit_and_read(public, key, smell_engine) -> dict:
    submitted = public.post("/gateway/v1/media/jobs", headers={"authorization": f"Bearer {key}"},
                            json={"intent": "smell.infer", "model": "smell-1", "input": {"vector": [0.25, -1.5, 3.0, 0.0]}})
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    for _ in range(100):
        polled = public.get(f"/gateway/v1/media/jobs/{job['id']}", headers={"authorization": f"Bearer {key}"}).json()
        if polled["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert polled["status"] == "succeeded"
    assert smell_engine.posts == [{"input": {"vector": [0.25, -1.5, 3.0, 0.0]}}]
    content = public.get(job["content_url"], headers={"authorization": f"Bearer {key}"})
    assert content.status_code == 200, content.text
    assert content.content == MOLECULE
    return job


def _enabled_engine(client):
    assert _add_engine(client, capabilities=["smell.infer"], models=[{"id": "smell-1", "public_name": "smell-1"}]).status_code == 201
    assert client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True}).status_code == 200


def _submit(gateway, key):
    return gateway.client().post("/gateway/v1/media/jobs", headers={"authorization": f"Bearer {key}"},
                                 json={"intent": "smell.infer", "model": "smell-1", "input": {"vector": [0.25, -1.5, 3.0, 0.0]}})


def _jobs_made() -> int:
    from gateway import media_jobs
    return len([job for job in media_jobs.recent(path=media_jobs.DB_PATH, audience="internal") if job["intent"] == "smell.infer"])


def test_step4_an_engine_in_maintenance_refuses_a_declared_job_before_any_upstream_request(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    put = client.put("/admin/api/operator/service-availability", headers=csrf_for(client),
                     json={"overrides": {"smell-box": {"state": "maintenance", "reason": "swap"}}})
    assert put.status_code == 200, put.text
    before = _jobs_made()
    refused = _submit(gateway, key)
    assert refused.status_code in {429, 503}, refused.text
    assert smell_engine.posts == [] and _jobs_made() == before
    # Clearing the override lets the same call through: the refusal was the owner's control, not the setup.
    assert client.put("/admin/api/operator/service-availability", headers=csrf_for(client),
                      json={"overrides": {"smell-box": {"state": "available"}}}).status_code == 200
    with gateway.client() as public:
        _submit_and_read(public, key, smell_engine)


def test_step4_a_disabled_intent_refuses_a_declared_job_before_any_upstream_request(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    from gateway import admin_store
    revision = admin_store.read_config_section("resource_admission", path=gateway.path)["revision"]
    put = client.put("/admin/api/operator/media-job-admission", headers={**csrf_for(client), "If-Match": revision}, json={"disabled_intents": ["smell.infer"]})
    assert put.status_code == 200, put.text
    before = _jobs_made()
    refused = _submit(gateway, key)
    assert refused.status_code in {429, 503}, refused.text
    assert smell_engine.posts == [] and _jobs_made() == before


def test_step4_an_intent_no_engine_declares_keeps_the_unsupported_refusal(owner, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    headers = {"authorization": f"Bearer {key}"}
    for body in ({"intent": "smell.other", "model": "smell-1", "input": {}},
                 {"intent": "smell.infer", "model": "unknown-model", "input": {}}):
        refused = gateway.client().post("/gateway/v1/media/jobs", headers=headers, json=body)
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "unsupported_media_job_intent"
        # The refusal lists what the gateway accepts now, and that includes the operation the enabled engine declares.
        assert refused.json()["error"]["supported_intents"][-1] == "smell.infer"


def test_step4_the_input_must_be_a_json_object(owner, smell_engine, gateway):
    client, overlay, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    _enabled_engine(client)
    key = _app_key(client, owner_id, plan_id)
    refused = gateway.client().post("/gateway/v1/media/jobs", headers={"authorization": f"Bearer {key}"},
                                    json={"intent": "smell.infer", "model": "smell-1", "input": [1, 2]})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_media_job_input"
    assert smell_engine.posts == []


def _public_documents(public, key) -> tuple[bytes, bytes, dict]:
    """The public models listing, the OpenAPI document and a refusal's supported intents, as the caller sees them."""
    headers = {"authorization": f"Bearer {key}"}
    models = public.get("/v1/models", headers=headers)
    assert models.status_code == 200, models.text
    # The loaded-model row stamps `created` with the time of the request; every other byte is the listing's.
    listing = json.loads(models.content)
    for row in listing["data"]:
        row.pop("created", None)
    openapi = public.get("/openapi.json", headers=headers)
    assert openapi.status_code == 200, openapi.text
    refused = public.post("/gateway/v1/media/jobs", headers=headers, json={"intent": "smell.other", "model": "x", "input": {}})
    assert refused.status_code == 400, refused.text
    return json.dumps(listing, sort_keys=True).encode("utf-8"), openapi.content, refused.json()["error"]["supported_intents"]


def test_step5_an_installed_engine_makes_its_operation_public_and_no_engine_changes_nothing(owner, gateway, fake_upstreams):  # noqa: F811
    client, overlay, owner_id, plan_id = owner
    key = _app_key(client, owner_id, plan_id)
    public = gateway.client()  # no lifespan: nothing here runs a background job
    fake_upstreams.router.add("127.0.0.1:4011", EmptyModelCatalog())
    none_models, none_openapi, none_intents = _public_documents(public, key)
    assert _install_description(client).status_code == 201
    # A description alone adds an engine to nothing: the documents are the bytes they were.
    assert _public_documents(public, key) == (none_models, none_openapi, none_intents)
    # An engine record that is off adds no operation either. (Its mere presence moves some availability fields and
    # alias tables, as it always did; that state is the baseline for turning the engine on and off again.)
    assert _add_engine(client, capabilities=["smell.infer"], models=[{"id": "smell-1", "public_name": "smell-1"}]).status_code == 201
    off_documents = _public_documents(public, key)
    assert all(b"smell" not in document for document in off_documents[:2]) and "smell.infer" not in off_documents[2]
    assert client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": True}).status_code == 200
    models, openapi, intents = _public_documents(public, key)
    listed = {row["id"]: row for row in json.loads(models)["data"]}
    assert listed["smell-1"]["azizam_operations"] == ["smell.infer"]
    assert "smell.infer" in intents
    submit = json.loads(openapi)["paths"]["/gateway/v1/media/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    declared = [shape for shape in submit["oneOf"] if "input" in shape["properties"]]
    assert [shape["properties"]["intent"]["enum"] for shape in declared] == [["smell.infer"]]
    off = client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={"enabled": False})
    assert off.status_code == 200, off.text
    assert _public_documents(public, key) == off_documents
