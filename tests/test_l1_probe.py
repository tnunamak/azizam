def test_probe():
    import json, proxy
    from gateway import policy_protocol
    r = proxy.GATEWAY_WORKFLOW_REGISTRY
    for rec in r.records():
        if rec.intent.startswith("image.") or rec.intent in ("video.generate","audio.generate") or rec.intent.startswith(("video.","audio.")):
            old = rec.intent.startswith("image.") or rec.intent in ("video.generate","audio.generate")
            new = policy_protocol.lists_as_model(rec.id, rec.contract)
            if old != new:
                print(rec.id, rec.intent, "old",old,"new",new, json.dumps({"in":{k:{kk:vv for kk,vv in v.items() if kk in("type","media_type")} for k,v in rec.contract["inputs"].items()},"out":[o.get("media_type") for o in rec.contract["outputs"].values()]}))
