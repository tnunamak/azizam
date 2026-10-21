"""Engine definitions: the data file Add engine reads, validated at load.

A definition says which fields a kind of server needs, which requests check
it, what it can do and what to tell the owner when something fails. All of
it is data in ``registry/engine_definitions.json``; this module only loads
and validates it. It names no engine.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import jsonschema

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
DEFINITIONS_PATH = _REGISTRY_DIR / "engine_definitions.json"
SCHEMA_PATH = _REGISTRY_DIR / "engine_definitions.schema.json"


class EngineDefinitionError(ValueError):
    """The definitions file cannot be used. The message names the file and the place."""


@dataclass(frozen=True)
class EngineCatalog:
    """The validated definitions file. ``data`` is never mutated; callers get copies."""

    data: Mapping[str, Any]
    source: str

    def definition(self, definition_id: Any) -> dict[str, Any] | None:
        for row in self.data["definitions"]:
            if row["definition"] == definition_id:
                return copy.deepcopy(row)
        return None

    def ids(self) -> list[str]:
        return [row["definition"] for row in self.data["definitions"]]

    def capability(self, capability_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.data["capabilities"][capability_id])

    def metadata_row(self, row_id: str) -> dict[str, Any]:
        for row in self.data["metadata_rows"]:
            if row["id"] == row_id:
                return copy.deepcopy(row)
        raise KeyError(row_id)

    def advice_rows(self, definition: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The definition's own rows first, then the shared rows it does not replace."""
        own = [copy.deepcopy(row) for row in definition.get("advice") or ()]
        codes = {row["code"] for row in own}
        return own + [copy.deepcopy(row) for row in self.data["advice"] if row["code"] not in codes]

    def public(self) -> dict[str, Any]:
        """What ``GET /admin/api/engine-definitions`` returns: every definition with its fields."""
        capabilities = self.data["capabilities"]
        out = []
        for row in self.data["definitions"]:
            item = {
                key: copy.deepcopy(row[key])
                for key in (
                    "definition", "definition_version", "title", "example", "summary", "badge",
                    "keywords", "flow", "steps", "fields", "check_rows", "models_note",
                )
                if key in row
            }
            item["fields"] = sorted(item["fields"], key=lambda field: field.get("order", 0))
            primary = row["probes"]["primary"]
            item["check_request"] = {"method": primary["method"], "path": primary["path"], "from": primary["from"]}
            item["capabilities"] = [
                {"id": cap_id, "label": capabilities[cap_id]["label"]} for cap_id in row["capabilities"]
            ]
            out.append(item)
        tests = {
            test_id: {"sent": test["sent"], "cost": test["cost"]}
            for test_id, test in self.data["real_tests"].items()
        }
        return {"format_version": self.data["format_version"], "definitions": out, "real_tests": tests}


def _where(path: Any) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(top level)"


