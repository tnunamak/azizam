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
        for m in ("ltx-2.5-i2v", "minimax-h3-t2v", "minimax-h3", "ltx-2.5-long", "video-long", "scail-2-replace", "minimax-h3-i2v"):
            await rec("shots_" + m, post({"prompt": "x", "model": m, "shots": [{"prompt": "a", "seconds": 2}]}))
            await rec("seconds60_" + m, post({"prompt": "x", "model": m, "seconds": "60"}))
        for m in ("ltx-2.5-long", "video-long", "minimax-h3"):
            await rec("shotsstr_" + m, post({"prompt": "x", "model": m, "shots": "not json"}))
            await rec("shotsempty_" + m, post({"prompt": "x", "model": m, "shots": []}))
            await rec("shotsnonobj_" + m, post({"prompt": "x", "model": m, "shots": [5]}))
            await rec("shotsnoprompt_" + m, post({"prompt": "x", "model": m, "shots": [{"seconds": 2}]}))
            await rec("shotsnosec_" + m, post({"prompt": "x", "model": m, "shots": [{"prompt": "a"}]}))
            await rec("shotsbadopt_" + m, post({"prompt": "x", "model": m, "extensions": {"com.azizam.workflow": {"zzz": 1}}, "seconds": "60"}))
        # extend invalid_*
        await rec("extend_bad_ext", w.call("POST", "/v1/videos/extensions", json_body={"video": {"id": vid}, "prompt": "p", "extensions": {"com.azizam.workflow": {"zzz": 1}}}))
        await rec("extend_shots_str", w.call("POST", "/v1/videos/extensions", json_body={"video": {"id": vid}, "prompt": "p", "shots": "x"}))
        await rec("extend_ext_not_map", w.call("POST", "/v1/videos/extensions", json_body={"video": {"id": vid}, "prompt": "p", "extensions": "x"}))
        # engine down at create
        w.engine.history_mode = None
        # legacy: no output
        old = video_rig.history_done
        video_rig.history_done = lambda *a, **k: {k.get("prompt_id", "p9"): {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": {}}}
        lv = legacy_video_record(w, "old-p9")
        w.engine.states["old-p9"] = "completed"
        await rec("content_no_output", w.call("GET", f"/v1/videos/{lv}/content"))
        video_rig.history_done = old
        w.engine.history_mode = "unavailable"
        lv2 = legacy_video_record(w, "old-p8")
        await rec("legacy_status_engine_down", w.call("GET", f"/v1/videos/{lv2}"))
        await rec("legacy_content_engine_down", w.call("GET", f"/v1/videos/{lv2}/content"))
        w.engine.history_mode = None
        await rec("remix_missing_source", w.call("POST", "/v1/videos/video_nosuch/remix", json_body={"prompt": "x"}))
        await rec("edit_missing_source", w.call("POST", "/v1/videos/edits", json_body={"prompt": "x", "video": {"id": "video_nosuch"}}))
    run(tmp_path, exercise)
    print("DUMP" + json.dumps(rows, sort_keys=True)); assert 0
