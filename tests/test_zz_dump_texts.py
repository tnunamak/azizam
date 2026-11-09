import json, os
from tests.support.video_rig import run, MODEL, ALICE
import proxy

OUT = os.path.join(os.path.dirname(__file__), "..", "texts_dump.json")

def test_dump(tmp_path):
    rows = []
    async def exercise(w):
        async def rec(label, response):
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:200]}
            err = body.get("error") if isinstance(body, dict) else None
            rows.append({"case": label, "status": response.status_code, "error": err if isinstance(err, dict) else body})
        post = lambda body, **kw: w.call("POST", "/v1/videos", json_body=body, **kw)
        C = {"prompt": "x", "model": MODEL}
        await rec("invalid_json", await w.call("POST", "/v1/videos", content=b"{", headers={"content-type": "application/json"}))
        await rec("prompt_number", await post({**C, "prompt": 7}))
        await rec("prompt_missing", await post({"model": MODEL}))
        await rec("prompt_empty", await post({**C, "prompt": ""}))
        await rec("seconds_low", await post({**C, "seconds": "0.1"}))
        await rec("seconds_high", await post({**C, "seconds": "9999"}))
        await rec("seconds_text", await post({**C, "seconds": "abc"}))
        await rec("seconds_negative", await post({**C, "seconds": "-3"}))
        await rec("size_number", await post({**C, "size": 5}))
        await rec("size_bad_text", await post({**C, "size": "bad"}))
        await rec("model_unknown", await post({**C, "model": "nope-model"}))
        await rec("model_number", await post({**C, "model": 5}))
        await rec("i2v_without_input_reference", await post({**C, "model": "ltx-2.5-i2v"}))
        await rec("h3_i2v_without_input_reference", await post({**C, "model": "minimax-h3-i2v"}))
        await rec("unknown_field", await post({**C, "bogus_field": 1}))
        await rec("shots_on_t2v", await post({**C, "shots": [{"prompt": "a"}]}))
        await rec("seconds_past_native", await post({**C, "seconds": "60"}))
        await rec("scail_without_source", await post({**C, "model": "scail-2-replace"}))
        await rec("audio_text", await post({**C, "audio": "maybe"}))
        await rec("extensions_text", await post({**C, "extensions": "x"}))
        await rec("extensions_workflow_unknown", await post({**C, "extensions": {"workflow": "nope"}}))
        await rec("multipart_bad", await w.call("POST", "/v1/videos", content=b"--x\r\nbroken", headers={"content-type": "multipart/form-data; boundary=x"}))
        await rec("input_reference_bad", await w.call("POST", "/v1/videos", files=[("prompt", (None, "x")), ("model", (None, "ltx-2.5-i2v")), ("input_reference", ("a.png", b"notanimage", "image/png"))]))
        await rec("status_invalid", await w.call("GET", "/v1/videos/nonsense"))
        await rec("status_unknown", await w.call("GET", "/v1/videos/video_nosuch"))
        await rec("content_invalid", await w.call("GET", "/v1/videos/nonsense/content"))
        await rec("content_unknown", await w.call("GET", "/v1/videos/video_nosuch/content"))
        await rec("delete_invalid", await w.call("DELETE", "/v1/videos/nonsense"))
        await rec("delete_unknown", await w.call("DELETE", "/v1/videos/video_nosuch"))
        await rec("list_limit_zero", await w.listing(limit="0"))
        await rec("list_limit_text", await w.listing(limit="many"))
        await rec("list_limit_high", await w.listing(limit="101"))
        await rec("list_order", await w.listing(order="sideways"))
        await rec("list_after_malformed", await w.listing(after="not-a-video"))
        await rec("list_after_unknown", await w.listing(after="video_evicted"))
        await rec("remix_unconfigured", await w.call("POST", "/v1/videos/video_nosuch/remix", json_body={"prompt": "x"}))
        await rec("remix_no_prompt", await w.call("POST", "/v1/videos/video_nosuch/remix", json_body={}))
        await rec("edit_no_prompt", await w.call("POST", "/v1/videos/edits", json_body={"video": {"id": "video_x"}}))
        await rec("edit_no_source", await w.call("POST", "/v1/videos/edits", json_body={"prompt": "x"}))
        await rec("edit_unconfigured", await w.call("POST", "/v1/videos/edits", json_body={"prompt": "x", "video": {"id": "video_x"}}))
        # a real video
        vid = await w.create()
        await rec("content_not_ready", await w.content(vid))
        w.state(vid, "completed")
        w.engine.view_error = True
        await rec("content_view_error", await w.content(vid))
        w.engine.view_error = False
        for label, body in [("extend_seconds_text", {"video": {"id": vid}, "prompt": "p", "seconds": "abc"}),
                            ("extend_no_prompt", {"video": {"id": vid}}),
                            ("extend_no_source", {"prompt": "p"}),
                            ("extend_unknown_source", {"video": {"id": "video_nosuch"}, "prompt": "p"}),
                            ("extend_bad_shots", {"video": {"id": vid}, "prompt": "p", "shots": "x"}),
                            ("extend_bad_workflow_ext", {"video": {"id": vid}, "prompt": "p", "extensions": {"workflow": "nope"}})]:
            await rec(label, await w.call("POST", "/v1/videos/extensions", json_body=body))
        # disabled / unreachable
        proxy.runtime().derived["COMFY_ENABLED"] = False
        await rec("disabled", await post(C))
        proxy.runtime().derived["COMFY_ENABLED"] = True
    run(tmp_path, exercise)
    json.dump(rows, open(OUT, "w"), indent=1, sort_keys=True)
