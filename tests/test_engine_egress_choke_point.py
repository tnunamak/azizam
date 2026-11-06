"""T1.1 egress, the choke point: every HTTP request to an engine is checked against a serving lease when it is sent.

``gateway/engine_egress.py`` gives the gateway's only httpx clients. Each carries a request hook that maps the
destination host and port to a transition-enabled residency domain (a map projected from the registry at each
publish) and refuses the request, unsent, unless the sending task holds a lease for that domain.

Part one drives the hook with real httpx clients over a fake transport. Part two is a static scan of
gateway/*.py and every module proxy.py imports (within the repo, transitively): no module may refer to an httpx
client class (a call, a default argument, a from-import or an alias), build an httpx transport, or open any other
HTTP or websocket connection, except through the factory or at a single site named below with its reason. The per-route guards in
tests/test_transition_gate_egress.py are the second layer.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import proxy
from gateway import engine_egress, transition_gate
from gateway.engine_protocol.transport import HttpxTransport, WireRequest
from gateway.engine_protocol.types import Timeouts
from gateway.transition_gate import TransitionUnavailable
from tests.support.fake_upstream import HandlerTransport

ENABLED = {"enabled": True, "activation": "pass_through"}
ENGINE = "http://127.0.0.1:59123"
OTHER = "http://127.0.0.1:59124"


def _record(port: int, *, enabled: bool = True, domain: str | None = None, **extra) -> dict:
    residency = {**({"domain": domain} if domain else {}), **({"transition": dict(ENABLED)} if enabled else {})}
    return {"enabled": True, "host": "127.0.0.1", "port": port, "residency": residency, **extra}


@pytest.fixture
def published(monkeypatch):
    """The map as the registry publishes it: one enabled domain at ENGINE, one engine outside any enabled domain."""
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({
        "engine": _record(59123, domain="dom", health_endpoint="/healthz",
                          observations={"loaded": {"http": {"path": "/api/ps"}}},
                          context_window_probe={"endpoint": "/v1/models"}),
        "plain": _record(59124, enabled=False)})
    token = transition_gate._EGRESS_LEASES.set(())
    yield
    transition_gate._EGRESS_LEASES.reset(token)


def _lease(domain: str, *, released: bool = False) -> SimpleNamespace:
    return SimpleNamespace(key=domain, released=released)


class Wire:
    """What reached the transport, and with which extensions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = HandlerTransport(self.answer)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": f"{ENGINE}/landed"})
        return httpx.Response(200, json={"ok": True})


def _send(client, method: str, url: str, **kwargs):
    return asyncio.run(_async_send(client, method, url, **kwargs))


async def _async_send(client, method: str, url: str, **kwargs):
    async with client:
        return await client.request(method, url, **kwargs)


# -- the hook ------------------------------------------------------------------------------------------


def test_a_request_to_an_enabled_domain_without_a_lease_is_refused_before_the_transport(published) -> None:
    wire = Wire()
    with pytest.raises(TransitionUnavailable) as refused:
        _send(engine_egress.async_client(transport=wire.transport), "POST", f"{ENGINE}/v1/chat/completions", json={})
    assert refused.value.reason == "path_not_leased" and refused.value.domain == "dom"
    assert wire.requests == []


def test_a_request_with_a_lease_for_the_domain_goes_out(published) -> None:
    transition_gate._EGRESS_LEASES.set((_lease("dom"),))
    wire = Wire()
    assert _send(engine_egress.async_client(transport=wire.transport), "POST", f"{ENGINE}/v1/x", json={}).status_code == 200
    assert len(wire.requests) == 1


@pytest.mark.parametrize("lease", [_lease("other-domain"), _lease("dom", released=True)], ids=["other_domain", "released"])
def test_a_lease_for_another_domain_or_a_released_one_does_not_count(published, lease) -> None:
    transition_gate._EGRESS_LEASES.set((lease,))
    wire = Wire()
    with pytest.raises(TransitionUnavailable):
        _send(engine_egress.async_client(transport=wire.transport), "POST", f"{ENGINE}/v1/x", json={})
    assert wire.requests == []


