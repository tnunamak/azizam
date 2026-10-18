"""Decisions D26, D28, D30, D31 as architecture rules, checked on the source.

Management code (the routes, the projection, the commands, the migration, the secret references) never reads a secret
back; one module does (``gateway/runtime_secrets.py``), and a short list of runtime modules import it. The projection
builds from registered types and never calls a serializer fallback. No module of the old detection (key names and value
shapes) is left on the response path. These rules fall back on review for what a static check cannot see (reflection,
dynamic imports); the canary matrix is what proves the behaviour.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "gateway"

# The modules that resolve a secret reference at the moment of use, and why each may.
RUNTIME_IMPORTERS = {
    "gateway/runtime_secrets.py": "the one place that reads a secret reference back",
    "gateway/litellm_runtime.py": "renders the executable router config into a private file",
    "gateway/lifecycle_drivers.py": "resolves a command, an environment and an admin key when it starts or stops an engine",
    "gateway/engine_process.py": "resolves the argument list and environment of an owned process when it starts it",
    "gateway/engine_observations.py": "resolves the query credentials of a readiness probe when it sends the probe",
    "proxy.py": "resolves a native call's headers and URL credentials when it forwards the call",
}
# Modules that read a provider key from the store directly (decision D26 moved the other carriers; a provider key is
# resolved for its origin where a request is built). Each may, a new one may not.
KEY_READERS = {
    "gateway/engine_credentials.py", "gateway/engine_admin.py", "gateway/providers.py", "gateway/provider_admin.py",
    "gateway/admin_runtime.py", "gateway/secret_store.py", "gateway/secret_refs.py", "gateway/runtime_secrets.py", "proxy.py",
    "gateway/provider_sidecar.py",
}
# Management modules: none of them reads a secret back.
MANAGEMENT = [
    "gateway/projection.py", "gateway/projection_types.py", "gateway/egress.py", "gateway/secret_commands.py",
    "gateway/secret_refs.py", "gateway/record_carriers.py", "gateway/secret_externalize_migration.py", "gateway/admin_api.py",
    "gateway/legacy_credential_rules.py",
]
RETIRED = ("engine_redaction", "secret_policy", "secret_restore")


def _tree(relative: str) -> ast.AST:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _production_files() -> list[str]:
    return sorted(str(path.relative_to(ROOT)) for path in [*GATEWAY.glob("*.py"), ROOT / "proxy.py"])


def test_only_the_listed_runtime_modules_import_the_secret_resolver_or_the_executable_router_renderer():
    importers = set()
    for relative in _production_files():
        imports = _imports(_tree(relative))
        if {"gateway.runtime_secrets", "gateway.litellm_runtime"} & imports or any(
                name.endswith(("runtime_secrets", "litellm_runtime")) for name in imports):
            importers.add(relative)
    # admin_runtime and provider_sidecar write the router config file; they import the renderer, not the resolver.
    allowed = set(RUNTIME_IMPORTERS) | {"gateway/admin_runtime.py", "gateway/provider_sidecar.py"}
    assert importers <= allowed, f"modules that read secrets back and are not listed: {sorted(importers - allowed)}"


def test_management_modules_never_import_the_resolver_or_decrypt():
    for relative in MANAGEMENT:
        tree = _tree(relative)
        imports = _imports(tree)
        assert not any(name.endswith("runtime_secrets") or name.endswith("litellm_runtime") for name in imports), relative
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "get_provider_secret" not in names, relative


def test_only_the_listed_modules_call_get_provider_secret():
    callers = {relative for relative in _production_files()
               if any(isinstance(node, ast.Attribute) and node.attr == "get_provider_secret" or isinstance(node, ast.Name) and node.id == "get_provider_secret"
                      for node in ast.walk(_tree(relative)))}
    assert callers <= KEY_READERS | set(RUNTIME_IMPORTERS), f"unlisted readers of the secret store: {sorted(callers - KEY_READERS - set(RUNTIME_IMPORTERS))}"


def test_the_projection_never_uses_a_serializer_fallback():
    for relative in ("gateway/projection.py", "gateway/projection_types.py"):
        tree = _tree(relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name not in {"model_dump", "dict", "repr", "str", "vars"}, (relative, name, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"__dict__", "model_dump", "__repr__"}, (relative, node.attr, node.lineno)


def test_the_retired_detection_by_key_name_and_value_shape_is_gone_from_the_code():
    for relative in _production_files():
        imports = _imports(_tree(relative))
        assert not any(name.split(".")[-1] in RETIRED or name.endswith(tuple(f".{r}" for r in RETIRED)) for name in imports), relative
    for name in RETIRED:
        assert not (GATEWAY / f"{name}.py").exists(), name


def test_the_credential_flag_rule_is_read_only_by_ingestion_and_the_migration():
    users = {relative for relative in _production_files() if any(name.endswith("legacy_credential_rules") for name in _imports(_tree(relative)))}
    assert users == {"gateway/record_carriers.py", "gateway/secret_commands.py"}, users


def test_no_route_module_decides_what_is_secret_by_a_key_name():
    """The old rule: a key whose name had a credential word in it was hidden. Views are built from structure."""
    for relative in ("gateway/projection.py", "gateway/projection_types.py", "gateway/egress.py", "gateway/admin_api.py", "gateway/engine_admin.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "looks_like_credential" not in source and "names_credential" not in source and "is_secret_key" not in source, relative


def test_only_the_secret_store_and_the_integrity_module_read_the_key_file():
    """The install proofs are keyed by the file that protects the store; one small module derives that key."""
    readers = {relative for relative in _production_files()
               if any(isinstance(node, (ast.Attribute, ast.Name)) and getattr(node, "attr", getattr(node, "id", "")) in {
                   "load_secret_key", "load_or_create_secret_key"} for node in ast.walk(_tree(relative)))}
    assert readers == {"gateway/secret_store.py", "gateway/record_integrity.py"}, readers


def test_only_the_package_code_and_the_migration_import_the_integrity_module():
    importers = {relative for relative in _production_files() if any(name.endswith("record_integrity") for name in _imports(_tree(relative)))}
    assert importers <= {"gateway/extension_packages.py", "gateway/admin_runtime.py", "gateway/secret_externalize_migration.py"}, importers
