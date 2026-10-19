"""Every function that reads a secret value registers it for the log scrub (decision D42).

A server can echo a credential it was sent, so the lines the drivers and probes log from an upstream pass through
``log_scrub.scrub``, which removes the values registered with ``log_scrub.remember`` (or ``env_secret``, or any
``runtime_secrets.resolve``). A reader that does not register leaves its key in the log when an upstream echoes it
(round 5 found the engine credential headers and the native lane). This test finds every function of ``gateway/`` and
``proxy.py`` that reads a secret and fails when one neither registers it nor is listed below with the reason it need not.

A function "reads a secret" when it calls ``get_provider_secret`` (a stored secret), or reads an environment variable by a
name that is not a literal (``os.environ.get(name)``, ``get_env(name)``, ``env(name)``): the name comes from a record or
a configuration, so the variable holds whatever the operator pointed it at. The discovery is syntactic. It does not
see a secret read through a helper in another module, which is why each helper that reads is itself found.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTERS = {"remember", "env_secret", "resolve"}
# A name that says its value is a credential. It decides what a literal environment name, a configuration key or a
# configuration attribute is: ``os.environ.get("OPENAI_API_KEY")`` and ``cfg.client_secret`` read a secret as surely as
# ``os.environ.get(name)`` does.
SECRET_NAME = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth)")
# ... unless the name says it holds something else: the name of a variable, a reference, an id, an address, a path.
NOT_A_VALUE = re.compile(r"(?i)(_env|_ref|_id|_origin|_endpoint|_url|_seconds|_s|_name|_path|_file|_bytes|_per_key)$")
NOT_A_VALUE_EXACT = {"auth_inject", "credentials", "auth", "auth_test", "authorization_endpoint", "token_endpoint"}
CONFIG_BASE = re.compile(r"(?i)(config|settings|cfg|conf)$")

# "file::qualified function": why it need not register. Each reads a name from configuration but the value is not a
# credential that can reach an upstream or a log, or its caller registers it.
NO_REGISTRATION_NEEDED = {
    "gateway/backends/llm_execution.py::_base_url_from_config": "an address, not a credential",
    "gateway/config_sources.py::_env_rows": "reads the names and presence of settings for a management view",
    "gateway/engine_admin.py::_probe_limit": "a number from the environment",
    "gateway/engine_admin.py::_stored_evidence_binding": "hashes the value into an evidence digest; the value goes nowhere",
    "gateway/engine_admin.py::engine_view": "asks only whether a variable is set",
    "gateway/engine_admin.py::EngineService.__init__": "stores the reader function; reads nothing",
    "gateway/litellm_substrate.py::litellm_provider_key_statuses": "asks only whether a variable is set",
    "gateway/provider_admin.py::_provider_key_fingerprint": "hashes the value into a fingerprint; the value goes nowhere",
    "gateway/provider_admin.py::_project_key_status": "asks only whether a key is set",
    "gateway/request_body_limits.py::RequestBodyLimits.from_config": "a number from the environment",
    "gateway/secret_store.py::export_provider_secret_env": "builds the process environment for the sidecar; its caller (build_sidecar_env) registers",
    "proxy.py::_admin_session_signing_key_from_config": "a signing key: never sent to an upstream and never logged",
    "proxy.py::_probe_service_resource_fact": "the name of a base-URL variable (an address)",
    "proxy.py::_service_resource_probe_url": "the name of a base-URL variable (an address)",
    "proxy.py::_is_internal_litellm_lifecycle_request": "compares the internal key with a request header; neither is logged or sent on",
    "proxy.py::_bootstrap_admin_oidc_cfg": "reads the names of the signing-key settings to build the sign-in configuration; the key is never sent or logged",
    "proxy.py::_resolved_admin_oidc_config": "the signing key of the admin session: signs a cookie, never sent to an upstream or logged",
    "proxy.py::admin_oidc_callback": "the signing key of the admin session: signs a cookie, never sent to an upstream or logged",
    "proxy.py::_admin_oidc_config_snapshot": "builds the management view of the sign-in settings: says whether a client secret is saved, never its value",
    "gateway/signin_api.py::create_signin_api.build_snapshot": "builds the management view of the sign-in settings: says whether a client secret is saved, never its value",
}


def _name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _chain(func: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def _value_name(name: object) -> bool:
    return isinstance(name, str) and bool(SECRET_NAME.search(name)) and not NOT_A_VALUE.search(name) and name not in NOT_A_VALUE_EXACT


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _config_base(node: ast.AST) -> bool:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
    return bool(CONFIG_BASE.search(name))


def _reads_secret(node: ast.AST) -> bool:
    """A stored secret, a variable read by a name that is not a literal, a variable whose literal name says it is a credential,
    or a configuration key or attribute whose name says it holds one."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        return _value_name(node.attr) and _config_base(node.value)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return _value_name(_literal(node.slice)) and _config_base(node.value)
    if not isinstance(node, ast.Call):
        return False
    name, chain = _name(node.func), _chain(node.func)
    if name == "get_provider_secret":
        return True
    named = chain in ("os.environ.get", "os.getenv", "get_env", "env") or name == "get_env"
    if named and node.args:
        literal = _literal(node.args[0])
        return not isinstance(node.args[0], ast.Constant) or bool(literal and SECRET_NAME.search(literal))
    if name == "get" and isinstance(node.func, ast.Attribute) and node.args and _config_base(node.func.value):
        return _value_name(_literal(node.args[0]))
    return False


