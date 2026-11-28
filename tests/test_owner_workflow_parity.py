"""Probe the compatibility projections against all bundled records."""

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import proxy
from gateway import registry_snapshot
from gateway.discovery import WorkflowRegistry
from gateway.manifests import manifest_identity
from gateway.workflow_openapi import ROOT_EXTENSION
from gateway.workflow_projections import SnapshotRegistry
from gateway.workflow_registry_store import facts_record
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, history_done
from tests.support.config_snapshot import publish_section
from tests.test_s7_parity_video_create import await_status

from gateway.video_limits import video_limits
from gateway.workflow_openapi import short_id, workflow_catalog
from tests.support.c2d_parity import shipped_registry


def _audio_result(record):
    from tests.test_adapter_differential import _wav

    selector = record.contract["bindings"][proxy.DERIVED._GRAPH_BINDING]["selectors"]["audio"]
    return selector, (_wav(), "audio/wav")


@pytest.mark.parametrize(("fixture", "route", "request_fields"), [
    ("image.generate.krea2-turbo", "/v1/images/generations",
     {"size": "1024x1024", "background": "transparent", "response_format": "b64_json"}),
    ("video.generate.minimax-h3-i2v", "/v1/videos", {"seconds": "3", "input_reference": "fixture.png"}),
    ("audio.generate.ace-step-1.5-xl-turbo", "/v1/audio/generate", {"duration": 8}),
])
def test_file_to_inline_keeps_public_catalog_and_outbound_prompt(tmp_path, fixture, route, request_fields):
    from tests.test_adapter_differential import _comfy

    release = registry_snapshot.bundled_release()
    original = release.workflows[fixture]
    contract = copy.deepcopy(original.contract)
    binding = contract["bindings"][proxy.DERIVED._GRAPH_BINDING]
    graph_path = Path(binding["workflow"])
    model = graph_path.stem
    alias = "fixture-owner-alias"
    contract.update(id=f"{original.intent}.owner-compatibility-test", compatibility_name=model, aliases=[alias])
    file_record = replace(original, manifest=contract, identity=manifest_identity(contract))
    candidates = dict(release.workflows)
    del candidates[fixture]
    candidates[file_record.id] = file_record
    release = replace(release, workflows=candidates)
    template = json.loads(graph_path.read_text())
    if fixture.startswith("image."):
        roles = {"background": {"output": "image", "processor": "10", "slot": 0}}
        binding["graph_roles"] = {k: v for k, v in roles.items() if k in ("background", "reference_order")}

    from tests.media_test_bytes import mp4_bytes, png_bytes
    if fixture.startswith("image."):
        selector, result = binding["selectors"]["image"], (png_bytes(), "image/png")
    elif fixture.startswith("video."):
        selector, result = binding["selectors"]["video"], (mp4_bytes(), "video/mp4")
    else:
        selector, result = _audio_result(file_record)
    suffix = result[1].split("/")[1]
    body = {"prompt": "A blue paper kite", "seed": 7, "model": alias, **request_fields}

    if route == "/v1/images/generations":
        body.pop("seed")
        body["extensions"] = {"com.azizam.workflow": {"seed": 7}}

    def publish(document, generation):
        snapshot = registry_snapshot.build(document, release, generation=generation)
        proxy.GATEWAY_REGISTRY_PROVIDER.swap(snapshot)
        serving = WorkflowRegistry()
        for record in SnapshotRegistry(snapshot).records():
            serving.add_manifest(record.contract, source_path=record.source_path, facts=record.facts)
        proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(serving)

    async def request_pair(rig):
        async def catalog():
            response = await rig.call("GET", "/v1/models")
            assert response.status_code == 200, response.text
            row, = [r for r in response.json()["data"] if r.get("azizam_workflow_id") == file_record.id]
            assert row["id"] == model
            # `created` is the artifact's filesystem timestamp when the binding names a file.
            row.pop("created")
            response = await rig.call("GET", "/openapi.json")
            assert response.status_code == 200, response.text
            entry, = [r for r in response.json()[ROOT_EXTENSION] if r["id"] == file_record.id]
            assert alias in entry["aliases"]
            return row, entry

        async def submitted_prompt(body):
            before = len(rig.recorder.engine)
            response = await rig.call("POST", route, json_body=body)
            assert response.status_code == 200, response.text
            if route == "/v1/videos":
                await await_status(SimpleNamespace(rig=rig), response.json()["id"])
            sent, = [entry for entry in rig.recorder.engine[before:] if entry["path"] == "/prompt"]
            return json.loads(sent["body"]["text"])["prompt"]

        publish({}, 1)
        file_rows = await catalog()
        file_prompt = await submitted_prompt(body)

        inline = copy.deepcopy(contract)
        inline_graph = copy.deepcopy(template)
        if route == "/v1/images/generations":
            # The processor relationship is carried by the binding.
            inline_graph.pop("_meta", None)
        inline["bindings"][proxy.DERIVED._GRAPH_BINDING]["workflow"] = {"inline": inline_graph}
        record = {"manifest": inline, "policy": facts_record(registry_snapshot._facts_of(original.facts)),
                  "published": True}
        owner_release = replace(release, workflows={k: v for k, v in release.workflows.items() if k != file_record.id})
        snapshot = registry_snapshot.build({"workflows": {file_record.id: record}}, owner_release, generation=2)
        proxy.GATEWAY_REGISTRY_PROVIDER.swap(snapshot)
        serving = WorkflowRegistry()
        for r in SnapshotRegistry(snapshot).records():
            serving.add_manifest(r.contract, source_path=r.source_path, facts=r.facts)
        proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(serving)
        if route == "/v1/videos":
            publish_section(rig.monkeypatch, "videos", {"workflows": {}})
            assert file_rows[1]["limits"]
        if route == "/v1/audio/generate":
            publish_section(rig.monkeypatch, "audio", {"workflows": {}})
            assert await submitted_prompt({**body, "model": model}) == file_prompt

        after_rows = await catalog()
        assert after_rows == file_rows, json.dumps({k: [file_rows[1].get(k), after_rows[1].get(k)] for k in file_rows[1].keys() | after_rows[1].keys() if file_rows[1].get(k) != after_rows[1].get(k)}, indent=2)
        assert await submitted_prompt(body) == file_prompt

    with GatewayRig("owner-workflow-parity", tmp_path) as rig:
        rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
        _comfy(rig, comfy_exchanges(
            history=history_done(selector["key"], f"fixture.{suffix}", node=selector["node"]),
            views=[result], strict=False))
        rig.set(proxy, "_litellm_models_payload", AsyncMock(return_value={"object": "list", "data": []}))
        rig.set(proxy, "_resolve_input_reference", AsyncMock(return_value=(png_bytes(), "fixture.png")))
        asyncio.run(request_pair(rig))




