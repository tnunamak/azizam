"""Every route that can make a credential, grant a role, move a credential, export configuration or change
how people sign in asks a stale session to confirm it is them (gateway/step_up.py).

The routes come from the running app, not from a list kept here. A route is risky when its path names one
of those jobs, or when its handler's own code calls one of the functions that do them. Each risky route
has to be in gateway/step_up.py, or be listed below with the reason it is safe. A new route that does
one of these jobs therefore fails this test until someone decides.
"""
import inspect
import re

import pytest

from gateway import step_up

# The jobs, as paths: (pattern, what it does, methods). "*" stands for one path segment.
CHANGES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
RISKY_PATHS = (
    (r"/keys$", "makes a key", CHANGES),
    (r"/try-key$", "makes a key", CHANGES),
    (r"/first-app$", "makes an App and a key", CHANGES),
    (r"/admin-keys$", "makes an admin key", CHANGES),
    (r"/litellm/keys(/delete)?$", "makes or removes gateway keys", CHANGES),
    (r"/providers(/\*/key)?$", "adds a provider or replaces its key", CHANGES),
    (r"/invites$", "invites a person, with a role", CHANGES),
    (r"/invites/\*/link$", "shows an invitation link", {"GET"}),
    (r"/people/\*(/reset)?$", "changes or removes a person, or resets a password", CHANGES),
    (r"/signin(/link-owner|/owner-password)?$", "changes who may sign in", CHANGES),
    (r"/settings/sessions$", "changes how long a sign-in lasts", CHANGES),
    (r"/export$|/settings/effective$", "exports configuration", {"GET"}),
    (r"/service-registry($|/)", "can move a stored credential", CHANGES),
    (r"/operator/services($|/\*$|/\*/secret-entries$)", "can move or replace a stored credential", CHANGES),
    (r"/host-commands$", "lets the gateway run commands", CHANGES),
    (r"/trusted-addresses$", "lets a device in with no key", CHANGES),
    (r"/extensions/enable$", "enables code that can read this computer", CHANGES),
    (r"/mcp/requests/\*$", "connects a client with a lasting grant", CHANGES),
)

# Calls that do those jobs. A handler whose own source contains one is risky whatever its path says.
RISKY_CALLS = (
    "create_credential", "create_credential_hash", "create_admin_key", "mint_user_app_key",
    "ensure_try_session_key", "put_provider_secret", "create_litellm_key", "update_service_registry",
    "apply_secret_commands", "replace_excluded_record", "create_link(", "update_session_policy",
)

# Risky by the rules above and safe, with the reason. A stale entry (the route is gone, or no longer
# looks risky) fails too, so this list cannot grow quietly.
REVIEWED_SAFE = {
    ("DELETE", "/admin/api/operator/services/*"): "removing a service moves no credential",
    ("DELETE", "/admin/api/operator/service-registry/excluded/*/*"): "dropping a left-out record moves no credential",
}


def normal(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "*", path).rstrip("/") or "/"


def app_routes(proxy):
    """(method, normalised path, handler) for every route the app serves."""
    seen = {}
    for route in proxy.app.routes:
        endpoint = getattr(route, "endpoint", None)
        for method in getattr(route, "methods", None) or ():
            if method not in {"HEAD", "OPTIONS"} and endpoint is not None:
                seen[(method, normal(route.path))] = endpoint
    return seen


def risky_reason(method: str, path: str, handler) -> str | None:
    for pattern, reason, methods in RISKY_PATHS:
        if method in methods and re.search(pattern, path):
            return reason
    source = inspect.getsource(handler)
    for call in RISKY_CALLS:
        if call in source:
            return f"its handler calls {call.rstrip('(')}"
    return None


@pytest.fixture(scope="module")
def routes():
    import proxy
    return app_routes(proxy)


def covered(method: str, path: str) -> bool:
    asked = step_up.is_sensitive(method, path.replace("*", "someid"))
    inside = any(m == method and normal(p) == path for m, p, _ in step_up.HANDLER_GATED)
    return asked or inside


def test_every_risky_route_is_in_the_step_up_list_or_reviewed_as_safe(routes):
    unclassified = sorted(
        (method, path, reason)
        for (method, path), handler in routes.items()
        if (reason := risky_reason(method, path, handler)) and not covered(method, path)
        and (method, path) not in REVIEWED_SAFE
    )

    assert unclassified == [], (
        "These routes do something a stolen session must not be able to do, and no step-up covers them. "
        "Add each to gateway/step_up.py (SENSITIVE_ROUTES, or HANDLER_GATED when only some requests are risky), "
        "or to REVIEWED_SAFE here with the reason.")


def test_the_reviewed_safe_list_has_no_stale_entries(routes):
    stale = sorted(
        key for key in REVIEWED_SAFE
        if key not in routes or risky_reason(*key, routes[key]) is None or covered(*key)
    )

    assert stale == []


def test_every_listed_route_exists_in_the_app(routes):
    asked = {(method, normal(path)) for method, path in step_up.SENSITIVE_ROUTES}
    gated = {(method, normal(path)) for method, path, _ in step_up.HANDLER_GATED}

    assert sorted((asked | gated) - set(routes)) == []
    assert not asked & gated, "a route is either asked by the middleware or inside its handler"


def test_the_detector_sees_the_routes_the_review_found_missing(routes):
    # The two routes the review reproduced as open: a member's key and a registry write.
    for key in (("POST", "/me/api/apps/*/keys"), ("PUT", "/admin/api/operator/service-registry"),
                ("POST", "/setup/first-app"), ("POST", "/me/api/try-key"), ("GET", "/admin/api/settings/effective")):
        assert key in routes and risky_reason(*key, routes[key]), key


# -- the registry funnel ----------------------------------------------------------------------------

def _calls(function) -> set[str]:
    import ast
    return {call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            for call in ast.walk(function) if isinstance(call, ast.Call)}


def test_every_registry_write_of_the_store_passes_the_step_that_asks_before_a_credential_moves():
    """Four routes can move a stored key (the registry save, services, secret commands, a left-out record).
    They all write through AdminSQLiteStore, and each writer must go through ``_settle_credential_bindings``,
    where ``require_recent`` asks. A new writer that skips it fails here."""
    import ast
    import pathlib

    from gateway import admin_runtime

    tree = ast.parse(pathlib.Path(admin_runtime.__file__).read_text())
    store = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AdminSQLiteStore")
    methods = {fn.name: fn for fn in store.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    writers = {"write_service_registry_with_audit", "write_service_registry_and_setting_with_audits"}
    # Helpers that only write; each of their callers is checked below.
    helpers = {"_write_registry_for_update", "_write_registry_for_persist"}
    # Removing or disabling package records moves no credential; enabling one settles above.
    reviewed = {"_change_extension_package"}

    writing = {name for name, fn in methods.items() if _calls(fn) & writers}
    assert writing == helpers | reviewed | {"enable_extension_package"}, writing
    for name, fn in methods.items():
        if _calls(fn) & helpers:
            assert "_settle_credential_bindings" in _calls(fn), name
    assert "_settle_credential_bindings" in _calls(methods["enable_extension_package"])
    assert "_settle_credential_bindings" in _calls(methods["_persist_service_registry"])
    settle = ast.unparse(methods["_settle_credential_bindings"])
    assert "require_recent" in settle and settle.index("require_recent") > settle.index("credential_binding_changes")
