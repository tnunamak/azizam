"""Real media dispatches keep the fact revisions captured before engine egress."""
import asyncio
import copy

import pytest

import proxy
from gateway import media_jobs, receipt_store
from gateway.retention_facts import ledger, revision
from tests.support.adapter_differential_rig import GatewayRig
from tests.test_adapter_differential import _audio_comfy, _video_comfy, _capture_scheduling, HAVE_FFMPEG


@pytest.mark.skipif(not HAVE_FFMPEG, reason="media execution needs ffmpeg")
@pytest.mark.parametrize("kind,background", [("audio", False), ("audio", True), ("video", False), ("video", True)])
def test_media_dispatch_persists_exact_fact_snapshot(tmp_path, kind, background):
    with GatewayRig(f"retention-{kind}-{background}", tmp_path) as rig:
        fact = copy.deepcopy(ledger({}, kinds={})["destinations"][0]["facts"][0])
        fact = {key: value for key, value in fact.items()
                if key not in {"revision", "stale", "stale_label", "source"}}
        fact["how_long"] = "before dispatch"
        expected = revision(fact)
        registry = {"services": {proxy._GRAPH_BACKEND_ID: {"name": "Captured destination", "config": {
            "connection": {"protocol": "node-graph", "base_url": "http://comfy.fk.test"},
            "retention_facts": [fact],
        }}}}
        rig.set(proxy.ADMIN_STORE, "get_service_registry", lambda: copy.deepcopy(registry))
        _capture_scheduling(rig)
        if kind == "audio":
            _audio_comfy(rig)
            body = {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}
            path = "/v1/audio/generate"
        else:
            _video_comfy(rig)
            rig.set(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0)
            body = {"prompt": "waves at dusk", "model": "ltx-2.5-t2v"}
            path = "/v1/videos"
        original_graph = proxy._graph

        def graph(client, capability):
            # The adapter factory is reached at actual egress. An owner edit
            # here must not replace the request's earlier fact snapshot.
            registry["services"][proxy._GRAPH_BACKEND_ID]["config"]["retention_facts"][0]["how_long"] = "edited during dispatch"
            return original_graph(client, capability)

        rig.set(proxy, "_graph", graph)

        async def drive():
            if background:
                response = await rig.call("POST", "/gateway/v1/media/jobs", json_body={
                    "intent": f"{kind}.generate", "request": body,
                })
                assert response.status_code == 202, response.text
                job_id = response.json()["job_id"]
                worker = proxy._run_audio_media_job if kind == "audio" else proxy._run_video_media_job
                await worker(job_id)
                assert media_jobs.get(job_id, path=rig.media_jobs_db)["status"] == "succeeded"
            else:
                response = await rig.call("POST", path, json_body=body)
                assert 200 <= response.status_code < 300, response.text
            receipts = receipt_store.recent(path=rig.receipts_db, audience="internal")
            work = [row for row in receipts if expected in row.get("retention_facts", [])]
            assert len(work) == 1, receipts
            receipt = work[0]
            assert receipt["backend"] == proxy._GRAPH_BACKEND_ID
            assert receipt["status"] == "succeeded"
            saved = media_jobs.fact_revisions_for_source("receipt", receipt["request_id"], path=rig.media_jobs_db)
            assert saved[proxy._GRAPH_BACKEND_ID][0]["revision"] == expected
            assert saved[proxy._GRAPH_BACKEND_ID][0]["how_long"] == "before dispatch"
            assert media_jobs.fact_destinations_for_source("receipt", receipt["request_id"], path=rig.media_jobs_db)[proxy._GRAPH_BACKEND_ID]["name"] == "Captured destination"

        asyncio.run(drive())