def _semantic_errors(data: Mapping[str, Any]) -> list[str]:
    """Rules a JSON Schema cannot say: references between rows, unique ids, regexes that compile."""
    errors: list[str] = []
    capabilities = data["capabilities"]
    metadata_ids = {row["id"] for row in data["metadata_rows"]}
    if len(metadata_ids) != len(data["metadata_rows"]):
        errors.append("metadata_rows: two rows share an id")
    for cap_id, cap in capabilities.items():
        test = cap.get("real_test")
        if test and test not in data["real_tests"]:
            errors.append(f"capabilities.{cap_id}.real_test: no real test named {test!r}")
    for index, rule in enumerate(data["model_rules"]):
        if rule["capability"] not in capabilities:
            errors.append(f"model_rules[{index}].capability: no capability named {rule['capability']!r}")
    for row in data["metadata_rows"]:
        for path, cap_id in (row.get("paths") or {}).items():
            if cap_id not in capabilities:
                errors.append(f"metadata_rows.{row['id']}.paths[{path!r}]: no capability named {cap_id!r}")
    patterns = [("against_body", data.get("against_body"))]
    patterns += [(f"public_name_rules[{i}]", r.get("strip") or r.get("replace")) for i, r in enumerate(data.get("public_name_rules") or ())]
    advice_codes: set[str] = set()
    for index, row in enumerate(data["advice"]):
        if row["code"] in advice_codes:
            errors.append(f"advice[{index}].code: {row['code']!r} is used twice")
        advice_codes.add(row["code"])
    seen: set[str] = set()
    for index, definition in enumerate(data["definitions"]):
        where = f"definitions[{index}] ({definition['definition']})"
        if definition["definition"] in seen:
            errors.append(f"{where}: the id is used twice")
        seen.add(definition["definition"])
        if "retention_facts" in definition:
            from gateway.retention_facts import validate_facts
            try:
                validate_facts(definition["retention_facts"])
            except ValueError as exc:
                errors.append(f"{where}.{exc}")
        keys = [field["key"] for field in definition["fields"]]
        if len(set(keys)) != len(keys):
            errors.append(f"{where}.fields: two fields share a key")
        if "address" not in keys:
            errors.append(f"{where}.fields: every definition needs an address field")
        for field in definition["fields"]:
            show_if = field.get("show_if")
            if show_if and show_if["field"] not in keys:
                errors.append(f"{where}.fields.{field['key']}.show_if: no field named {show_if['field']!r}")
            secondary = field.get("secondary_of")
            if secondary and secondary not in keys:
                errors.append(f"{where}.fields.{field['key']}.secondary_of: no field named {secondary!r}")
        for cap_id in definition["capabilities"]:
            if cap_id not in capabilities:
                errors.append(f"{where}.capabilities: no capability named {cap_id!r}")
        for row_id in definition["probes"]["metadata"]:
            if row_id not in metadata_ids:
                errors.append(f"{where}.probes.metadata: no metadata row named {row_id!r}")
        own_codes = [row["code"] for row in definition["advice"]]
        if len(set(own_codes)) != len(own_codes):
            errors.append(f"{where}.advice: two rows share a code")
        for row in [*definition["advice"], *data["advice"]]:
            for key in ("address_matches", "body_matches"):
                if key in row["when"]:
                    patterns.append((f"{where}.advice.{row['code']}.when.{key}", row["when"][key]))
    for row in data["model_rules"]:
        if "matches" in row:
            patterns.append(("model_rules", row["matches"]))
    for where, pattern in patterns:
        if pattern is None:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"{where}: the pattern {pattern!r} does not compile ({exc})")
    return errors


def parse_engine_catalog(data: Any, *, schema: Mapping[str, Any], source: str = "<memory>") -> EngineCatalog:
    """Validate ``data`` against ``schema`` and the cross-row rules. Raises with every place at fault."""
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    problems = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if problems:
        lines = [f"{_where(error.absolute_path)}: {error.message}" for error in problems[:10]]
        more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
        raise EngineDefinitionError(f"{source}: the engine definitions do not match the schema{more}:\n  " + "\n  ".join(lines))
    errors = _semantic_errors(data)
    if errors:
        raise EngineDefinitionError(f"{source}: the engine definitions are inconsistent:\n  " + "\n  ".join(errors))
    return EngineCatalog(data=copy.deepcopy(data), source=source)


_MEMO: dict[tuple[Path, Path], tuple[tuple[int, int], EngineCatalog]] = {}


def load_engine_catalog(path: Path = DEFINITIONS_PATH, schema_path: Path = SCHEMA_PATH) -> EngineCatalog:
    """Read and validate the definitions file. Parsed again only when a file changes."""
    try:
        signature = (path.stat().st_mtime_ns, schema_path.stat().st_mtime_ns)
    except OSError as exc:
        raise EngineDefinitionError(f"{path}: cannot read the engine definitions ({exc.strerror or exc})") from None
    cached = _MEMO.get((path, schema_path))
    if cached and cached[0] == signature:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineDefinitionError(f"{path}: not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngineDefinitionError(f"{schema_path}: the schema is not valid JSON at line {exc.lineno}: {exc.msg}") from None
    catalog = parse_engine_catalog(data, schema=schema, source=str(path))
    _MEMO[(path, schema_path)] = (signature, catalog)
    return catalog
