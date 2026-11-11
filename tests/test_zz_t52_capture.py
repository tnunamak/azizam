import json
from pathlib import Path
import proxy, route_graph

def test_capture():
    proxy.app.openapi_schema = None
    live = proxy.app.openapi()
    frozen = json.loads((Path(__file__).parent / "fixtures" / "headless_openapi.json").read_text())
    moved = {k: (live[k] == frozen.get(k)) for k in live}
    paths_added = sorted(set(live["paths"]) - set(frozen["paths"]))
    paths_changed = sorted(p for p in frozen["paths"] if live["paths"].get(p) != frozen["paths"][p])
    comp_added = sorted(set(live["components"]["schemas"]) - set(frozen["components"]["schemas"]))
    comp_changed = sorted(k for k in frozen["components"]["schemas"] if live["components"]["schemas"].get(k) != frozen["components"]["schemas"][k])
    other_comp = {k: live["components"][k] == frozen["components"][k] for k in frozen["components"] if k != "schemas"}
    new = live["paths"].get("/admin/api/engine-protocols/{protocol_id}")
    assert False, json.dumps({"moved": moved, "paths_added": paths_added, "paths_changed": paths_changed, "comp_added": comp_added,
        "comp_changed": comp_changed, "other_comp": other_comp, "new": new, "digest_live": route_graph.openapi_digest(proxy.app)}, indent=1, sort_keys=True)
