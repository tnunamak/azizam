import json
from tests.support import video_rig
from tests.support.video_rig import run, drive, MODEL, ALICE
from tests.support.legacy_video import legacy_video_record
from tests.test_s7_parity_video_create import configure_remix
import proxy


def test_probe(tmp_path):
    rows = []

    async def rec(label, r):
        try:
            r = await r
        except BaseException as exc:
            rows.append({"case": label, "status": -1, "body": {"exc": repr(exc)[:200]}})
            return
        try:
            b = r.json()
        except Exception:
            b = {"raw": r.text[:300]}
        rows.append({"case": label, "status": r.status_code, "body": b})

    async def exercise(w):
        post = lambda body, **k: w.call("POST", "/v1/videos", json_body=body, **k)
        r0 = await w.call("POST", "/v1/videos", json_body={"prompt": "x", "model": MODEL})
        vid = r0.json()["id"]
        w.engine.states[vid.removeprefix("video_")] = "completed"
        configure_remix(w, None)
        await rec("remix_unconfigured", w.call("POST", f"/v1/videos/{vid}/remix", json_body={"prompt": "new dusk"}))
        await rec("edit_unconfigured", w.call("POST", "/v1/videos/edits", json_body={"prompt": "new dusk", "video": {"id": vid}}))
        configure_remix(w, "video-ltx-2.5-t2v")
        await rec("remix_source_video_ext", w.call("POST", f"/v1/videos/{vid}/remix", json_body={"prompt": "x", "extensions": {"com.azizam.workflow": {"source_video": "x"}}}))
        await rec("create_unknown_ext_key", post({"prompt": "x", "model": MODEL, "extensions": {"com.azizam.workflow": {"nope": 1}}}))
        import copy
        from tests.support.config_snapshot import publish_section
        wf = copy.deepcopy(proxy.config["videos"]["workflows"])
        rows.append({"case": "wfkeys", "body": {"k": {k: [v.get("chain_workflow"), v.get("max_seconds")] for k, v in wf.items()}}, "status": 0})
        for k in wf:
            wf[k].pop("chain_workflow", None)
        publish_section(w.rig.monkeypatch, "videos", {**proxy.config["videos"], "workflows": wf})
        await rec("nochain_shots", w.call("POST", "/v1/videos", json_body={"prompt": "x", "model": MODEL, "shots": [{"prompt": "a", "seconds": 2}]}))
        await rec("nochain_seconds", w.call("POST", "/v1/videos", json_body={"prompt": "x", "model": MODEL, "seconds": "9"}))
        old = video_rig.history_done
        import tests.support.video_rig as vr
        vr.history_done = lambda kind, name, **k: old(kind, "clip.png", **k)
        await rec("content_no_output", w.call("GET", f"/v1/videos/{vid}/content"))
        vr.history_done = old
        w.engine.history_mode = "unavailable"
        await rec("legacy_status_engine_down", w.call("GET", f"/v1/videos/{vid}"))
        await rec("legacy_content_engine_down", w.call("GET", f"/v1/videos/{vid}/content"))
        w.engine.history_mode = None
        await rec("remix_missing_source", w.call("POST", "/v1/videos/video_nosuch/remix", json_body={"prompt": "x"}))
        await rec("edit_missing_source", w.call("POST", "/v1/videos/edits", json_body={"prompt": "x", "video": {"id": "video_nosuch"}}))
    run(tmp_path, exercise)
    print("DUMP" + json.dumps(rows, sort_keys=True)); assert 0
