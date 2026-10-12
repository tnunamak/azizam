"""Ratchet: test files that fake an HTTP upstream on their own (TEST_STRATEGY 6).

New upstream fakes belong in tests/support/fake_upstream/. This count may
only fall; when a migration lowers it, lower the ceiling in the same change.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_TESTS = Path(__file__).parent
_FK = _TESTS / "support" / "fake_upstream"
# Names that stand in for an upstream over HTTP, found through imports and
# attribute access so an alias (`from httpx import MockTransport as MT`) still counts.
_OWN_SERVER_NAMES = {
    "HTTPServer", "ThreadingHTTPServer", "BaseHTTPRequestHandler", "socketserver", "start_server", "start_unix_server",
}
_UVICORN_NAMES = {"Server", "Config", "run"}
_ASGI_TRANSPORTS = {"ASGITransport", "StreamingASGITransport"}
_APP_FACTORIES = {"FastAPI", "Starlette", "Flask", "Quart"}
# A local app that imports none of these is a stand-alone fake. One that does
# is the gateway (or a router of it) hosted for a test.
_GATEWAY_MODULES = {"proxy", "gateway", "routes", "main"}

# Files that match a mechanism but serve real gateway code, not a fake upstream.
_NOT_FAKES = {
    "tests/compat/python/macro_ux_fixture.py": "serves the real proxy app over uvicorn for the UX fixture",
    "tests/test_decision_engine_server.py": "serves the real decision engine Handler under test",
    "tests/test_provider_missing_keys_live.py": "serves the real provider router over TCP",
    "tests/test_route_policy_inventory.py": "patches AsyncClient as a no-network tripwire",
}


def _mechanisms(tree: ast.AST) -> list[str]:
    """Which ways this module stands in for an HTTP upstream (empty: none)."""
    kinds: set[str] = set()
    imported: set[str] = set()
    local_apps: set[str] = set()
    asgi_apps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            imported.add(module)
            for alias in node.names:
                if alias.name == "MockTransport":
                    kinds.add("mock_transport")
                if alias.name in _OWN_SERVER_NAMES or (module == "aiohttp" and alias.name == "web"):
                    kinds.add("own_server")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
                if alias.name.split(".")[0] in {"socketserver", "http"} and alias.name != "http":
                    kinds.add("own_server")
        elif isinstance(node, ast.Attribute):
            if node.attr == "MockTransport":
                kinds.add("mock_transport")
            elif node.attr in _OWN_SERVER_NAMES - {"socketserver"}:
                kinds.add("own_server")
            elif isinstance(node.value, ast.Name) and node.value.id == "uvicorn" and node.attr in _UVICORN_NAMES:
                kinds.add("own_server")
            elif node.attr == "AsyncClient" and isinstance(node.value, ast.Name) and node.value.id == "httpx":
                pass
        elif isinstance(node, ast.Name):
            if node.id == "MockTransport":
                kinds.add("mock_transport")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"patch", "setattr", "object"} and _patches_async_client(node):
                kinds.add("patched_async_client")
            if name in _ASGI_TRANSPORTS:
                for arg in [*node.args[:1], *(kw.value for kw in node.keywords if kw.arg == "app")]:
                    if isinstance(arg, ast.Name):
                        asgi_apps.add(arg.id)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _APP_FACTORIES:
                local_apps.update(t.id for t in node.targets if isinstance(t, ast.Name))
    if asgi_apps & local_apps and not imported & _GATEWAY_MODULES:
        kinds.add("local_asgi_app")
    return sorted(kinds)


def _patches_async_client(call: ast.Call) -> bool:
    """patch("x.httpx.AsyncClient"), patch.object(httpx, "AsyncClient"), setattr(httpx, "AsyncClient", ...)."""
    args = [ast.unparse(arg).strip("'\"") for arg in call.args[:2]]
    if args and args[0].endswith("httpx.AsyncClient"):
        return True
    return len(args) == 2 and "httpx" in args[0] and args[1] == "AsyncClient" or (
        len(args) == 2 and args[1] == "AsyncClient" and isinstance(call.args[1], ast.Constant)
    )


def ad_hoc_fake_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_TESTS.rglob("*.py")):
        name = path.relative_to(_TESTS.parent).as_posix()
        if path.is_relative_to(_FK) or path == Path(__file__) or path.name == "_hermetic.py" or name in _NOT_FAKES:
            continue
        kinds = _mechanisms(ast.parse(path.read_text(encoding="utf-8"), filename=name))
        if kinds:
            found[name] = kinds
    return found


def test_ad_hoc_upstream_fakes_only_fall() -> None:
    ceiling = tomllib.loads((_TESTS / "budgets.toml").read_text(encoding="utf-8"))["ratchets"]["ad_hoc_upstream_fake_files"]
    found = ad_hoc_fake_files()
    listing = "\n".join(f"  {name}: {', '.join(kinds)}" for name, kinds in found.items())
    assert len(found) <= ceiling, (
        f"{len(found)} test files fake an upstream on their own (ceiling {ceiling}). "
        f"Serve the upstream from tests/support/fake_upstream/ instead:\n{listing}"
    )
    assert len(found) == ceiling, (
        f"The count fell to {len(found)}: lower [ratchets] ad_hoc_upstream_fake_files "
        f"in tests/budgets.toml from {ceiling} to {len(found)}.\n{listing}"
    )


def _kinds(source: str) -> list[str]:
    return _mechanisms(ast.parse(source))


def test_the_scanner_sees_every_way_a_test_fakes_an_upstream() -> None:
    assert _kinds("from httpx import MockTransport as MT\nclient = MT(handler)") == ["mock_transport"]
    assert _kinds("import httpx\nhttpx.AsyncClient(transport=httpx.MockTransport(h))") == ["mock_transport"]
    assert _kinds("from asyncio import start_server\nasync def f(): await start_server(h, '127.0.0.1', 0)") == ["own_server"]
    assert _kinds("from http.server import HTTPServer\nHTTPServer(('', 0), H)") == ["own_server"]
    assert _kinds("import uvicorn\nuvicorn.Server(uvicorn.Config(app))") == ["own_server"]
    assert _kinds("from unittest.mock import patch\nwith patch.object(proxy.httpx, 'AsyncClient', Fake): pass") == ["patched_async_client"]
    assert _kinds("monkeypatch.setattr(httpx, 'AsyncClient', Fake)") == ["patched_async_client"]
    assert _kinds("with patch('proxy.httpx.AsyncClient', Fake): pass") == ["patched_async_client"]
    local_fake = (
        "import httpx\nfrom fastapi import FastAPI\napp = FastAPI()\n"
        "client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))"
    )
    assert _kinds(local_fake) == ["local_asgi_app"]


def test_the_scanner_ignores_the_gateway_under_test() -> None:
    hosted = (
        "import httpx\nimport proxy\nfrom fastapi import FastAPI\napp = FastAPI()\n"
        "httpx.AsyncClient(transport=httpx.ASGITransport(app=app))"
    )
    assert _kinds(hosted) == []
    assert _kinds("import httpx\nhttpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app))") == []
    assert _kinds("from fastapi.testclient import TestClient\nTestClient(app)") == []
