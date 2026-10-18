"""The oracle of decisions D26 to D32, write side: no admin or member route that changes something answers with a canary.

``test_admin_secret_sweep.py`` reads every GET route. A route that writes can answer with registry content too (a
delete that returns the whole registry), so this file calls every POST, PUT, PATCH and DELETE route under ``/admin/api``
and ``/me/api``, found by walking the route table of the exported app, as an administrator, the owner and a signed-out
client. Each call has its own fresh seed, so a delete cannot remove what the next call needs. A call that has a built
body (an echo of what the page was shown, a command, a credentialed package source) runs beside three generic ones (no
body, an empty object, malformed JSON), and success and error answers are both searched for the canaries.

Where a call is built, the owner's call must succeed: a clean answer from a route that only refused the body would prove
nothing about the answer a working call gives. Error answers are searched too: a canary sent in an invalid body must not
come back in the 400 or the 422 (``test_secret_canary_matrix.py`` sends them through the real routes).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Callable

import pytest

import proxy
from gateway import admin_store, projection_types
from tests.support import canary
from tests.support.route_discovery import discover
from tests.test_admin_owner_holes_round4 import _git_package
from tests.test_admin_secret_sweep import (  # noqa: F401  (host_isolated is a fixture)
    PARAMETERS, S, UNSEEDED_ID, _leaf_errors, _leaks, _seed_registry, host_isolated,
)
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

MODEL = canary.MODEL

ROOT = Path(__file__).resolve().parent.parent
MUTATING = ("POST", "PUT", "PATCH", "DELETE")

# Calls with a different body than the generic ones. The drain flag is process-wide: an empty body
# turns draining ON for the rest of the worker, so only the call that sets it off is made.
ONLY_THESE_BODIES = {("POST", "/admin/api/drain"): [("drain off", {"json": {"draining": False}})]}

# Routes the sweep does not call, each with its reason. A test fails when an entry names no mounted route.
EXCLUDED_ROUTES: dict[tuple[str, str], str] = {}


def _mutating_routes() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for item in discover(proxy.app):
        if item.path.startswith(("/admin/api", "/me/api")):
            found.update((method, item.path) for method in item.methods if method in MUTATING)
    return sorted(found - set(EXCLUDED_ROUTES))


class Ctx:
    def __init__(self, gateway, owner, admin, tmp_path, monkeypatch) -> None:
        self.gateway, self.owner, self.admin, self.tmp_path, self.monkeypatch = gateway, owner, admin, tmp_path, monkeypatch

    def registry_echo(self) -> dict[str, Any]:
        return self.admin.get("/admin/api/operator/service-registry").json()["service_registry"]

    def left_out_view(self, record_id: str) -> dict[str, Any]:
        """The record the registry read shows for a left-out service, as the page would send it back."""
        excluded = self.admin.get("/admin/api/operator/service-registry").json()["excluded"]
        return next(item["record"] for item in excluded if item["id"] == record_id)

    def left_out_fixed(self, record_id: str) -> dict[str, Any]:
        record = self.left_out_view(record_id)
        record["config"]["models"] = []
        return record

    def entry_id(self, carrier: str, index: int) -> str:
        """The id of the ``index``th entry of a carrier of engine-a, read the way a page reads it."""
        record = self.admin.get("/admin/api/operator/service-registry").json()["service_registry"]["services"]["engine-a"]
        node: Any = record
        for part in carrier.split("."):
            node = node[part]
        return node[index]["id"]

    def engine(self) -> dict[str, Any]:
        return self.admin.get("/admin/api/engines/engine-a").json()["engine"]

    def credentialed_package(self, name: str) -> dict[str, Any]:
        self.packages = getattr(self, "packages", 0) + 1
        name = f"{name}{self.packages}"
        yaml = (f"id: {name}svc\nname: Plain\nconfig:\n  protocol: openai-compatible\n"
                "  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n")
        source = _git_package(self.monkeypatch, self.tmp_path, yaml, name=name)
        return {**source, "url": f"https://user:{S['package-password']}@git.invalid/{name}.git?access_token={S['package-query']}"}


    def enable_package_a(self) -> dict[str, Any]:
        """Enable the credentialed git package the sweep names ``package-a``, so a call can remove it."""
        yaml = ("id: packageasvc\nname: Plain\nconfig:\n  protocol: openai-compatible\n"
                "  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n")
        source = {**_git_package(self.monkeypatch, self.tmp_path, yaml, name="package-a"),
                  "url": f"https://user:{S['package-password']}@git.invalid/package-a.git?access_token={S['package-query']}"}
        done = self.owner.post("/admin/api/extensions/enable", json={"source": source},
                               headers={**csrf_for(self.owner), "If-Match": _etag(self.gateway)})
        assert done.status_code == 200, done.text
        return {}


def _create_provider(ctx: Ctx):
    return ctx.owner.post("/admin/api/providers", headers=csrf_for(ctx.owner), json={
        "preset_id": "openrouter", "provider_id": "provider-a", "key_mode": "secret",
        "api_key": "synthetic-provider-key-4f2a", "models": [MODEL], "caps": {"daily_usd": 5}})


# Built calls: (label, owner must succeed, body factory). The factory runs after the fresh seed.
Built = tuple[str, bool, Callable[[Ctx], dict[str, Any]]]


def _engine_body(ctx: Ctx, **extra: Any) -> dict[str, Any]:
    """What the engine page posts back: the values it was shown, without the headers (a header is an entry, changed by
    the secret-entries commands, and an edit that does not name headers leaves them as stored)."""
    view = ctx.engine()
    values = {key: value for key, value in view["values"].items() if key != "headers"}
    return {"definition": view["definition"], "values": values, **extra}


BUILT: dict[tuple[str, str], list[Built]] = {
    ("PUT", "/admin/api/operator/service-registry"): [
        ("registry echo", True, lambda c: {"json": c.registry_echo()}),
        ("registry echo with a broken record", False, lambda c: {"json": _broken(c.registry_echo())}),
    ],
    ("POST", "/admin/api/operator/services"): [
        ("engine create with a plain header", True,
         lambda c: {"json": {**_engine_body(c, disabled=True), "name": "Copy", "values": {
             "address": "http://192.168.1.99:9", "headers": [{"name": "X-Team", "value": "team-public"}]}}}),
        ("engine create with a literal marker", False,
         lambda c: {"json": {**_engine_body(c, disabled=True), "name": "Marked", "values": {
             "address": "http://marked.invalid:9", "headers": [{"name": "X-Vendor", "value": "[redacted]"}]}}}),
        ("form create", True, lambda c: {"json": {"name": "Form", "base_url": "http://form.invalid:9/v1"}}),
    ],
    ("PATCH", "/admin/api/operator/services/{service_id}"): [
        ("engine page echo", True, lambda c: {"json": _engine_body(c, disabled=True)}),
        ("engine page moves the host", False,
         lambda c: {"json": _engine_body(c, disabled=True, values={"address": "http://moved.invalid:1"})}),
        ("engine page names a header", False,
         lambda c: {"json": _engine_body(c, disabled=True, values={"headers": [{"name": "X-New", "value": S["header-neutral"]}]})}),
        ("form rename", True, lambda c: {"json": {"name": "Engine A"}}),
    ],
    ("PUT", "/admin/api/operator/service-registry/excluded/{section}/{record_id}"): [
        ("fix a left-out service", True, lambda c: {"json": {"record": c.left_out_fixed("held-url")}}),
        ("edit that stays invalid", False, lambda c: {"json": {"record": c.left_out_view("held-url")}}),
    ],
    ("DELETE", "/admin/api/operator/service-registry/excluded/{section}/{record_id}"): [
        ("remove a left-out service", True, lambda c: {})],
    ("POST", "/admin/api/operator/services/{service_id}/secret-entries"): [
        ("add a header", True, lambda c: {"json": {"commands": [
            {"carrier": "config.headers", "operation": "add", "name": "X-Added", "value": S["header-provider"]}]}}),
        ("replace and clear by id", True, lambda c: {"json": {"commands": [
            {"carrier": "config.headers", "operation": "replace", "entry_id": c.entry_id("config.headers", 0), "value": S["header-neutral"]},
            {"carrier": "config.headers", "operation": "clear", "entry_id": c.entry_id("config.headers", 1)}]}}),
        ("a refused command", False, lambda c: {"json": {"commands": [
            {"carrier": "config.headers", "operation": "replace", "entry_id": "ent_" + "0" * 16, "value": S["header-neutral"]}]}}),
    ],
    ("POST", "/admin/api/operator/services/{service_id}/enabled"): [("enable", True, lambda c: {"json": {"enabled": True}})],
    ("POST", "/admin/api/engines/check"): [("check the shown values", False, lambda c: {"json": _engine_body(c, service_id="engine-a")})],
    ("POST", "/admin/api/engines/test"): [("test the shown values", False,
                                           lambda c: {"json": _engine_body(c, service_id="engine-a", capability="chat")})],
    ("POST", "/admin/api/engines/access"): [("who can use it", True,
                                             lambda c: {"json": {"capabilities": ["chat"], "models": ["first"]}})],
    ("POST", "/admin/api/extensions/validate"): [("credentialed git source", True,
                                                  lambda c: {"json": {"source": c.credentialed_package("sweepone")}})],
    ("POST", "/admin/api/extensions/enable"): [("credentialed git source", True,
                                                lambda c: {"json": {"source": c.credentialed_package("sweeptwo")}})],
    ("POST", "/admin/api/providers"): [
        ("create a provider", True, lambda c: {"json": {
            "preset_id": "openrouter", "provider_id": "provider-b", "key_mode": "secret",
            "api_key": "synthetic-provider-key-9z", "models": [MODEL], "caps": {"daily_usd": 5}}})],
    ("PUT", "/admin/api/providers/{provider_id}/models"): [("replace the models", True, lambda c: {"json": {"models": [MODEL]}})],
    ("PUT", "/admin/api/providers/{provider_id}/key"): [("replace the key", True, lambda c: {"json": {"api_key": "synthetic-provider-key-7q"}})],
    ("PUT", "/admin/api/providers/{provider_id}/caps"): [("set the caps", True, lambda c: {"json": {"daily_usd": 6}})],
    ("DELETE", "/admin/api/providers/{provider_id}/caps"): [("clear the caps", True, lambda c: {})],
    ("DELETE", "/admin/api/providers/{provider_id}"): [("remove a provider", True, lambda c: {})],
    ("DELETE", "/admin/api/operator/services/{service_id}"): [("remove an engine", True, lambda c: {})],
    ("DELETE", "/admin/api/extensions/{package_id}"): [("remove an enabled package", True, lambda c: c.enable_package_a())],
}
# Routes that need a provider to exist: one is created (by the owner) before each call.
NEEDS_PROVIDER_PREFIX = "/admin/api/providers/{provider_id}"
# Routes whose success answer carries registry content: the owner's built call must reach it.
GENERIC: list[tuple[str, dict[str, Any]]] = [
    ("no body", {}),
    ("empty object", {"json": {}}),
    ("malformed json", {"content": b"{not json", "headers": {"content-type": "application/json"}}),
]


def _broken(registry: dict[str, Any]) -> dict[str, Any]:
    registry["services"]["engine-a"]["config"]["models"] = ["not a model record"]
    return registry


def _fill(path: str) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: PARAMETERS.get(m.group(1).split(":", 1)[0], UNSEEDED_ID), path)


def test_every_mutating_route_is_called_or_excluded_with_a_reason() -> None:
    routes = set(_mutating_routes()) | set(EXCLUDED_ROUTES)
    assert len(routes) >= 80, "the route table lost its mutating routes"
    assert set(EXCLUDED_ROUTES) <= routes
    assert {("DELETE", "/admin/api/extensions/{package_id}"), ("PUT", "/admin/api/operator/service-registry"),
            ("DELETE", "/admin/api/operator/services/{service_id}")} <= routes
    assert set(BUILT) <= routes, "a built call names a route that is not mounted"
    assert set(ONLY_THESE_BODIES) <= routes


@pytest.mark.parametrize("method, path", _mutating_routes(), ids=lambda value: str(value))
def test_no_mutating_route_answers_with_a_stored_secret(gateway, host_isolated, tmp_path, monkeypatch, method, path) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    ctx = Ctx(gateway, owner, admin, tmp_path, monkeypatch)
    clients = {"admin": admin, "owner": owner, "anonymous": gateway.client()}
    attempts: list[Built] = [(label, False, lambda c, kw=kwargs: kw) for label, kwargs in
                             ONLY_THESE_BODIES.get((method, path), GENERIC)] + BUILT.get((method, path), [])
    url = _fill(path)
    leaks: list[str] = []
    errored: list[str] = []
    owner_successes: dict[str, tuple[int, str]] = {}
    for who, client in clients.items():
        for label, must_succeed, factory in attempts:
            _seed_registry(gateway)
            if path.startswith(NEEDS_PROVIDER_PREFIX):
                setup = _create_provider(ctx)
                found = _leaks(setup)
                if found:
                    leaks.append(f"setup {who}/{label}: {found}")
            try:
                kwargs = dict(factory(ctx))
                headers = {"If-Match": _etag(gateway), **kwargs.pop("headers", {})}
                if who != "anonymous":
                    headers.update(csrf_for(client))
                response = client.request(method, url, headers=headers, **kwargs)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001  (a route that raises answered nothing; it is listed below)
                errored.append(f"{who} / {label}: {'; '.join(_leaf_errors(exc)) or type(exc).__name__}")
                continue
            found = _leaks(response)
            if found:
                leaks.append(f"{who} / {label} -> {response.status_code}: {', '.join(found)}")
            if who == "owner" and must_succeed:
                owner_successes[label] = (response.status_code, response.text[:300])
    assert not leaks, "stored secrets in responses:\n" + "\n".join(leaks)
    assert not errored, "calls that raised instead of answering:\n" + "\n".join(errored)
    for label, must_succeed, _ in attempts:
        if must_succeed:
            assert 200 <= owner_successes.get(label, (0, ""))[0] < 300, (label, owner_successes.get(label))


def _etag(gateway) -> str:
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    return f'"os-{record["operator_settings_generation"] if record else 0}"'


# -- one door -----------------------------------------------------------------------------------------

# The body keys that carry stored records are the projection types' (gateway/projection_types.py): a route that sets one of them
# must answer with ``egress.projected_response``; ``plain_json_response`` refuses them at run time, and a bare
# ``JSONResponse`` is how a route once forgot the projection. A branch that adds a route meets this test on merge.
RECORD_CONTENT_KEYS = frozenset(projection_types.BODY_KEYS)
# The modules that make management responses. They build them with ``gateway/egress.py`` only.
MANAGEMENT_MODULES = ("admin_api.py", "provider_admin.py", "engine_admin.py")


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == name]


def test_no_management_module_builds_a_response_without_the_egress_constructors() -> None:
    bare = []
    for name in MANAGEMENT_MODULES:
        path = ROOT / "gateway" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls_named(tree, "JSONResponse") + _calls_named(tree, "StreamingResponse") + _calls_named(tree, "Response"):
            bare.append(f"gateway/{name}:{node.lineno}")
    assert not bare, "build it with gateway/egress.py (projected_response, plain_json_response, safe_error_response):\n" + "\n".join(bare)


def test_no_route_answers_with_record_content_through_the_plain_constructor() -> None:
    found: list[str] = []
    for path in sorted((ROOT / "gateway").glob("*.py")) + [ROOT / "proxy.py"]:
        if path.name == "egress.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls_named(tree, "plain_json_response"):
            content = next((kw.value for kw in node.keywords if kw.arg == "content"), node.args[0] if node.args else None)
            if isinstance(content, ast.Dict):
                keys = {key.value for key in content.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
                if keys & RECORD_CONTENT_KEYS:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {sorted(keys & RECORD_CONTENT_KEYS)}")
    assert not found, "answer with egress.projected_response instead:\n" + "\n".join(found)
