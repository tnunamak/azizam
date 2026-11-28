"""An owner adds four kinds of engine from the shipped definitions alone, in one gateway.

The Add engine dialog's own admin routes are the only way in: ``/admin/api/engines/check``, ``/admin/api/engines/test``
and ``/admin/api/operator/services``. The four definitions are ``openai-speech``, ``openai-transcription``, ``sdapi`` and
``workflow-image-engine``. Each runs against the fake upstream its own definition test already uses. No gateway code
names any of these products: tests/test_engine_knowledge_boundary.py keeps it that way.

What each engine proves here:
- speech, transcription and sdapi: a public request reaches the fake and the answer comes back.
- the workflow engine: the check lists which shipped workflows it can run. A first image through this engine is
  proved by tests/test_engine_comfy_definition.py::test_a_first_image_goes_through_the_added_record_to_the_engine
  (it needs a node-graph engine fake); this journey proves the engine is saved and is the graph service while the
  sdapi server serves its own checkpoint.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from types import SimpleNamespace

import proxy
from gateway import admin_store, engine_check, media_jobs, process_template, receipt_store
from gateway.engine_admin import shipped_workflow_manifests
from gateway.engine_protocol import executor, transport
from tests.support.adapter_differential_rig import exchange, scenario
from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_engine_contract_runtime import free_port
from tests.test_engine_definitions_speech import VOICES, _speech_server
from tests.test_engine_definitions_transcription import ASR_ID, ASR_PUBLIC, _clip, _transcription_server
from tests.test_engine_protocol_sdapi import BETA, FakeSdapi, _sdapi_server
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

TOKEN = "t0k3n"


def _node_list_server(fakes, node_classes):
    """A node-graph engine on a real loopback port, from the shared comfy exchanges plus its node list."""
    nodes = {name: {"input": {"required": {}}, "output": ["IMAGE"], "name": name} for name in node_classes}
    not_found = {"status": 404, "json": {"detail": "Not Found"}}
    return fakes.loopback(scenario("engine-probe", "journey-node-graph", [
        exchange("baseline-get", "GET", f"/zz-not-a-route-{TOKEN}", not_found, repeat=True, required=False),
        exchange("baseline-post", "POST", f"/zz-not-a-route-{TOKEN}", not_found, repeat=True, required=False),
        exchange("stats", "GET", "/system_stats", {"json": {"system": {"os": "invented"}, "devices": []}}, repeat=True, required=False),
        exchange("nodes", "GET", "/object_info", {"json": nodes}, repeat=True, required=False)]))


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _add(owner, definition, name, address, check):
    """What the dialog does after a check: save what the check found, ticked as the check ticked it."""
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": definition, "name": name, "values": {"address": address},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]], "models": check["models"]})
    assert created.status_code == 201, created.text


def _check(owner, definition, values, *, outcome="ok"):
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner), json={"definition": definition, "values": values})
    assert checked.status_code == 200, checked.text
    check = checked.json()["check"]
    assert check["outcome"] == outcome, json.dumps(check, default=str)[:3000]
    return check


def test_four_engines_added_without_code(gateway, fake_upstreams, monkeypatch, tmp_path):
    use_fake_engine_dns(monkeypatch)
    monkeypatch.setattr(engine_check, "_baseline_token", lambda: TOKEN)
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    bearer = {"authorization": f"Bearer {key}"}

    # The workflows the release ships that a node-graph engine can run: the fake engine lists exactly their nodes.
    shipped = [m for m in shipped_workflow_manifests() if "comfyui" in (m.get("bindings") or {})]
    assert shipped, "the release ships no workflow for a node-graph engine"
    needed = {n for m in shipped for n in m["bindings"]["comfyui"].get("requires", {}).get("custom_nodes", [])}

    speech = _speech_server(fake_upstreams)
    ear = _transcription_server(fake_upstreams)
    pictures = _sdapi_server(fake_upstreams)
    graph = _node_list_server(fake_upstreams, needed)

    # 1. speech
    check = _check(owner, "openai-speech", {"address": speech.base_url})
    assert [row["id"] for row in check["voices"]] == VOICES
    _add(owner, "openai-speech", "garage-voice", speech.base_url, check)

    # 2. transcription
    check = _check(owner, "openai-transcription", {"address": ear.base_url})
    assert [row["id"] for row in check["models"]] == [ASR_ID]
    _add(owner, "openai-transcription", "garage-ear", ear.base_url, check)

    # 3. sdapi: the owner ticks the one checkpoint
    check = _check(owner, "sdapi", {"address": pictures.base_url})
    picked = [row for row in check["models"] if row["id"] == BETA]
    assert picked
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "sdapi", "name": "garage-pictures", "values": {"address": pictures.base_url},
        "capabilities": ["images"], "models": picked})
    assert created.status_code == 201, created.text

    # 4. the workflow engine: check lists the shipped workflows it can run, then save with the folder and port slots.
    # The fake lists nodes only, no model files, so its capabilities stay unticked (outcome "warning"); the workflow
    # listing is the result this step reads. A workflow needing a model file the engine does not list says so.
    check = _check(owner, "workflow-image-engine", {"address": graph.base_url}, outcome="warning")
    workflows = check["workflows"]
    offered = {row["id"] for row in workflows["offered"]}
    assert offered, "no shipped workflow is offered although the engine lists every custom node they need"
    assert offered | {row["id"] for row in workflows["unavailable"]} == {m["id"] for m in shipped}
    assert all(row["missing"] and row["missing"][0]["reason"] for row in workflows["unavailable"])
    install = tmp_path / "allowed" / "studio"
    (install / ".venv" / "bin").mkdir(parents=True)
    (install / "main.py").write_text("# stand-in; never started\n")
    os.symlink(sys.executable, install / ".venv" / "bin" / "python")
    admin_store.put_operator_setting(process_template.SETTING, {"folders": [str(tmp_path / "allowed")]}, path=gateway.path)
    port = free_port()
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "workflow-image-engine", "name": "garage-studio", "slots": {"folder": str(install), "port": port}})
    assert created.status_code == 201, created.text

    # All four are saved as data, side by side.
    saved = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]
    assert {"garage-voice", "garage-ear", "garage-pictures", "garage-studio"} <= set(saved)
    proxy.reload_runtime_config()
    assert proxy._graph_service("images").service_id == "garage-studio"

    # One public request per engine.
    spoken = owner.post("/v1/audio/speech", headers=bearer,
                        json={"model": "tts-1", "voice": "am_adam", "input": "hello", "response_format": "wav"})
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"

    heard = owner.post("/v1/audio/transcriptions", headers=bearer, data={"model": ASR_PUBLIC, "response_format": "json"},
                       files={"file": ("clip.wav", _clip(), "audio/wav")})
    assert heard.status_code == 200, heard.text
    assert heard.json()["text"] == "hello from the fake"

    fake = FakeSdapi()
    wire_transport = transport.HttpxTransport.for_tests(HandlerTransport(fake))
    monkeypatch.setattr(executor, "HttpxTransport", lambda: wire_transport)
    made = owner.post("/v1/images/generations", headers=bearer,
                      json={"model": picked[0]["public_name"], "prompt": "a blue square", "response_format": "b64_json"})
    assert made.status_code == 200, made.text
    assert base64.b64decode(made.json()["data"][0]["b64_json"]) == fake.image
    assert fake.generated_with == [BETA]
