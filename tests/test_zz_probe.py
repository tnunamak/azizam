import json
from tests.support.video_rig import run, MODEL
from tests.test_adapter_differential import _comfy_down
from tests.support.adapter_differential_rig import GatewayRig

def test_probe(tmp_path):
    out = {}
    with GatewayRig("probe-down", tmp_path) as rig:
        _comfy_down(rig)
        import asyncio
        async def go():
            r = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": MODEL})
            out["status"], out["text"] = r.status_code, r.text
        asyncio.run(go())
    print("DUMP" + json.dumps(out)); assert 0
