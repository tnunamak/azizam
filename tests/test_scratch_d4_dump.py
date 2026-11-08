import json, os
from pathlib import Path
import proxy, route_graph
from tests.test_content_access_inventory import _event_writers, _observed
import base64

def emit(name, text):
    print("D4DUMP", name, base64.b64encode(text.encode()).decode())

def test_dump():
    pol = route_graph.policy(proxy)
    emit("policy.json", json.dumps(pol, indent=1, sort_keys=True))
    proxy.app.openapi_schema = None
    emit("openapi.json", json.dumps(proxy.app.openapi(), indent=2, sort_keys=True)+"\n")
    emit("digest.txt", route_graph.openapi_digest(proxy.app))
    emit("writers.json", json.dumps([w for w in _event_writers() if "job_retry_hint" in w["path"]], indent=1))
    obs = _observed()
    emit("routes.json", json.dumps(obs["routes"], indent=1))
    from tests.test_headless_access import _registered_routes, _inventoried
    reg = _registered_routes(proxy.app.routes)
    emit("noncontrol.json", json.dumps(sorted(list(r) for r in reg if not _inventoried(r[1])), indent=1))
    emit("capture.json", json.dumps(route_graph.capture(proxy), indent=1, sort_keys=True))