def test_a_destination_outside_every_enabled_domain_is_never_refused(published) -> None:
    wire = Wire()
    for url in (OTHER, "http://127.0.0.1:59999", "https://api.example.com"):
        assert _send(engine_egress.async_client(transport=wire.transport), "POST", f"{url}/v1/x", json={}).status_code == 200
    assert len(wire.requests) == 3


def test_the_sync_client_is_checked_too(published) -> None:
    wire = Wire()
    with pytest.raises(TransitionUnavailable):
        with engine_egress.client(transport=wire.transport) as client:
            client.post(f"{ENGINE}/x", json={})
    assert wire.requests == []


def test_a_long_lived_client_is_checked_at_every_send_not_when_it_was_built(published) -> None:
    wire = Wire()

    async def scenario() -> list[str]:
        outcomes = []
        async with engine_egress.async_client(transport=wire.transport) as client:
            token = transition_gate._EGRESS_LEASES.set((_lease("dom"),))
            outcomes.append((await client.post(f"{ENGINE}/a", json={})).text and "sent")
            transition_gate._EGRESS_LEASES.reset(token)
            try:
                await client.post(f"{ENGINE}/b", json={})
            except TransitionUnavailable:
                outcomes.append("refused")
        return outcomes

    assert asyncio.run(scenario()) == ["sent", "refused"]
    assert [request.url.path for request in wire.requests] == ["/a"]


def test_a_redirect_into_an_enabled_domain_is_checked_at_the_hop(published) -> None:
    wire = Wire()
    with pytest.raises(TransitionUnavailable):
        _send(engine_egress.async_client(transport=wire.transport, follow_redirects=True), "GET", f"{OTHER}/hop")
    assert [request.url.path for request in wire.requests] == ["/hop"], "the first hop went out, the landing did not"


# -- the probe mark ------------------------------------------------------------------------------------


def test_a_probe_get_needs_no_lease_and_the_mark_never_reaches_the_transport(published) -> None:
    wire = Wire()
    response = _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}/health",
                     extensions=engine_egress.probe())
    assert response.status_code == 200
    assert engine_egress.PROBE_EXTENSION not in wire.requests[0].extensions


def test_a_probe_mark_covers_only_the_paths_the_record_declares(published) -> None:
    """Some engines change state on a GET (llama-swap ``GET /unload``): the mark on an undeclared path exempts nothing.
    A path counts only when it equals a declared path: llama-swap starts a model on ``GET /upstream/<model>/<anything>``,
    so ``/upstream/m/health`` is refused although it ends with ``/health``. Failed before the fix: any marked GET
    passed, then any path that ended with a declared one."""
    wire = Wire()
    for path in ("/healthz", "/health", "/api/ps", "/v1/models"):
        assert _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}{path}",
                     extensions=engine_egress.probe()).status_code == 200
    sent = len(wire.requests)
    for path in ("/unload", "/running/../unload", "/v1/chat/completions", "/healthz/unload", "/",
                 "/upstream/m/health", "/upstream/m/healthz", "/health/", "/x/health"):
        with pytest.raises(TransitionUnavailable):
            _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}{path}", extensions=engine_egress.probe())
    assert len(wire.requests) == sent, "a refused GET is not sent"
    transition_gate._EGRESS_LEASES.set((_lease("dom"),))
    assert _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}/unload",
                 extensions=engine_egress.probe()).status_code == 200, "with a lease any request goes"


