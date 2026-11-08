import collections, json
from gateway import discovery
def test_probe():
    keys = collections.defaultdict(list)
    for wid, m in discovery.bundled_workflow_manifests().items():
        keys[tuple(sorted(m["bindings"]))].append((wid, m.get("intent"), m.get("capability")))
    out = [f"{k}: {len(v)} e.g. {v[:3]}" for k, v in keys.items()]
    raise AssertionError("\n".join(out))
