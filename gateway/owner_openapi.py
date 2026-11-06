"""Describe existing owner HTTP contracts without changing route authorization.

The explicit allowlist includes hidden JSON operations. New routes need review
before they become CLI commands. It is schema data, not an authorization layer.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.routing import Mount

from gateway import operation_security


OWNER_OPERATIONS = json.loads(Path(__file__).with_name("owner_openapi_routes.json").read_text())


def walk_routes(routes, prefix=""):
    """Yield every route, including mount containers and prefixed children."""
    for route in routes:
        path = prefix + getattr(route, "path", "")
        yield path, route
        if isinstance(route, Mount):
            yield from walk_routes(route.routes, path)


def augment_owner_openapi(schema: dict, routes: list) -> None:
    selected = []
    for path, route in walk_routes(routes):
        methods = getattr(route, "methods", None) or ()
        if not any(f"{method} {path}" in OWNER_OPERATIONS for method in methods):
            continue
        if not isinstance(route, APIRoute):
            raise ValueError(f"Owner schema metadata requires an APIRoute: {path}")
        described_route = copy.copy(route)
        described_route.path = path
        described_route.path_format = re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)
        described_route.include_in_schema = True
        selected.append(described_route)
    # Copies let FastAPI infer typed bodies/parameters on hidden routes, while
    # keeping the running route objects and their handlers untouched.
    owner_schema = get_openapi(title="Owner API", version="1", routes=selected)
    schema.setdefault("components", {}).setdefault("schemas", {}).update(
        owner_schema.get("components", {}).get("schemas", {}))
    schema["components"].setdefault("securitySchemes", {})["OwnerBearer"] = {
        "type": "http", "scheme": "bearer",
        "description": "Admin automation credential with the required owner/admin role. App keys are rejected."
    }
    for route in selected:
        for method in sorted(route.methods):
            metadata = OWNER_OPERATIONS.get(f"{method} {route.path}")
            if metadata is None:
                continue
            operation = owner_schema["paths"][route.path][method.lower()]
            operation["operationId"] = "owner_" + method.lower() + "_" + re.sub(
                r"[^a-zA-Z0-9]+", "_", route.path).strip("_")
            operation["tags"] = [metadata["tag"]]
            bearer_only = metadata.get("bearer_only", False)
            operation["security"] = operation_security.security_for(
                operation_security.OWNER_BEARER if bearer_only else operation_security.OWNER)
            parameters = operation.setdefault("parameters", [])
            for name in re.findall(r"\{([^}:]+)(?::[^}]+)?\}", route.path):
                if not any(p.get("in") == "path" and p.get("name") == name for p in parameters):
                    parameters.append({"name": name, "in": "path", "required": True,
                                       "schema": {"type": "string"}})
            audience = (". A browser session is refused, even with CSRF and action headers."
                        if bearer_only else ". Browser session mutations require CSRF/action headers; bearer calls do not.")
            operation["description"] = (operation.get("description", "") + "\n\nAuthorization: "
                + metadata["gate"] + audience).strip()
            # Most legacy handlers manually read JSON. An open object is honest;
            # required fields and validation remain documented by the handler.
            if "requestBody" not in operation and method in {"POST", "PUT", "PATCH"}:
                operation["requestBody"] = {"required": False, "content": {
                    "application/json": {"schema": {"type": "object", "additionalProperties": True}}}}
            for code in ("401", "403"):
                operation["responses"].setdefault(code, {"description": "Authentication or role denied",
                    "content": {"application/json": {"schema": {"type": "object",
                        "properties": {"error": {"type": "object", "properties": {
                            "code": {"type": "string"}, "message": {"type": "string"}}}}}}}})
            if "success_status" in metadata:
                operation["responses"][str(metadata["success_status"])] = operation["responses"].pop("200")
            if route.path == "/admin/api/config/export":
                operation["responses"]["200"] = {"description": "Canonical configuration YAML",
                    "content": {"text/plain": {"schema": {"type": "string"}}}}
            schema["paths"].setdefault(route.path, {})[method.lower()] = operation
    mark_owner_operations(schema)


OWNER_ONLY_TAG = "Owner only"
OWNER_ONLY_NOTE = "Owner only: needs the owner's sign-in."
OWNER_AND_ADMINS_TAG = "Owner and admins"
OWNER_AND_ADMINS_NOTE = "Needs the owner's or an admin's sign-in."


def mark_owner_operations(schema: dict) -> None:
    """Tag every operation that takes the owner's or an admin's credential, and open its description with its audience.

    The audience is the handler's gate, recorded per operation (``owner_only`` in owner_openapi_routes.json). ADMIN_AUTH
    admits the owner, admin and superadmin roles, so an operation with no such record takes the admins too. The rule keys
    on the OwnerBearer requirement, so an operation declared anywhere in the app is marked the same way. Running it twice
    changes nothing.
    """
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or {"OwnerBearer": []} not in operation.get("security", []):
                continue
            owner_only = OWNER_OPERATIONS.get(f"{method.upper()} {path}", {}).get("owner_only", False)
            tag, note = (OWNER_ONLY_TAG, OWNER_ONLY_NOTE) if owner_only else (OWNER_AND_ADMINS_TAG, OWNER_AND_ADMINS_NOTE)
            tags = operation.setdefault("tags", [])
            if tag not in tags:
                tags.append(tag)
            description = operation.get("description", "")
            if not description.startswith(note):
                operation["description"] = f"{note}\n\n{description}".strip()
