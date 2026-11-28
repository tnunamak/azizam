"""Owner start, stop and restart of an engine: POST /admin/api/engines/{id}/lifecycle.

The route runs over the lifecycle driver the request path would use. Most tests mount the real
route on a small app with a stand-in sign-in and drive a real ProcessDriver around a stub HTTP
server, so a "stopped" answer is checked against the port itself. The last tests go through the
whole gateway to prove the wiring in the proxy and the real sign-in roles.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, engine_admin, lifecycle_drivers
from gateway.admin_api import AdminContext
from gateway.engine_lifecycle import EngineLifecycle
from gateway.engine_process import ProcessDriver
from gateway.swap_drain import SwapDrain
from tests.test_engine_contract_runtime import free_port, process_service
from tests.support.app_keys import issue_app_credential
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

URL = "/admin/api/engines/{}/lifecycle"
EXTERNAL_WHY = "Azizam does not own this server; stop it where it runs."


def answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def sign_in_stand_in(request):
    role = request.headers.get("x-role")
    if role == "owner":
        return AdminContext(subject="owner-1", roles=frozenset({"owner", "admin"}), actor_id="owner-1")
    if role == "admin":
        return AdminContext(subject="admin-1", roles=frozenset({"admin"}), actor_id="admin-1")
    return JSONResponse({"error": {"code": "admin_forbidden"}}, status_code=403)


class Rig:
    """A process engine ("fixture") and an external engine ("remote") behind the real route."""

    def __init__(self, tmp_path: Path, monkeypatch, *, verify_s=3.0):
        self.db = tmp_path / "admin.db"
        admin_store.init_db(self.db)
        self.allowed = [True]
        self.svc = process_service(tmp_path)
        self.svc["residency"] = {"idle_owner": "engine"}  # no gateway idle timer in these tests
        self.port = self.svc["process"]["port"]["fixed"]
        self.state_file = tmp_path / "state" / "fixture.json"
        # The external engine is a server somebody else runs: a listener the test owns.
        self.external = socket.socket()
        self.external.bind(("127.0.0.1", 0))
        self.external.listen()
        self.external_port = self.external.getsockname()[1]
        self.backends = {
            "fixture": self.svc,
            "remote": {"id": "remote", "connection": {"address": f"http://127.0.0.1:{self.external_port}"}},
        }
        self.driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: self.allowed[0])
        monkeypatch.setattr(lifecycle_drivers, "_process_driver", self.driver)
        self.drain = SwapDrain()
        lifecycle = EngineLifecycle(backends=lambda: self.backends, in_flight=self.drain.in_flight,
                                    commands_allowed=lambda: self.allowed[0], verify_s=verify_s)
        registry = {"services": {name: {} for name in self.backends}}
        service = SimpleNamespace(store=SimpleNamespace(path=self.db, get_service_registry=lambda: registry),
                                  db_path=self.db)
        router = APIRouter(prefix="/admin/api")
        engine_admin.register_engine_routes(router, service=service, guard=sign_in_stand_in,
                                            read_json=lambda request: request.json(), lifecycle=lifecycle)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def post(self, service_id, action, role="owner"):
        return self.client.post(URL.format(service_id), json={"action": action}, headers={"x-role": role})

    def pid(self):
        return json.loads(self.state_file.read_text())["pid"]

    def audit(self):
        return admin_store.list_audit(path=self.db, action_prefix="engine.lifecycle.")

    def close(self):
        self.client.close()
        self.driver.stop(self.svc)
        self.external.close()


def make_rig(tmp_path, monkeypatch, **kwargs):
    rig = Rig(tmp_path, monkeypatch, **kwargs)
    return rig


@pytest.fixture
def rig(tmp_path, monkeypatch):
    made = make_rig(tmp_path, monkeypatch)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def quick_rig(tmp_path, monkeypatch):
    """A rig whose stop check gives up fast, for the stop that does not take effect."""
    made = make_rig(tmp_path, monkeypatch, verify_s=0.3)
    try:
        yield made
    finally:
        made.close()


def started(rig):
    answer = rig.post("fixture", "start")
    assert answer.status_code == 200, answer.text
    assert answers(rig.port)
    return answer


def test_an_external_engine_is_not_managed_for_every_action(rig):
    for action in ("start", "stop", "restart"):
        answer = rig.post("remote", action)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"service_id": "remote", "action": action, "result": "not_managed",
                                 "state": "unmanaged", "why": EXTERNAL_WHY}
    assert answers(rig.external_port)  # the server somebody else runs was not touched


def test_stop_answers_stopped_only_after_the_port_refuses(rig):
    started(rig)
    answer = rig.post("fixture", "stop")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["service_id"], body["action"], body["result"], body["state"]) == ("fixture", "stop", "done", "stopped")
    assert not answers(rig.port)  # the answer came after the port closed, not before


def test_start_brings_a_stopped_engine_back_to_running(rig):
    started(rig)
    assert rig.post("fixture", "stop").json()["result"] == "done"
    assert not answers(rig.port)
    body = rig.post("fixture", "start").json()
    assert (body["result"], body["state"]) == ("done", "running")
    assert answers(rig.port)


def test_restart_replaces_the_process_and_answers_running(rig):
    started(rig)
    before = rig.pid()
    body = rig.post("fixture", "restart").json()
    assert (body["action"], body["result"], body["state"]) == ("restart", "done", "running")
    assert answers(rig.port)
    assert rig.pid() != before


def test_a_stop_whose_port_keeps_answering_is_unverified(quick_rig):
    rig = quick_rig
    started(rig)

    async def stop_that_does_nothing(_svc):
        return None

    rig.driver.stop_async = stop_that_does_nothing
    answer = rig.post("fixture", "stop")
    body = answer.json()
    assert (body["result"], body["state"]) == ("unverified", "unknown"), answer.text
    assert body["why"]
    assert answers(rig.port)  # never reported green on missing evidence


def test_a_running_execution_makes_stop_and_restart_busy_and_stops_nothing(rig):
    started(rig)
    pid = rig.pid()
    lease = rig.drain.lease("fixture")
    for action in ("stop", "restart"):
        answer = rig.post("fixture", action)
        assert answer.status_code == 409, answer.text
        body = answer.json()
        assert (body["result"], body["state"]) == ("busy", "running")
        assert "1 request" in body["why"]
    assert answers(rig.port) and rig.pid() == pid
    lease.release()
    assert rig.post("fixture", "stop").json()["result"] == "done"


def test_a_stop_waits_out_a_held_residency_lock_and_holds_it_for_the_whole_stop(rig, monkeypatch):
    """A request mints its lease inside a hold of the domain lock, so the stop holds that lock from its busy check to
    its end: a request that arrives after the check queues behind the stop instead of being cut off by it."""
    from gateway import engine_lifecycle

    started(rig)
    monkeypatch.setattr(engine_lifecycle, "_LOCK_WAIT_S", 0.2)

    async def scenario():
        lock = asyncio.Lock()
        lifecycle = EngineLifecycle(backends=lambda: rig.backends, in_flight=rig.drain.in_flight,
                                    lock_for=lambda _service_id: lock, commands_allowed=lambda: True, verify_s=3.0)
        await lock.acquire()  # a request making its target ready
        held = await lifecycle.apply("fixture", "stop")
        lock.release()
        seen_locked: list[bool] = []

        async def watch():
            while not stop.done():
                seen_locked.append(lock.locked())
                await asyncio.sleep(0.01)

        stop = asyncio.ensure_future(lifecycle.apply("fixture", "stop"))
        await watch()
        return held, await stop, seen_locked, lock.locked()

    held, stopped, seen_locked, locked_after = asyncio.run(scenario())
    assert (held.result, held.state) == ("busy", "running")
    assert (stopped.result, stopped.state) == ("done", "stopped")
    # Samples before the stop task first runs may see the lock free; from the first hold to the end it stays held.
    assert True in seen_locked and all(seen_locked[seen_locked.index(True):]) and not locked_after
    assert not answers(rig.port)


def test_with_host_commands_off_start_and_restart_are_refused_and_start_nothing(rig):
    rig.allowed[0] = False
    answer = rig.post("fixture", "start")
    assert answer.status_code == 409, answer.text
    body = answer.json()
    assert body["result"] == "refused" and "Allow Azizam to run commands" in body["why"]
    assert not answers(rig.port)
    assert not rig.state_file.exists()
    # A restart is refused before it stops anything: a running engine is not left down.
    rig.allowed[0] = True
    started(rig)
    pid = rig.pid()
    rig.allowed[0] = False
    assert rig.post("fixture", "restart").status_code == 409
    assert answers(rig.port) and rig.pid() == pid
    # Off blocks new starts only; stopping a running engine is still allowed.
    assert rig.post("fixture", "stop").json()["result"] == "done"


def test_only_the_owner_may_act_and_a_refused_caller_changes_nothing(rig):
    started(rig)
    pid = rig.pid()
    audited = len(rig.audit())
    admin = rig.post("fixture", "stop", role="admin")
    assert admin.status_code == 403 and admin.json()["error"]["code"] == "owner_required"
    member = rig.post("fixture", "stop", role="member")
    assert member.status_code == 403
    assert answers(rig.port) and rig.pid() == pid
    assert len(rig.audit()) == audited  # a refused caller leaves no lifecycle event


def test_unknown_service_and_bad_action_use_the_engine_error_shape(rig):
    missing = rig.post("nope", "stop")
    assert missing.status_code == 404
    assert missing.json()["error"] == {"code": "service_not_found", "message": "No engine called 'nope'."}
    bad = rig.post("fixture", "reboot")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_action" and bad.json()["error"]["field"] == "action"


def test_each_answer_is_audited_with_the_result_and_no_secret(rig):
    started(rig)
    rig.post("fixture", "stop")
    rig.post("remote", "stop")
    events = rig.audit()
    seen = {(e["action"], e["target_id"], e["metadata"]["result"]) for e in events}
    assert len(events) == 3 and seen == {("engine.lifecycle.start", "fixture", "done"),
                                         ("engine.lifecycle.stop", "fixture", "done"),
                                         ("engine.lifecycle.stop", "remote", "not_managed")}
    assert all(e["actor_id"] == "owner-1" and e["target_kind"] == "service" for e in events)


def test_the_gateway_wires_the_route_to_its_own_backends_and_roles(gateway):
    import proxy

    owner, _owner_id, _plan, admin = owner_and_admin(gateway)
    # An engine the registry does not know is a 404 for the owner, whatever the backends hold.
    missing = owner.post(URL.format("no-such-engine"), headers=csrf_for(owner), json={"action": "stop"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "service_not_found"
    # An administrator who is not the owner is refused with the owner rule.
    refused = admin.post(URL.format("no-such-engine"), headers=csrf_for(admin), json={"action": "stop"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "owner_required"
    # A member's app key is not an admin credential: it never reaches the owner rule.
    member_id = admin_store.upsert_oidc_user("https://id.example", "m", email="m@example.test", path=gateway.path)["id"]
    app_id = owner.post("/admin/api/apps", headers=csrf_for(owner), json={
        "name": "members", "kind": "service", "access_plan": _plan, "owner_user_id": member_id}).json()["app"]["id"]
    key = issue_app_credential(gateway.path, app_id, label="k")
    keyed = gateway.client().post(URL.format("no-such-engine"), json={"action": "stop"},
                                  headers={"Authorization": f"Bearer {key['api_key']}"})
    assert keyed.status_code == 403, keyed.text
    # The proxy hands the route its live backends and the serving-lease count.
    assert proxy.ADMIN_ENGINE_LIFECYCLE is not None
    assert proxy.ADMIN_ENGINE_LIFECYCLE.in_flight("no-such-engine") == 0
