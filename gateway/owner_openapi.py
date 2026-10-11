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


OWNER_OPERATIONS = json.loads(Path(__file__).with_name("owner_openapi_routes.json").read_text())


def augment_owner_openapi(schema: dict, routes: list) -> None:
    selected = [route for route in routes if isinstance(route, APIRoute)
                and any(f"{method} {route.path}" in OWNER_OPERATIONS for method in route.methods)]
    # Copies let FastAPI infer typed bodies/parameters on hidden routes, while
    # keeping the running route objects and their handlers untouched.
    described = []
    for route in selected:
        described_route = copy.copy(route)
        described_route.include_in_schema = True
        described.append(described_route)
    owner_schema = get_openapi(title="Owner API", version="1", routes=described)
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
            operation["security"] = [{"OwnerBearer": []}]
            parameters = operation.setdefault("parameters", [])
            for name in re.findall(r"\{([^}:]+)(?::[^}]+)?\}", route.path):
                if not any(p.get("in") == "path" and p.get("name") == name for p in parameters):
                    parameters.append({"name": name, "in": "path", "required": True,
                                       "schema": {"type": "string"}})
            operation["description"] = (operation.get("description", "") + "\n\nAuthorization: "
                + metadata["gate"] + ". Browser session mutations require CSRF/action headers; bearer calls do not."
                + (" Drain writes accept bearer credentials only." if method == "POST" and route.path.endswith("/drain") else "")).strip()
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
