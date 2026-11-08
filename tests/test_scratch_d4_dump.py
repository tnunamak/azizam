import json, os
from pathlib import Path
import proxy, route_graph
from tests.test_content_access_inventory import _event_writers, _observed
OUT = Path("/home/tnunamak/.tmp/d4-dump"); 

def test_dump():
    OUT.mkdir(parents=True, exist_ok=True)
    pol = route_graph.policy(proxy)
    (OUT/"policy.json").write_text(json.dumps(pol, indent=1, sort_keys=True))
    proxy.app.openapi_schema = None
    (OUT/"openapi.json").write_text(json.dumps(proxy.app.openapi(), indent=2, sort_keys=True)+"\n")
    (OUT/"digest.txt").write_text(route_graph.openapi_digest(proxy.app))
    (OUT/"writers.json").write_text(json.dumps([w for w in _event_writers() if "job_retry_hint" in w["path"]], indent=1))
    obs = _observed()
    (OUT/"routes.json").write_text(json.dumps(obs["routes"], indent=1))
    from tests.test_headless_access import _registered_routes, _inventoried
    reg = _registered_routes(proxy.app.routes)
    (OUT/"noncontrol.json").write_text(json.dumps(sorted(list(r) for r in reg if not _inventoried(r[1])), indent=1))
    (OUT/"capture.json").write_text(json.dumps(route_graph.capture(proxy), indent=1, sort_keys=True))
