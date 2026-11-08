import json, base64
import proxy, route_graph
from tests.test_content_access_inventory import _event_writers, _observed

def emit(name, text):
    print("D4DUMP", name, base64.b64encode(text.encode()).decode())

def test_dump():
    emit("policy.json", json.dumps(route_graph.policy(proxy), indent=1, sort_keys=True))
    proxy.app.openapi_schema = None
    emit("openapi.json", json.dumps(proxy.app.openapi(), indent=2, sort_keys=True)+"\n")
    emit("digest.txt", route_graph.openapi_digest(proxy.app))
    from tests.test_headless_access import _registered_routes, _inventoried
    reg = _registered_routes(proxy.app.routes)
    emit("noncontrol.json", json.dumps(sorted(list(r) for r in reg if not _inventoried(r[1])), indent=1))
    emit("writers.json", json.dumps(_event_writers(), indent=1))
    emit("routes.json", json.dumps(_observed()["routes"], indent=1))
    graph = sorted((path, method) for route in proxy.app.routes
                   for path in [getattr(route, "path", "")]
                   for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
                   if path.startswith(("/v1/", "/openai/", "/anthropic/", "/ollama/", "/sdapi/", "/mcp")))
    from tests.test_engine_contract_oracle import _digest
    emit("oracle.txt", _digest(graph))
