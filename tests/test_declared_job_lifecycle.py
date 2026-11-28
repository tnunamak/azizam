"""A declared-operation job runs the same selection and serving-lease lifecycle as a synchronous protocol call.

The owner chain (tests/test_owner_engine_chain_smell.py) proves the job runs for an engine with request-field selection.
These tests use an engine whose offerings select by switch: the lease is held for the whole run and released on success,
failure and cancellation; the requested model is the one selected before the job is submitted; and a caller whose plan
may not load models gets no switch.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import proxy
from gateway import media_jobs, receipt_store
from gateway.engine_protocol import description, executor, transport
from tests.support.access import authenticated, everything, identity
from tests.support.fake_upstream import HandlerTransport

ENGINE = "job-box"
DESCRIPTION = """
protocol_version: 1
id: lifecycle
title: Declared job lifecycle fixture
executor: http-v1
operations:
  choose:
    class: control
    request: {method: POST, path: /selected, body: {encoding: json, base: passthrough}}
    response: {framing: none}
  selected:
    class: control
    request: {method: GET, path: /selected}
    response: {framing: json, select: {value: /active}}
  smell.infer:
    class: generate
    job:
      submit:
        request:
          method: POST
          path: /v1/infer
          body:
            encoding: json
            base: empty
            map:
              - copy: {from: /body/vector, to: /input/vector}
        id: /job/id
      poll:
        request: {method: GET, path: "/v1/infer/{id}"}
        status:
          path: /state
          states: {queued: pending, working: running, ready: succeeded, failed: failed}
        interval_s: 0.5
        max_wait_s: 5
      fetch:
        request: {method: GET, path: "/v1/infer/{id}/molecule"}
"""
MOLECULE = b'{"smiles": "CCO"}'


class Engine:
    """The fake upstream: the selected model, the model each submit saw, the lease count at each step, and its poll answer."""

    def __init__(self):
        self.requests = []
        self.active = "a"
        self.submitted_with = []
        self.in_flight = []
        self.poll_state = "ready"

    def __call__(self, request):
        self.requests.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path == "/selected":
            if request.method == "POST":
                self.active = json.loads(request.content)["model"]
                return httpx.Response(204)
            return httpx.Response(200, json={"active": self.active})
        if path == "/v1/infer":
            self.submitted_with.append(self.active)
            self.in_flight.append(proxy._swap_drain.in_flight(ENGINE))
            return httpx.Response(200, json={"job": {"id": "j1"}})
        if path == "/v1/infer/j1":
            return httpx.Response(200, json={"state": self.poll_state})
        if path == "/v1/infer/j1/molecule":
            return httpx.Response(200, content=MOLECULE, headers={"content-type": "application/octet-stream"})
        raise AssertionError((request.method, path))


def _offering(name, upstream):
    return {"id": name, "kind": "model", "upstream": upstream,
            "tasks": [{"task": "smell.infer", "evidence": "declared"}],
            "selection": {"mode": "switch", "switch": {
                "call": "choose", "args": {"model": upstream}, "observe_selected": "selected",
                "exclusive_scope": "engine", "drain_timeout_s": 0.5, "queue_wait_s": 0.5, "on_failure": "restore"}}}


@pytest.fixture
def world(monkeypatch, tmp_path):
    fake = Engine()
    record = {"id": ENGINE, "name": "Job box", "enabled": True, "capabilities": ["smell.infer"], "config": {
        "connection": {"transport": "http", "dispatch": "protocol", "protocol": "lifecycle", "address": "http://localhost:9100",
                       "credentials": {}, "timeouts": {"connect_s": 2, "first_byte_s": 10, "total_s": 30}},
        "capabilities": ["smell.infer"], "observations": {"selected": {"op": "selected"}},
        "offerings": [_offering("model-a", "a"), _offering("model-b", "b")],
        "models": [{"id": "a", "public_name": "model-a", "upstream_model": "a"},
                   {"id": "b", "public_name": "model-b", "upstream_model": "b"}]}}
    registry = {"schema_version": 2, "services": {ENGINE: record}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    shipped = tmp_path / "protocols"
    shutil.copytree(description.REGISTRY_DIR, shipped)
    (shipped / "lifecycle.yml").write_text(DESCRIPTION)
    monkeypatch.setattr(description, "REGISTRY_DIR", shipped)
    original = description.load_registry
    monkeypatch.setattr(description, "load_registry", lambda directory=shipped: original(directory))
    wire = transport.HttpxTransport.for_tests(HandlerTransport(fake))
    monkeypatch.setattr(executor, "HttpxTransport", lambda: wire)
    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "assets")
    monkeypatch.setattr(media_jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(receipt_store, "DB_PATH", tmp_path / "receipts.db")
    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db(path=receipt_store.DB_PATH)
    return SimpleNamespace(fake=fake, registry=registry)


def _plan(**overrides):
    return identity(replace(everything(), all_work_types=True, **overrides))


def _run(model="model-b", *, caller=None, cancel_after_submit=False):
    """Submit one declared job in one event loop and wait for its task; the job's final row."""
    async def go():
        transport_ = httpx.ASGITransport(app=proxy.app)
        async with httpx.AsyncClient(transport=transport_, base_url="http://test") as client:
            response = await client.post("/gateway/v1/media/jobs", json={
                "intent": "smell.infer", "model": model, "input": {"vector": [1.0]}}, headers={"authorization": "Bearer k"})
            if response.status_code != 202:
                return response, None
            tasks = list(proxy._declared_operation_job_tasks)
            if cancel_after_submit:
                for _ in range(200):
                    if world_fake.submitted_with:
                        break
                    await asyncio.sleep(0.01)
                for task in tasks:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return response, media_jobs.get(response.json()["id"], path=media_jobs.DB_PATH)
    with authenticated(caller or _plan()):
        return asyncio.run(go())