def test_a_probe_mark_covers_the_declared_path_under_the_records_own_url_prefix(monkeypatch) -> None:
    """A record whose URL is ``http://h:port/v1`` is read at ``/v1/health``: its own prefix plus a declared path."""
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({"engine": {"enabled": True, "base_url": f"{ENGINE}/v1/", "health_endpoint": "/healthz",
                                      "residency": {"domain": "dom", "transition": dict(ENABLED)}}})
    token = transition_gate._EGRESS_LEASES.set(())
    try:
        wire = Wire()
        for path in ("/healthz", "/health", "/v1/healthz", "/v1/health"):
            assert _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}{path}",
                         extensions=engine_egress.probe()).status_code == 200
        for path in ("/v2/health", "/v1/v1/unload", "/v1/unload", "/upstream/m/v1/health"):
            with pytest.raises(TransitionUnavailable):
                _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}{path}", extensions=engine_egress.probe())
    finally:
        transition_gate._EGRESS_LEASES.reset(token)


def test_the_declared_paths_are_read_from_the_record_and_its_offerings() -> None:
    record = {"health_endpoint": "http://h:1/status?token=x", "config": {"context_window_probe": {"endpoint": "/props"}},
              "offerings": [{"health_endpoint": "/o-health", "observations": {"x": {"http": {"path": "/o-obs"}}}}],
              "lifecycle": {"driver_config": {"verify_endpoint": "/v1/model"}}}
    assert engine_egress.declared_probe_paths(record) == {"/health", "/status", "/props", "/o-health", "/o-obs", "/v1/model"}
    assert engine_egress.declared_probe_paths({}) == {"/health"}


def test_a_probe_mark_on_anything_but_get_does_not_exempt_the_request(published) -> None:
    wire = Wire()
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        with pytest.raises(TransitionUnavailable):
            _send(engine_egress.async_client(transport=wire.transport), method, f"{ENGINE}/v1/x", extensions=engine_egress.probe())
    assert wire.requests == []


def test_an_unmarked_get_is_not_a_probe(published) -> None:
    wire = Wire()
    with pytest.raises(TransitionUnavailable):
        _send(engine_egress.async_client(transport=wire.transport), "GET", f"{ENGINE}/health")


def test_each_probe_call_gets_its_own_mark() -> None:
    first = engine_egress.probe()
    first.clear()
    assert engine_egress.probe() == {engine_egress.PROBE_EXTENSION: True}


# -- the adapter transport -----------------------------------------------------------------------------


def test_the_protocol_transport_refuses_an_unleased_send_to_an_enabled_domain(published) -> None:
    """The adapter's own client (``HttpxTransport.send``) goes through the factory."""
    wire = Wire()
    transport = HttpxTransport.for_tests(wire.transport)
    request = WireRequest("POST", f"{ENGINE}/prefix/api", {}, b"{}")
    with pytest.raises(TransitionUnavailable):
        asyncio.run(transport.send(request, Timeouts()))
    assert wire.requests == []
    transition_gate._EGRESS_LEASES.set((_lease("dom"),))
    sent = asyncio.run(transport.send(request, Timeouts()))
    assert sent.status == 200 and len(wire.requests) == 1


# -- the map -------------------------------------------------------------------------------------------


def test_the_map_has_every_destination_of_an_enabled_domain_and_none_of_another(monkeypatch) -> None:
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({
        "a": _record(8001, domain="shared"),
        "b": {"enabled": True, "config": {"base_url": "https://b.example:8443/v1"}, "residency": {"domain": "shared"}},
        "c": {"url": "http://localhost:8003/x", "residency": {"domain": "solo", "transition": dict(ENABLED)}},
        "off": _record(8004, enabled=False),
    })
    mapped = engine_egress.mapped_destinations()
    # b does not enable transition itself, but its domain is enabled by a, so it is leased too.
    assert mapped["127.0.0.1:8001"] == ("shared",)
    assert mapped["b.example:8443"] == ("shared",)
    assert mapped["127.0.0.1:8003"] == ("solo",), "localhost and 127.0.0.1 are one destination"
    assert "127.0.0.1:8004" not in mapped


