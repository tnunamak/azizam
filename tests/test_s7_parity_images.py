"""S7 parity: the image routes' observable behaviour, old route against frontend row (scratch)."""
from __future__ import annotations

import json

from tests import test_adapter_differential as base
from tests.support.adapter_differential_rig import comfy_exchanges
from tests.support.s7_parity_images_rig import observe

GEN = "/v1/images/generations"


def test_probe_generate(tmp_path):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")]))

    req = {"json_body": {"prompt": "a red fox", "response_format": "b64_json", **base.BG}}
    old = observe(tmp_path, GEN, req, setup, row=False)
    new = observe(tmp_path, GEN, req, setup, row=True)
    out = {}
    for k, o in (("old", old), ("row", new)):
        out[k] = {"status": o.status, "headers": o.headers, "body": str(o.body)[:300], "engine": o.engine, "sleeps": o.sleeps,
                  "receipts": o.receipts, "assets": o.assets}
    print("PROBE", json.dumps(out, indent=1, default=str)[:12000])
    assert False
