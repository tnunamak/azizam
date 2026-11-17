"""Measure fd30cbfca public-route answers for the video review regressions."""
import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import proxy
from gateway.workflow_run import plain
from tests.test_s7_parity_video_create import world, HOST, submitted, await_status, rows
from tests.support.access import authenticated, identity, plan_for
from tests.support.config_snapshot import publish_section
from tests.support.fake_upstream import HandlerTransport


@pytest.mark.parametrize("case", ["row-plain", "row-resolution", "operator-shots", "operator-seconds",
                                 "chain-denied", "chain-receipt", "chain-submit-failure"])
def test_main_public_review_reference(world, record_property, case):
    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    body = {"model": "ltx-2.5-t2v", "prompt": "x", "seed": 7}
    if case.startswith("row-"):
        videos = copy.deepcopy(proxy.config["videos"])
        del videos["workflows"]["video-ltx-2.5-t2v"]
        publish_section(world.rig.monkeypatch, "videos", videos)
        if case == "row-resolution":
            body["inputs"] = {"steps": 6}
    if case.startswith("operator-"):
        original = proxy.WORKFLOW_SNAPSHOTS.current()
        key = "video.generate.ltx-2.5-t2v"
        entry = original.get(key)
        contract = plain(entry.contract)
        del contract["video"]["variants"]["long"]
        changed = replace(entry, contract=contract)
        class Snapshot:
            workflows = {**original.workflows, key: changed}
            def get(self, workflow_id):
                return changed if workflow_id == key else original.get(workflow_id)
            def by_intent(self, intent):
                return tuple(self.get(item.id) for item in original.by_intent(intent))
            def __getattr__(self, name):
                return getattr(original, name)
        world.rig.set(proxy, "WORKFLOW_SNAPSHOTS", SimpleNamespace(current=lambda: Snapshot()))
        if case == "operator-shots":
            body["shots"] = [{"prompt":"first", "seconds":4}, {"prompt":"second", "seconds":4}]
        else:
            body["seconds"] = 16
    if case.startswith("chain-"):
        body.update(model="ltx-2.5-long", seconds=16)
    if case == "chain-submit-failure":
        def handle(request):
            if request.method == "POST" and request.url.path == "/prompt":
                raise httpx.ConnectError("connection refused", request=request)
            return world.engine(request)
        world.rig.monkeypatch.setitem(world.rig.fakes.router._routes, HOST, HandlerTransport(handle))

    async def drive():
        if case == "chain-denied":
            with authenticated(identity(plan_for(caps=(), categories=()))):
                response = await world.rig.call("POST", "/v1/videos", json_body=body)
        else:
            response = await world.rig.call("POST", "/v1/videos", json_body=body)
        answer = response.json()
        if response.status_code == 200 and answer["id"].startswith("video_mj_"):
            await submitted(world, 1)
            await await_status(world, answer["id"])
        kept = rows(world.rig.receipts_db, "receipts")
        result = {"request":body, "status":response.status_code,
                  "body":{key:value for key,value in answer.items() if key not in
                          {"id","created_at","completed_at","expires_at","updated_at"}},
                  "receipts":[json.loads(row["receipt_json"]) for row in kept]}
        record_property("main_reference", json.dumps(result, sort_keys=True))
        assert response.status_code == ({"row-plain":200,"row-resolution":404,"operator-shots":400,
                                        "operator-seconds":400,"chain-denied":403,"chain-receipt":200,
                                        "chain-submit-failure":502}[case]), response.text
    asyncio.run(drive())
