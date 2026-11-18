from dataclasses import replace
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway.api import create_gateway_router
from gateway.registry_snapshot import bundled_release, build
from tests.support.gateway import gateway_client, use_admin_db
from tests.support.access import authenticated, everything, identity
import asyncio


EXPOSURE_ID = "image.generate.krea2-turbo"
STATE_ID = "image.generate.anima-turbo"


def closed_snapshot():
    snap = build({}, bundled_release(), generation=1)
    rows = dict(snap.workflows)
    rows[STATE_ID] = replace(rows[STATE_ID], state="disabled")
    rows[EXPOSURE_ID] = replace(rows[EXPOSURE_ID], facts=replace(rows[EXPOSURE_ID].facts, exposure="disabled"))
    return replace(snap, workflows=rows)


def test_client_workflows_hide_both_closed_but_admin_sees_them():
    provider = SimpleNamespace(current=closed_snapshot)
    app = FastAPI()
    app.include_router(create_gateway_router(provider))
    app.include_router(create_gateway_router(provider, audience_resolver=lambda _: "admin", prefix="/admin-view"))
    with TestClient(app, client=("203.0.113.10", 1234)) as client:
        for prefix in ("/gateway/v1", "/admin-view"):
            response = client.get(prefix + "/workflows")
            assert response.status_code == 200, response.text
            ids = {row["id"] for row in response.json()["workflows"]}
            assert (EXPOSURE_ID in ids) == (prefix == "/admin-view")
            assert (STATE_ID in ids) == (prefix == "/admin-view")
            if prefix == "/admin-view":
                rows = {row["id"]: row for row in response.json()["workflows"]}
                assert rows[EXPOSURE_ID]["exposure"] == "disabled"
                assert rows[STATE_ID]["state"] == "disabled"


def test_models_hide_both_closed(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    snapshot = closed_snapshot()
    monkeypatch.setattr(type(proxy.GATEWAY_REGISTRY_PROVIDER), "current", lambda self: snapshot)
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", proxy.GATEWAY_REGISTRY_PROVIDER)

    async def get():
        with authenticated(identity(everything())):
            async with gateway_client(client=("203.0.113.11", 1234)) as client:
                return await client.get("/v1/models")

    response = asyncio.run(get())
    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()["data"]}
    assert "krea2-turbo" not in ids
    assert "anima-turbo" not in ids


def test_client_capabilities_hide_closed_and_admin_keeps_them():
    from gateway.capabilities import Capability

    provider = SimpleNamespace(current=closed_snapshot)
    capabilities = [Capability(intent="image.generate", backend="fixture", workflow=wid, model=wid)
                    for wid in (EXPOSURE_ID, STATE_ID)]
    app = FastAPI()
    app.include_router(create_gateway_router(provider, capabilities_provider=lambda _: capabilities))
    app.include_router(create_gateway_router(provider, capabilities_provider=lambda _: capabilities,
                                            audience_resolver=lambda _: "admin", prefix="/admin-view"))
    with TestClient(app, client=("203.0.113.15", 1234)) as client:
        assert client.get("/gateway/v1/capabilities").json()["capabilities"] == []
        assert len(client.get("/admin-view/capabilities").json()["capabilities"]) == 2


def test_aliases_hide_closed_targets_and_refuse_closed_run(tmp_path):
    import copy
    from tests.support.adapter_differential_rig import GatewayRig
    from tests.support.fake_upstream import StreamingASGITransport
    from tests.test_s7_parity_images import ok

    with GatewayRig("closed-alias", tmp_path) as rig:
        use_admin_db(rig.monkeypatch, tmp_path)
        ok(rig)
        snapshot = closed_snapshot()
        rows = dict(snapshot.workflows)
        edit_id = "image.edit.qwen-image-2.1-edit"
        rows[edit_id] = replace(rows[edit_id], state="disabled")
        snapshot = replace(snapshot, workflows=rows)
        rig.monkeypatch.setattr(type(proxy.GATEWAY_REGISTRY_PROVIDER), "current", lambda self: snapshot)
        rig.set(proxy, "WORKFLOW_SNAPSHOTS", proxy.GATEWAY_REGISTRY_PROVIDER)
        rig._client._transport = StreamingASGITransport(proxy.app, client=("203.0.113.16", 1234))
        aliases = copy.deepcopy(proxy.config["model_aliases"])
        aliases["images"]["stable"]["only-closed"] = "krea2-turbo"
        aliases["chat"]["stable"]["only-closed"] = "default"
        aliases["images"]["stable"]["mixed-alias"] = "moss-image-fast"
        aliases["image_edits"]["stable"]["mixed-alias"] = "qwen-image-2.1-edit"
        rig.monkeypatch.setitem(proxy.config, "model_aliases", aliases)

        async def exercise():
            models = await rig.call("GET", "/v1/models")
            closed = await rig.call("POST", "/v1/images/generations",
                                    json_body={"model": "only-closed", "prompt": "hello"})
            return models, closed

        models, closed = asyncio.run(exercise())
        rows = {row["id"]: row for row in models.json()["data"]}
        assert "only-closed" not in rows
        assert rows["mixed-alias"]["azizam_resolves_to"] == {"images": "moss-image-fast"}
        assert closed.status_code == 409
        assert closed.json()["error"] == {"code": "workflow_disabled", "message": "workflow image.generate.krea2-turbo is disabled"}
        assert not rig.recorder.engine


@pytest.mark.parametrize("manifest_missing", [False, True])
def test_disabled_audio_is_absent_and_both_routes_refuse_before_engine(tmp_path, manifest_missing):
    from tests.support.adapter_differential_rig import GatewayRig
    from tests.support.fake_upstream import StreamingASGITransport
    from tests.test_s7_parity_audio import _ok, BODY, JOB_BODY

    with GatewayRig("closed-audio", tmp_path) as rig:
        use_admin_db(rig.monkeypatch, tmp_path)
        _ok(rig)
        rig._client._transport = StreamingASGITransport(proxy.app, client=("203.0.113.17", 1234))
        snapshot = proxy.GATEWAY_REGISTRY_PROVIDER.current()
        wid = "audio.generate.tangoflux"
        closed = replace(snapshot.get(wid), state="disabled")
        rig.monkeypatch.setattr(proxy.GATEWAY_REGISTRY_PROVIDER, "_snapshot", replace(
            snapshot, workflows={**snapshot.workflows, wid: closed}))
        rig.set(proxy, "WORKFLOW_SNAPSHOTS", proxy.GATEWAY_REGISTRY_PROVIDER)
        if manifest_missing:
            rig.set(proxy, "_load_audio_workflow_manifest", lambda _name: None)

        async def exercise():
            listing = await rig.call("GET", "/v1/audio/models")
            sync = await rig.call("POST", "/v1/audio/generate", json_body=BODY)
            queued = await rig.call("POST", "/gateway/v1/media/jobs", json_body=JOB_BODY)
            return listing, sync, queued

        listing, sync, queued = asyncio.run(exercise())
        assert "tangoflux" not in {item["id"] for item in listing.json()["data"]}
        for response in (sync, queued):
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "workflow_disabled"
            assert response.json()["error"]["message"] == "workflow audio.generate.tangoflux is disabled"
        assert not rig.recorder.engine