class _Functions(ast.NodeVisitor):
    """For each function, the secret reads and registrations in its own body (a nested function is its own entry)."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: dict[str, dict[str, bool]] = {}

    def _visit_function(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        qualified = ".".join(self.stack)
        entry = self.found.setdefault(qualified, {"reads": False, "registers": False})
        pending = list(ast.iter_child_nodes(node))
        while pending:
            child = pending.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            entry["reads"] = entry["reads"] or _reads_secret(child)
            if isinstance(child, ast.Call):
                entry["registers"] = entry["registers"] or _name(child.func) in REGISTERS
            pending.extend(ast.iter_child_nodes(child))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def discover() -> dict[str, dict[str, bool]]:
    found: dict[str, dict[str, bool]] = {}
    for path in [*sorted((ROOT / "gateway").rglob("*.py")), ROOT / "proxy.py"]:
        visitor = _Functions()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        relative = path.relative_to(ROOT).as_posix()
        for qualified, entry in visitor.found.items():
            if entry["reads"]:
                found[f"{relative}::{qualified}"] = entry
    return found


def test_every_function_that_reads_a_secret_registers_it_or_says_why_not():
    found = discover()
    bare = sorted(key for key, entry in found.items() if not entry["registers"] and key not in NO_REGISTRATION_NEEDED)
    assert not bare, (
        "these functions read a secret and do not register it with gateway/log_scrub.py (remember / env_secret), "
        f"nor are they listed with a reason in this test: {bare}")


def test_the_list_of_functions_that_need_no_registration_holds_no_stale_entry():
    found = discover()
    stale = sorted(key for key in NO_REGISTRATION_NEEDED if key not in found)
    registering = sorted(key for key in NO_REGISTRATION_NEEDED if key in found and found[key]["registers"])
    assert not stale, f"listed, but no longer reads a secret: {stale}"
    assert not registering, f"listed, but it registers now: {registering}"


def test_the_discovery_finds_the_readers_it_is_meant_to_find():
    found = discover()
    for key in ("gateway/engine_credentials.py::credential_headers", "gateway/engine_connection.py::inference_connection",
                "gateway/providers.py::resolve_provider_api_key", "gateway/provider_sidecar.py::build_sidecar_env",
                "gateway/engine_admin.py::_stored_key", "gateway/runtime_secrets.py::resolve"):
        assert key in found and found[key]["registers"], key
    assert len(found) >= 15