def test_composed_owner_template_can_be_inline(monkeypatch):
    graph = {"1": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}}}
    record = SimpleNamespace(manifest={"bindings": {"gateway": {"workflow": {"inline": graph}}}})
    monkeypatch.setattr(proxy, "IDENTITY_MANIFEST_ID", "image.edit.owner-fixture")
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", {"image.edit.owner-fixture": record})
    monkeypatch.setattr(proxy, "_identity_template_cache", None)
    assert proxy._identity_template()[0] == graph


def test_bundled_video_catalog_does_not_change():
    from gateway.video_limits import workflow_video_limits
    records = shipped_registry().records()
    old = video_limits(proxy.config.get("videos"))
    new = workflow_video_limits(records, proxy.config.get("videos"))
    assert workflow_catalog(records, limits=old) == workflow_catalog(records, limits=new)


def _install_owner_video_edit_alias(monkeypatch):
    original = registry_snapshot.bundled_release().workflows['video.generate.minimax-h3-i2v']
    contract = copy.deepcopy(original.contract)
    binding = contract['bindings'][proxy.DERIVED._GRAPH_BINDING]
    graph = json.loads(Path(binding['workflow']).read_text())
    contract.update(id='video.edit.owner-media-fixture', intent='video.edit',
                    compatibility_name='fixture-clip-edit')
    contract['inputs']['source_video'] = {'type': 'asset', 'accepts': ['video/*'], 'required': True}
    binding['workflow'] = {'inline': graph}
    release = registry_snapshot.bundled_release()
    document = {'workflows': {contract['id']: {'manifest': contract, 'published': True,
                'policy': facts_record(registry_snapshot._facts_of(original.facts))}}}
    snapshot = registry_snapshot.build(document, release, generation=1)
    proxy.GATEWAY_REGISTRY_PROVIDER.swap(snapshot)
    serving = WorkflowRegistry()
    for record in SnapshotRegistry(snapshot).records():
        serving.add_manifest(record.contract, source_path=record.source_path, facts=record.facts)
    proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(serving)
    publish_section(monkeypatch, 'model_aliases', {'videos': {'stable': {'fixture-morph': 'fixture-clip-edit'}}})
    publish_section(monkeypatch, 'videos', {'workflows': {}})
    assert contract['id'] not in release.workflows
    return snapshot, contract


def test_owner_inline_video_edit_alias_keeps_asset_modalities(tmp_path):
    with GatewayRig('owner-alias-metadata', tmp_path) as rig:
        _install_owner_video_edit_alias(rig.monkeypatch)
        rig.set(proxy, '_litellm_models_payload', AsyncMock(return_value={'object': 'list', 'data': []}))
        async def listed():
            response = await rig.call('GET', '/v1/models')
            assert response.status_code == 200, response.text
            row, = [r for r in response.json()['data'] if r['id'] == 'fixture-morph']
            assert row['architecture'] == {'input_modalities': ['text', 'image', 'video'],
                                           'output_modalities': ['video']}
        asyncio.run(listed())


def test_owner_video_edit_alias_request_validation_accepts_image_and_video(tmp_path):
    from gateway import workflow_projections
    from gateway.result_bundle import ResultLimits
    from gateway.workflow_runtime_types import MediaRef, RunRequest
    with GatewayRig('owner-alias-validation', tmp_path) as rig:
        snapshot, contract = _install_owner_video_edit_alias(rig.monkeypatch)
        target = proxy.resolve_model_for_operation('videos', 'fixture-morph').effective_model
        record = proxy._video_record(target)
        assert record.id == contract['id']
        entry = snapshot.get(record.id)
        profile = workflow_projections.PROJECTIONS.execution_profile(entry, proxy.DERIVED._GRAPH_BINDING, limits=ResultLimits())
        image_name = next(name for name, spec in contract['inputs'].items()
                          if spec.get('type') == 'asset' and 'image/*' in spec.get('accepts', []))
        request = RunRequest(workflow_id=entry.id, inputs={'prompt': 'A paper kite'}, media={
            image_name: (MediaRef('sha256:' + 'a' * 64, 'image/png', 100, asset_id='img_fixture'),),
            'source_video': (MediaRef('sha256:' + 'b' * 64, 'video/mp4', 100, asset_id='vid_fixture'),)})
        assert proxy._workflow_run_core()._validate(entry, request, profile) == []
        bad = replace(request, media={**request.media, 'source_video': request.media[image_name]})
        assert any(error.code == 'media_type' for error in proxy._workflow_run_core()._validate(entry, bad, profile))