def test_a_destination_shared_by_two_enabled_domains_is_satisfied_by_either_lease(monkeypatch) -> None:
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({"a": _record(8010, domain="one"), "b": _record(8010, domain="two")})
    assert engine_egress.mapped_destinations()["127.0.0.1:8010"] == ("one", "two")
    token = transition_gate._EGRESS_LEASES.set((_lease("two"),))
    try:
        engine_egress.check_request(httpx.Request("POST", "http://127.0.0.1:8010/x"))
    finally:
        transition_gate._EGRESS_LEASES.reset(token)


def test_a_publish_replaces_the_map(monkeypatch) -> None:
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({"a": _record(8020)})
    engine_egress.publish({"b": _record(8021)})
    assert set(engine_egress.mapped_destinations()) == {"127.0.0.1:8021"}
    engine_egress.publish(None)
    assert not engine_egress.mapped_destinations()


def _function(name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(ast.parse(Path(proxy.__file__).read_text()))
                if isinstance(node, ast.FunctionDef) and node.name == name)


@pytest.mark.parametrize("name", ["_publish_runtime_config", "_publish_boot_snapshot"])
def test_the_map_goes_live_before_the_config_snapshot(name) -> None:
    """The boot and the reload install the egress map, then publish the snapshot that enables the domain."""
    lines = {ast.unparse(node.func): node.lineno for node in ast.walk(_function(name)) if isinstance(node, ast.Call)}
    assert "_project_egress" in lines
    assert lines["engine_egress.install"] < lines["CONFIG_HOME.publish"]


class _Abort(Exception):
    pass


def _candidate_with_an_enabled_domain() -> dict:
    import copy

    candidate = copy.deepcopy(dict(proxy.config))
    candidate["backends"] = {**candidate.get("backends", {}), "t1-probe": _record(59130, domain="t1-dom")}
    return candidate


def test_a_reload_publishes_the_map_before_the_snapshot(monkeypatch) -> None:
    """A request that binds the new snapshot must find its domain in the map: at the snapshot's publish the new map is live.
    Failed before the fix: the map was swapped after the snapshot, so a new config met the old map."""
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    seen: list = []

    class Home:
        def __getattr__(self, name):
            return getattr(real, name)

        def publish(self, snapshot):
            seen.append(dict(engine_egress.mapped_destinations()))
            raise _Abort

    real = proxy.CONFIG_HOME
    monkeypatch.setattr(proxy, "CONFIG_HOME", Home())
    with pytest.raises(_Abort):
        proxy._publish_runtime_config(_candidate_with_an_enabled_domain())
    assert seen and seen[0].get("127.0.0.1:59130") == ("t1-dom",)


def test_a_reload_whose_projection_raises_publishes_nothing(monkeypatch) -> None:
    """The projection is built before anything goes live: when it raises, the old config and the old map stay."""
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish({"old": _record(59131, domain="old-dom")})
    old_map, old_config, old_snapshot = engine_egress.mapped_destinations(), proxy.config, proxy.CONFIG_HOME.holder.current()

    def broken(_backends):
        raise _Abort

    monkeypatch.setattr(proxy, "_project_egress", broken)
    with pytest.raises(_Abort):
        proxy._publish_runtime_config(_candidate_with_an_enabled_domain())
    assert engine_egress.mapped_destinations() is old_map
    assert proxy.config is old_config
    assert proxy.CONFIG_HOME.holder.current() is old_snapshot


