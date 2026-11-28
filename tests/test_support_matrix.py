"""Pin documentation to the real registered route inventory and rejection markers."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import proxy

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gen_support_matrix", ROOT / "scripts/gen_support_matrix.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_emit_support_matrix():
    print(matrix.BEGIN)
    print(matrix.render(proxy.app), end="")
    print(matrix.END)


def test_support_matrix_is_current():
    assert (ROOT / "docs/SUPPORT_MATRIX.md").read_text() == matrix.render(proxy.app), (
        "Commit the route changes, run python3 scripts/gen_support_matrix.py --write, "
        "then commit the updated matrix."
    )


def test_rejections_and_websocket_are_honest():
    routes = {(route.path, method): route for route in matrix.public_routes(proxy.app)
              for method in (getattr(route, "methods", None) or {"WS"})}
    assert matrix.classification(routes[("/api/pull", "POST")]) == (
        "unsupported", "501 after authentication and policy checks")
    assert matrix.classification(routes[("/sdapi/v1/img2img", "POST")])[0] == "unsupported"
    assert matrix.classification(routes[("/v1/assistants{rest:path}", "POST")])[1].startswith("400")
    assert matrix.classification(routes[("/mcp", "GET")])[1].startswith("405")
    assert matrix.classification(routes[("/v1/responses/input_tokens", "POST")])[0] == "unsupported"
    assert matrix.classification(routes[("/v1/responses/{response_id}/cancel", "POST")])[0] == "unsupported"
    assert matrix.classification(routes[("/v1/realtime", "WS")])[0] == "partial"
    assert ("/gateway/v1/capabilities", "GET") in routes


def test_wrapped_router_inventory():
    endpoint = lambda: None
    leaf = SimpleNamespace(path="/test", endpoint=endpoint)
    wrapper = SimpleNamespace(original_router=SimpleNamespace(routes=[leaf]))
    assert list(matrix.public_routes(SimpleNamespace(routes=[wrapper]))) == [leaf]