world_fake: Engine


@pytest.fixture(autouse=True)
def _expose_fake(world):
    global world_fake
    world_fake = world.fake


def test_switch_mode_selects_the_requested_model_before_the_job_is_submitted(world):
    response, job = _run("model-b")
    assert response.status_code == 202, response.text
    assert job["status"] == "succeeded", job
    assert world.fake.submitted_with == ["b"]            # the job saw model b, which was not selected at the start
    first_submit = [r for r in world.fake.requests if r[1] == "/v1/infer"]
    assert first_submit and world.fake.requests.index(first_submit[0]) > world.fake.requests.index(("POST", "/selected", b'{"model":"b"}'))


def test_the_lease_is_held_while_the_job_runs_and_released_on_success(world):
    _, job = _run("model-b")
    assert job["status"] == "succeeded"
    assert world.fake.in_flight == [1]                   # held when the engine got the submit
    assert proxy._swap_drain.in_flight(ENGINE) == 0


def test_the_lease_is_released_when_the_job_fails(world):
    world.fake.poll_state = "failed"
    _, job = _run("model-b")
    assert job["status"] == "failed"
    assert world.fake.in_flight == [1]
    assert proxy._swap_drain.in_flight(ENGINE) == 0


def test_the_lease_is_released_when_the_job_is_canceled(world):
    world.fake.poll_state = "working"
    _, job = _run("model-b", cancel_after_submit=True)
    assert job["status"] == "canceled"
    assert world.fake.in_flight == [1]
    assert proxy._swap_drain.in_flight(ENGINE) == 0


def test_a_caller_who_may_not_load_models_gets_no_switch(world):
    _, job = _run("model-b", caller=_plan(can_load_models=False))
    assert job["status"] == "failed"
    assert world.fake.submitted_with == []                # never reached the engine's job route
    assert not [r for r in world.fake.requests if r[0] == "POST"]
    assert world.fake.active == "a"
    assert proxy._swap_drain.in_flight(ENGINE) == 0


def test_a_caller_who_may_not_load_models_still_runs_on_the_model_already_selected(world):
    _, job = _run("model-a", caller=_plan(can_load_models=False))
    assert job["status"] == "succeeded", job
    assert world.fake.submitted_with == ["a"]