def test_a_reader_never_sees_the_destinations_of_one_projection_with_the_probe_paths_of_another() -> None:
    """``install`` swaps one reference, so a check on another thread sees one whole projection.

    Projection one maps the address to domain ``dom-one`` and declares ``/probe-one``; projection two maps it to
    ``dom-two`` and declares ``/probe-two``. In either whole projection, a marked GET of ``/probe-one`` is allowed or
    refused for ``dom-two``, and a marked GET of ``/probe-two`` is allowed or refused for ``dom-one``. A refusal for
    ``dom-one`` on ``/probe-one`` (or for ``dom-two`` on ``/probe-two``) can come only from a pair of one projection's
    destinations with the other's probe paths.
    Failed when ``install`` assigned the destinations and the probe paths one after the other."""
    import sys
    import threading

    key = "127.0.0.1:59140"
    one = engine_egress.project({"one": _record(59140, domain="dom-one", health_endpoint="/probe-one")})
    two = engine_egress.project({"two": _record(59140, domain="dom-two", health_endpoint="/probe-two")})
    assert one.destinations[key] == ("dom-one",) and "/probe-one" in one.probe_paths[key]
    assert two.destinations[key] == ("dom-two",) and "/probe-two" in two.probe_paths[key]
    engine_egress.install(one)
    stop = threading.Event()

    def install_in_a_loop() -> None:
        while not stop.is_set():
            engine_egress.install(two)
            engine_egress.install(one)

    def refused_for(path: str) -> str | None:
        request = httpx.Request("GET", f"http://127.0.0.1:59140{path}", extensions=engine_egress.probe())
        try:
            engine_egress.check_request(request)
        except TransitionUnavailable as exc:
            return exc.domain
        return None

    mixed: list[tuple[str, str | None]] = []
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    installer = threading.Thread(target=install_in_a_loop, daemon=True)
    installer.start()
    try:
        for _ in range(20000):
            if (domain := refused_for("/probe-one")) == "dom-one":
                mixed.append(("/probe-one", domain))
            if (domain := refused_for("/probe-two")) == "dom-two":
                mixed.append(("/probe-two", domain))
    finally:
        stop.set()
        installer.join(timeout=10)
        sys.setswitchinterval(interval)
    assert not installer.is_alive()
    assert mixed == []


def test_a_service_whose_config_is_not_a_mapping_is_projected_without_error() -> None:
    """``state_paths`` read ``.get`` on ``record["config"]`` when it was truthy: a string there raised AttributeError."""
    record = {**_record(59132, domain="dom"), "config": "not-a-mapping"}
    projection = proxy._project_egress({"odd": record})
    assert projection.destinations["127.0.0.1:59132"] == ("dom",)
    assert "/v1/models" in projection.probe_paths["127.0.0.1:59132"]


# -- the scan: nothing reaches an engine except through the factory ---------------------------------

_ROOT = Path(proxy.__file__).resolve().parent
_FACTORY = "gateway/engine_egress.py"


def _relative(path: Path) -> str:
    try:
        return path.relative_to(_ROOT).as_posix()
    except ValueError:
        return path.name


def _module_file(dotted: str) -> Path | None:
    base = _ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_files(path: Path) -> set[Path]:
    """The repo modules one file imports (anywhere in it, relative imports included)."""
    found: set[Path] = set()
    package = _relative(path).rsplit("/", 1)[0].replace("/", ".") if "/" in _relative(path) else ""
    for node in ast.walk(ast.parse(path.read_text())):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                module = ".".join([*parts, *([module] if module else [])])
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        for name in names:
            file = _module_file(name) if name else None
            if file is not None:
                found.add(file)
    return found


def _import_closure(entry: Path) -> set[Path]:
    seen, queue = {entry}, [entry]
    while queue:
        for file in _imported_files(queue.pop()):
            if file not in seen:
                seen.add(file)
                queue.append(file)
    return seen


# Every module the gateway can load: the gateway package, plus whatever proxy.py imports (status_endpoint.py and
# the like sit outside the package).
_MODULES = sorted({*(_ROOT / "gateway").rglob("*.py"), *_import_closure(_ROOT / "proxy.py")})

