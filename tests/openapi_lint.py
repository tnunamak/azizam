"""A small OpenAPI 3.1 structural validator for hermetic tests.

``openapi-spec-validator`` is not in the hermetic lock, so this checks the
rules that break real clients: resolvable ``$ref``s, valid JSON Schemas,
declared path parameters, unique operation ids, typed request bodies and
non-empty responses.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from jsonschema import Draft202012Validator

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _walk(node: Any, trail: str = "#") -> Iterator[tuple[str, Any]]:
    yield trail, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{trail}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{trail}/{index}")


def _resolve(doc: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node: Any = doc
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def validate_openapi(doc: dict[str, Any]) -> list[str]:
    """Return a list of problems; empty means the document is sound."""
    problems: list[str] = []
    if not str(doc.get("openapi", "")).startswith("3."):
        problems.append("openapi version missing or not 3.x")
    info = doc.get("info") or {}
    for key in ("title", "version"):
        if not info.get(key):
            problems.append(f"info.{key} missing")

    for trail, node in _walk(doc):
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            try:
                _resolve(doc, node["$ref"])
            except (KeyError, TypeError):
                problems.append(f"{trail}: unresolvable $ref {node['$ref']}")

    for name, schema in (doc.get("components", {}).get("schemas") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            problems.append(f"components.schemas.{name}: invalid component name")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"components.schemas.{name}: {str(exc).splitlines()[0]}")

    seen_ids: dict[str, str] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not path.startswith("/"):
            problems.append(f"{path}: path must start with /")
        template = set(re.findall(r"{([^}]+)}", path))
        shared = {p.get("name") for p in item.get("parameters", []) if p.get("in") == "path"}
        for method, op in item.items():
            if method not in METHODS:
                continue
            where = f"{method.upper()} {path}"
            declared = shared | {
                p.get("name") for p in op.get("parameters", []) if p.get("in") == "path"
            }
            if template - declared:
                problems.append(f"{where}: undeclared path params {sorted(template - declared)}")
            if not op.get("responses"):
                problems.append(f"{where}: no responses")
            op_id = op.get("operationId")
            if op_id:
                if op_id in seen_ids:
                    problems.append(f"{where}: duplicate operationId {op_id} ({seen_ids[op_id]})")
                seen_ids[op_id] = where
            body = op.get("requestBody")
            if body is not None:
                content = body.get("content") if isinstance(body, dict) else None
                if not content:
                    problems.append(f"{where}: requestBody has no content")
                else:
                    for media, entry in content.items():
                        if "schema" not in entry:
                            problems.append(f"{where}: requestBody {media} has no schema")
            for code, response in (op.get("responses") or {}).items():
                if not re.fullmatch(r"[1-5][0-9X]{2}|default", str(code)):
                    problems.append(f"{where}: bad status code {code}")
                if isinstance(response, dict) and "$ref" not in response and "description" not in response:
                    problems.append(f"{where}: response {code} has no description")
    return problems
