"""Last domain literals: changed data reaches consumers; shipped contracts stay fixed."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import proxy
from gateway import (admin_store, capability_ids, discovery_metadata, domain_rows,
                     engine_admin, identity_api, media_job_intents, openai_media_schemas,
                     toolcall_detection, workflow_openapi, workflow_request_maps)
from gateway.resource_admission_http import advise_route_admission, admission_http_response
from tests.test_domain_literal_ratchet import all_sites, partition_reviewed_sites

ROOT = Path(__file__).resolve().parents[1]


def test_no_unreviewed_domain_sites_remain():
    _, remaining = partition_reviewed_sites(all_sites())
    assert remaining == {}


def test_published_queue_name_is_preserved():
    decision = advise_route_admission(
        intent="image.generate", service_available=True, queue_key=proxy._MEDIA_JOB_QUEUE_KEY,
        max_queue_depth=1, queue_depths={proxy._MEDIA_JOB_QUEUE_KEY: 1})
    response = admission_http_response(decision, audience="owner")
    assert response.body["error"]["message"] == "queue full: comfyui-media"
    assert proxy._AUDIO_SECTION == "audio"
    assert (proxy._IMAGE_FIELD, proxy._VIDEO_FIELD, proxy._VIDEO_OBJECT,
            proxy._NODE_IMAGE_INPUT, proxy._STREAM_FORMAT_AUDIO) == ("image", "video", "video", "image", "audio")


def test_identity_manifest_and_model_follow_gateway_binding(monkeypatch):
    assert proxy._identity_workflow_ids() == ("image.edit.image-identity", "image-identity")
    record = SimpleNamespace(id="portrait.compose.new", manifest={"bindings": {"gateway": {
        "route": identity_api.IDENTITY_PATH, "compose": "identity_scene", "model_id": "portrait-new"}}})
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", SimpleNamespace(records=lambda: [record]))
    assert proxy._identity_workflow_record() is record
    assert proxy._identity_workflow_ids() == ("portrait.compose.new", "portrait-new")


def test_identity_job_intent_follows_internal_route_row(monkeypatch):
    rows = media_job_intents.load_rows()
    row = next(r for r in rows if r.intent == "image.identity")
    assert row.execution_route == identity_api.IDENTITY_PATH
    from dataclasses import replace
    monkeypatch.setattr(media_job_intents, "ROWS", tuple(
        replace(r, intent="portrait.compose") if r is row else r for r in rows))
    assert media_job_intents.execution_intent(identity_api.IDENTITY_PATH) == "portrait.compose"


def test_internal_intent_route_refuses_missing_or_ambiguous_rows(monkeypatch):
    row = next(r for r in media_job_intents.load_rows() if r.intent == "image.identity")
    for rows in ((), (row, row)):
        monkeypatch.setattr(media_job_intents, "ROWS", rows)
        with pytest.raises(ValueError, match="execution route"):
            media_job_intents.execution_intent(identity_api.IDENTITY_PATH)


def test_video_reference_input_follows_request_map(tmp_path):
    document = yaml.safe_load(workflow_request_maps.DEFAULT_PATH.read_text())
    row = next(r for r in document["maps"] if "image" in r.get("inputs", {}))
    row["inputs"]["portrait"] = row["inputs"].pop("image")
    path = tmp_path / "maps.yml"
    path.write_text(yaml.safe_dump(document))
    maps = workflow_request_maps.load(path)
    assert workflow_request_maps.input_for_field("input_reference", maps=maps) == "portrait"
    assert workflow_openapi.VIDEO_REFERENCE_INPUT == "image"
    with pytest.raises(ValueError, match="input_reference"):
        workflow_request_maps.input_for_field("input_reference", maps=workflow_request_maps.load(
            _without_reference(tmp_path, document)))


def _without_reference(tmp_path, document):
    changed = copy.deepcopy(document)
    for row in changed["maps"]:
        row.get("inputs", {}).pop("portrait", None)
    path = tmp_path / "no-reference.yml"
    path.write_text(yaml.safe_dump(changed))
    return path


def test_discovery_guidance_reads_copy(monkeypatch):
    messages = dict(toolcall_detection.GATEWAY_MESSAGES)
    messages.update(surface_existing_clients="; legacy at {convenience}.",
                    surface_new_setup="Begin at {canonical}")
    monkeypatch.setattr(toolcall_detection, "GATEWAY_MESSAGES", messages)
    protocols = [{"default": True, "canonical_base_path": "/api", "convenience_base_path": "/old"}]
    assert discovery_metadata._default_surface_guidance(protocols, numbered=True) == (
        "4. Use /api by default for new setup; legacy at /old.")
    assert discovery_metadata._default_surface_guidance(protocols) == (
        "Begin at /api; /old remains a convenience route for existing clients.")


def test_recommendation_templates_preserve_bytes_and_accept_changed_copy():
    messages = toolcall_detection.GATEWAY_MESSAGES
    assert messages["long_video_not_supported"].format(model="demo", max_seconds="8") == (
        "Model demo cannot render shots or clips longer than 8 s; use video-long, ltx-2.5-long or minimax-h3")
    assert messages["video_extend_not_supported"].format(model=repr("demo")) == (
        "model 'demo' cannot extend videos; use video-long, ltx-2.5-long or minimax-h3")


def test_identity_reference_schema_is_data_and_shipped_shape_is_identical(monkeypatch):
    schema = identity_api._identity_reference_schema()
    assert schema == {"type": "object", "required": ["image", "prompt"], "properties": {
        "image": {"type": "array", "items": {"type": "string", "format": "binary"},
                  "description": "Reference photo(s) at full resolution. Never send tiny face crops: the gateway crops roomy heads itself. Several photos of one person are welcome: list them in people[].images."},
        "prompt": {"type": "string", "description": "The new scene, poses and gaze. People are tagged for you."}}}
    fragments = copy.deepcopy(openai_media_schemas._SCHEMA_FRAGMENTS)
    fragments["POST " + identity_api.IDENTITY_PATH]["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]["image"]["description"] = "Changed reference help."
    monkeypatch.setattr(openai_media_schemas, "_SCHEMA_FRAGMENTS", fragments)
    assert identity_api._identity_reference_schema()["properties"]["image"]["description"] == "Changed reference help."


def test_starter_migration_uses_declared_media_capability(monkeypatch):
    from gateway import access_plans
    monkeypatch.setattr(capability_ids, "MEDIA_TYPE_MODALITIES", {"model/gltf-binary": "sculpture"})
    monkeypatch.setattr(access_plans, "CAPABILITIES", ("chat", "sculpture"))
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript("CREATE TABLE schema_migration_steps(name TEXT PRIMARY KEY); CREATE TABLE access_plans(id TEXT, name TEXT, plan_json TEXT, updated_at TEXT);")
        conn.execute("INSERT INTO access_plans VALUES (?, ?, ?, ?)", (
            "p", admin_store.STARTER_PLAN_NAME, json.dumps({"can_use": {"chat": {"items": "all"}}}), "old"))
        admin_store._grant_mesh_to_starter_plan(conn)
        document = json.loads(conn.execute("SELECT plan_json FROM access_plans").fetchone()[0])
        assert document["can_use"] == {"chat": {"items": "all"}, "sculpture": {"items": "all"}}
        assert conn.execute("SELECT name FROM schema_migration_steps").fetchone()[0] == admin_store.MESH_STARTER_STEP
    finally:
        conn.close()


def test_recommendation_copy_reaches_both_video_routes(tmp_path, monkeypatch):
    from tests.support import video_rig
    from tests.support.config_snapshot import publish_section
    from tests.support.video_rig import MODEL
    messages = {**toolcall_detection.GATEWAY_MESSAGES,
                "long_video_not_supported": "Select configured long renderer for {model} ({max_seconds}).",
                "video_extend_not_supported": "Select configured extension renderer for {model}."}
    monkeypatch.setattr(toolcall_detection, "GATEWAY_MESSAGES", messages)

    async def exercise(world):
        workflows = copy.deepcopy(proxy.config["videos"]["workflows"])
        for entry in workflows.values():
            entry.pop("chain_workflow", None)
        publish_section(world.rig.monkeypatch, "videos", {**proxy.config["videos"], "workflows": workflows})
        response = await world.call("POST", "/v1/videos", json_body={
            "model": MODEL, "prompt": "waves", "shots": [{"prompt": "waves", "seconds": 5}]})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["message"].startswith("Select configured long renderer for ")
        from tests.test_s7_parity_video_texts import make_video, finish
        vid = await make_video(world)
        await finish(world, vid)
        response = await world.call("POST", "/v1/videos/extensions", json_body={
            "model": "unconfigured-renderer", "prompt": "waves", "video": {"id": vid}})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["message"] == "Select configured extension renderer for 'unconfigured-renderer'."
    video_rig.run(tmp_path, exercise)


def test_transcription_settings_use_frontend_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(domain_rows, "route_intent", lambda path: "speech.recognize" if path == "/v1/audio/transcriptions" else None)
    record = {"capabilities": ["speech.recognize"], "config": {"chunking": {}}}
    seen = []
    store = SimpleNamespace(path=tmp_path / "fixture.db",
                            get_service_registry_snapshot=lambda: ({"services": {"s": record}}, 0),
                            update_service=lambda **kwargs: seen.append(kwargs) or {"operator_settings_generation": 1})
    result = engine_admin.EngineService(store).configure_stt(
        SimpleNamespace(roles=("owner",)), "s", {"native_long_audio_offline": True}, expected_generation=0)
    assert result["revision"] == '"os-1"'
    assert seen[0]["service_id"] == "s"


def test_identity_upload_and_error_param_follow_wire_declaration(monkeypatch):
    from dataclasses import replace
    from tests.test_identity_api import build, PHOTO, GOOD
    registry = openai_media_schemas._MEDIA_SCHEMA_REGISTRY
    monkeypatch.setattr(openai_media_schemas, "_MEDIA_SCHEMA_REGISTRY", replace(
        registry, wire_error_params={**registry.wire_error_params, "identity_scene": {"reference_photos": "portrait"}}))
    client, calls, _ = build([GOOD])
    with client:
        response = client.post(identity_api.IDENTITY_PATH,
                               files={"portrait": ("ref.png", PHOTO, "image/png")},
                               data={"prompt": "standing", "attempts": "1", "refine": "off"})
        assert response.status_code == 200, response.text
        assert calls
        response = client.post(identity_api.IDENTITY_PATH, files={"other": ("empty", b"x")}, data={"prompt": "x"})
        assert response.status_code == 400
        assert response.json()["error"]["param"] == "portrait"


def test_private_diagnostics_preserve_declared_wire_param(monkeypatch):
    from dataclasses import replace
    from tests.test_identity_jobs import result
    registry = openai_media_schemas._MEDIA_SCHEMA_REGISTRY
    monkeypatch.setattr(openai_media_schemas, "_MEDIA_SCHEMA_REGISTRY", replace(
        registry, wire_error_params={**registry.wire_error_params, "identity_scene": {"reference_photos": "portrait"}}))
    scene = result()
    scene.warnings = [{"code": "reference_warning", "param": "portrait"}]
    request = SimpleNamespace(prompt="portrait", people=[])
    assert identity_api.stored_scene_extras(scene, request)["warnings"][0]["param"] == "portrait"