# Single client sites, keyed by (file, outermost function, the exact expression), each with its reason. An
# exemption names one site, never a whole function, and the in-process ones are checked below to sit over an ASGI
# transport.
IN_PROCESS_CLIENTS = {
    ("gateway/home_api.py", "create_home_router",
     "httpx.AsyncClient(transport=transport, base_url='http://connection-test.invalid')"):
        "ASGITransport onto the gateway app",
    ("proxy.py", "_mcp_internal_call",
     "httpx.AsyncClient(transport=_mcp_transport(request), base_url='http://gateway.internal', timeout=None)"):
        "ASGITransport onto the gateway app",
    ("proxy.py", "_mcp_dispatch_call",
     "httpx.AsyncClient(transport=_mcp_transport(request), base_url='http://gateway.internal', timeout=None)"):
        "ASGITransport onto the gateway app",
    ("proxy.py", "_operator_stt_transcribe",
     "httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=None), base_url='http://gateway.internal', timeout=None)"):
        "ASGITransport onto the gateway app",
}
# The only place a transport is built for a client the factory then wraps with the hook.
TRANSPORT_BUILDERS = {
    ("gateway/engine_check.py", "checked_transport", "httpx.AsyncHTTPTransport(retries=0)"):
        "the address-checked transport; every client over it comes from the factory",
}
# Not an httpx request and not to an engine.
OTHER_HTTP = {
    ("proxy.py", "_init_cloudflare_ip_ranges", "urllib.request.urlopen(url, timeout=10)"): "Cloudflare's published ranges, not an engine",
}
_HTTPX_CLIENTS = {"AsyncClient", "Client"}
_HTTPX_TRANSPORTS = {"AsyncHTTPTransport", "HTTPTransport"}
_HTTPX_SHORTCUTS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
_OTHER_HTTP = {"urllib.request.urlopen", "urlopen", "aiohttp.ClientSession", "requests.get", "requests.post", "requests.request",
               "requests.Session", "http.client.HTTPConnection", "http.client.HTTPSConnection"}
_WEBSOCKETS = {"ws_connect", "websockets.connect", "websockets.asyncio.client.connect", "websockets.client.connect"}


def _call_name(node: ast.Call) -> str:
    target, parts = node.func, []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


class _Module:
    """One parsed module: parents, the outermost function of each node, the httpx names it binds."""

    def __init__(self, path: Path, source: str | None = None) -> None:
        self.relative = _relative(path)
        self.tree = ast.parse(path.read_text() if source is None else source)
        self.parent: dict[ast.AST, ast.AST] = {}
        self.owner: dict[ast.AST, str] = {}
        self.annotation: set[ast.AST] = set()

        def visit(node: ast.AST, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
                name = owner
                if not owner and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                self.owner[child] = name or "<module>"
                visit(child, name)

        visit(self.tree, "")
        for node in ast.walk(self.tree):
            notes = []
            if isinstance(node, ast.arg) and node.annotation is not None:
                notes.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                notes.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                notes.append(node.annotation)
            for note in notes:
                self.annotation.update(ast.walk(note))
        self.httpx_names = {"httpx"}
        self.from_httpx: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                self.httpx_names.update(alias.asname or alias.name for alias in node.names if alias.name == "httpx")
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                self.from_httpx.update({alias.asname or alias.name: alias.name for alias in node.names})

    def site(self, node: ast.AST) -> str:
        parent = self.parent.get(node)
        return ast.unparse(parent if isinstance(parent, ast.Call) and parent.func is node else node)

    def httpx_references(self, attributes: set[str]) -> list[tuple[ast.AST, str, str]]:
        """(node, owner, site text) of each value reference to ``httpx.<attribute>`` (annotations are not values)."""
        found = []
        for node in ast.walk(self.tree):
            if node in self.annotation:
                continue
            hit = False
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                hit = node.value.id in self.httpx_names and node.attr in attributes
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                hit = self.from_httpx.get(node.id) in attributes
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                hit = any(alias.name in attributes or alias.name == "*" for alias in node.names)
            elif isinstance(node, ast.Call) and _call_name(node) == "getattr" and len(node.args) == 2:
                target, name = node.args
                hit = (isinstance(target, ast.Name) and target.id in self.httpx_names
                       and isinstance(name, ast.Constant) and name.value in attributes)
            if hit:
                found.append((node, self.owner.get(node, "<module>"), self.site(node)))
        return found

    def calls(self, names: set[str]) -> list[tuple[ast.Call, str, str]]:
        return [(node, self.owner.get(node, "<module>"), ast.unparse(node)) for node in ast.walk(self.tree)
                if isinstance(node, ast.Call) and _call_name(node) in names]


def _parsed(modules: list[Path] | None = None) -> list[_Module]:
    return [_Module(path) for path in (_MODULES if modules is None else modules) if _relative(path) != _FACTORY]


def _unlisted_httpx(attributes: set[str], allowed: dict, modules: list[_Module]) -> list[str]:
    return [f"{module.relative}:{node.lineno} {owner} refers to httpx: {site}"
            for module in modules for node, owner, site in module.httpx_references(attributes)
            if (module.relative, owner, site) not in allowed]


def _unlisted(names: set[str], allowed: dict, modules: list[_Module]) -> list[str]:
    return [f"{module.relative}:{node.lineno} {owner} builds {_call_name(node)}"
            for module in modules for node, owner, site in module.calls(names)
            if (module.relative, owner, site) not in allowed]


def _by_relative(relative: str) -> _Module:
    return next(module for module in _parsed() if module.relative == relative)


def test_the_scan_covers_every_module_proxy_imports_not_only_the_gateway_package() -> None:
    relative = {_relative(path) for path in _MODULES}
    assert {"proxy.py", "status_endpoint.py", "gateway/engine_control.py", "gateway/engine_observations.py",
            "gateway/engine_check.py"} <= relative


def test_no_module_refers_to_an_httpx_client_class_except_through_the_factory() -> None:
    """Failed before the fix: status_endpoint.py (5 raw clients), the default argument of EngineControlRuntime and
    ObservationReader, and the whole body of create_home_router were not scanned or were exempt."""
    found = _unlisted_httpx(_HTTPX_CLIENTS, IN_PROCESS_CLIENTS, _parsed())
    assert not found, (
        "Build the client with gateway.engine_egress.async_client or .client, so every send passes the lease check. "
        f"A client over an in-process transport is listed in IN_PROCESS_CLIENTS, as one site. Found: {found}")


def test_every_exempt_client_site_exists_and_sits_over_an_asgi_transport() -> None:
    for (relative, owner, site), _reason in IN_PROCESS_CLIENTS.items():
        module = _by_relative(relative)
        match = [node for node, own, text in module.httpx_references(_HTTPX_CLIENTS) if (own, text) == (owner, site)]
        assert match, f"{relative}:{owner} no longer has the exempt site {site!r}; remove the exemption"
        call = module.parent[match[0]]
        assert isinstance(call, ast.Call)
        transport = next(keyword.value for keyword in call.keywords if keyword.arg == "transport")
        text = ast.unparse(transport)
        if isinstance(transport, ast.Name):  # a local that the same function assigned from an ASGI transport
            function = next(node for node in ast.walk(module.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name == owner)
            text = " ".join(ast.unparse(node.value) for node in ast.walk(function)
                            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == transport.id for t in node.targets))
        assert "ASGITransport" in text or "_mcp_transport" in text, f"{relative}:{owner} is listed as in-process but is not over an ASGI transport"
    for relative, owner, site in TRANSPORT_BUILDERS:
        found = {(own, text) for _n, own, text in _by_relative(relative).httpx_references(_HTTPX_TRANSPORTS)}
        assert (owner, site) in found, f"{relative}:{owner} no longer has the exempt site {site!r}; remove the exemption"
    for relative, owner, site in OTHER_HTTP:
        found = {(own, text) for _n, own, text in _by_relative(relative).calls(_OTHER_HTTP)}
        assert (owner, site) in found, f"{relative}:{owner} no longer has the exempt site {site!r}; remove the exemption"


def test_no_module_builds_an_httpx_transport_or_shortcut_call_outside_the_factory() -> None:
    assert not _unlisted_httpx(_HTTPX_TRANSPORTS, TRANSPORT_BUILDERS, _parsed())
    assert not _unlisted_httpx(_HTTPX_SHORTCUTS, {}, _parsed()), "httpx.get/post/... open a client the factory did not make"


def test_no_module_uses_another_http_library_except_the_listed_sites() -> None:
    assert not _unlisted(_OTHER_HTTP, OTHER_HTTP, _parsed())


def test_every_websocket_connection_is_a_listed_site_and_the_gateway_side_one_is_guarded() -> None:
    open_sites = [module.relative for module in _parsed() if module.calls(_WEBSOCKETS)]
    assert sorted(open_sites) == ["gateway/engine_protocol/node_graph.py", "proxy.py"], (
        "An httpx hook cannot see a websocket send. Guard the new site with _require_egress_lease and list it here.")
    proxy_module = _by_relative("proxy.py")
    guarded = {owner for _node, owner, _t in proxy_module.calls({"_require_egress_lease", "require_egress_lease"})}
    assert "_handle_realtime" in guarded
    assert [owner for _n, owner, _t in proxy_module.calls(_WEBSOCKETS)] == ["_handle_realtime"]
    # node_graph.watch: the push channel of a connection a guarded factory made; it only receives.
    assert [owner for _n, owner, _t in _by_relative("gateway/engine_protocol/node_graph.py").calls(_WEBSOCKETS)] == ["watch"]


def test_the_factory_attaches_the_hook_to_real_httpx_clients() -> None:
    assert engine_egress.check_request in engine_egress.client().event_hooks["request"]
    assert engine_egress._check_async in engine_egress.async_client().event_hooks["request"]


def test_the_two_clients_that_used_to_bypass_the_factory_default_to_it() -> None:
    """Failed before the fix: both defaulted to ``httpx.AsyncClient`` (a control POST/PUT/DELETE sent with no lease)."""
    import inspect

    from gateway.engine_control import EngineControlRuntime
    from gateway.engine_observations import ObservationReader

    for owner in (EngineControlRuntime, ObservationReader):
        assert inspect.signature(owner.__init__).parameters["client_factory"].default is engine_egress.async_client


def test_the_scan_catches_every_spelling_of_a_raw_client(tmp_path) -> None:
    """Negative control: a call, a default argument, an alias, a from-import and getattr are all reported; an
    annotation is not."""
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "import httpx\nimport httpx as hx\nfrom httpx import AsyncClient as AC\nfrom httpx import Client\n\n"
        "async def send(factory=httpx.AsyncClient):\n    async with httpx.AsyncClient() as c:\n        await c.post('http://x')\n\n"
        "def aliased():\n    return hx.Client()\n\n"
        "def imported():\n    return AC()\n\n"
        "def indirect():\n    return getattr(httpx, 'AsyncClient')\n\n"
        "def annotated(client: httpx.AsyncClient) -> httpx.Client:\n    return client\n\n"
        "async def listen():\n    await ws_connect('ws://x')\n")
    source = rogue.read_text().splitlines()
    line = {text.strip(): number for number, text in enumerate(source, 1)}
    indirect = "return getattr(httpx, 'AsyncClient')"
    listening = "await ws_connect('ws://x')"
    found = sorted(_unlisted_httpx(_HTTPX_CLIENTS, {}, _parsed([rogue])), key=lambda text: int(text.split(":")[1].split()[0]))
    assert [item.split(" refers")[0] for item in found] == [
        f"rogue.py:{line['from httpx import AsyncClient as AC']} <module>", f"rogue.py:{line['from httpx import Client']} <module>",
        f"rogue.py:{line['async def send(factory=httpx.AsyncClient):']} send", f"rogue.py:{line['async with httpx.AsyncClient() as c:']} send",
        f"rogue.py:{line['return hx.Client()']} aliased", f"rogue.py:{line['return AC()']} imported",
        f"rogue.py:{line[indirect]} indirect"]
    assert _unlisted(_WEBSOCKETS, {}, _parsed([rogue])) == [f"rogue.py:{line[listening]} listen builds ws_connect"]
