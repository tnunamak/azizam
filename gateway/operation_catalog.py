"""Typed, inert operation catalog data. No dispatch or context-free resolution."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator


class CatalogError(ValueError):
    """An invalid document, with a zero-based row and a named rule."""


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently accepting the last."""


def _mapping(loader: _UniqueLoader, node: Any, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise CatalogError("catalog row root: rule mapping_keys: keys must be strings")
        if key in result:
            raise CatalogError("catalog row root: rule duplicate_key: " + key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Catalog:
    operations: Mapping[str, Mapping[str, Any]]
    _rows: tuple[Mapping[str, Any], ...]
    unexpressed: tuple[Mapping[str, Any], ...] = ()

    def relations(
        self, kind: str, context: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        """Return inert rows in file order, optionally limited by context."""
        return tuple(
            row for row in self._rows
            if row["kind"] == kind
            and (context is None or row.get("context") == context)
        )


def _location(path: Any) -> str:
    parts = list(path)
    if len(parts) >= 2 and parts[0] in ("operations", "relations"):
        return f"{parts[0]} row {parts[1]}"
    return "catalog row root"


def _validate(document: Any, schema: dict) -> None:
    validator = Draft202012Validator(schema)
    error = next(validator.iter_errors(document), None)
    if error is None:
        return
    location = _location(error.absolute_path)
    rule = "schema." + error.validator
    # A oneOf failure otherwise hides the useful typed-row refusal.
    path = list(error.absolute_path)
    if len(path) >= 2 and path[0] == "relations":
        row = document["relations"][path[1]]
        if isinstance(row, dict):
            kind = row.get("kind")
            kinds = tuple(
                item["$ref"].rsplit("/", 1)[1]
                for item in schema["properties"]["relations"]["items"]["oneOf"]
            )
            if not isinstance(kind, str) or kind not in kinds:
                raise CatalogError(f"{location}: rule relation_kind: unknown or missing kind")
            if kind == "family" and (("operation" in row) == ("word" in row)):
                raise CatalogError(
                    f"{location}: rule family_target: exactly one of operation and word is required"
                )
            typed = Draft202012Validator({
                "$ref": "#/$defs/" + kind, "$defs": schema["$defs"]
            })
            detail = next(typed.iter_errors(row), None)
            if detail is not None:
                error = detail
                rule = "schema." + detail.validator
                if detail.validator == "additionalProperties":
                    rule = "no_execution_fields_or_untyped_fields"
    raise CatalogError(f"{location}: rule {rule}: {error.message}")


def load(path: str | Path) -> Catalog:
    """Load catalog YAML, reject invalid facts, and return deeply frozen data.

    The schema is shipped with the module's repository, not taken from the
    supplied catalog's directory. A catalog cannot choose a weaker schema.
    JSON is also accepted because it is a YAML subset.
    """
    source = Path(path)
    schema_path = Path(__file__).resolve().parents[1] / "registry" / "operations.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        document = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueLoader)
        # Reject recursive aliases, non-finite numbers and YAML-only values.
        json.dumps(document, allow_nan=False)
    except CatalogError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise CatalogError(f"catalog row root: rule parse: {exc}") from exc
    _validate(document, schema)

    operations = {}
    for index, row in enumerate(document["operations"]):
        identifier = row["id"]
        if identifier in operations:
            raise CatalogError(f"operations row {index}: rule unique_operation_id: {identifier}")
        operations[identifier] = _freeze(row)

    seen = set()
    seen_family_words = set()
    for index, row in enumerate(document["relations"]):
        kind = row["kind"]
        if kind == "family" and "word" in row:
            key = (row.get("context"), row["word"])
            if key in seen_family_words:
                raise CatalogError(
                    f"relations row {index}: rule unique_family_word_in_context: {key}"
                )
            seen_family_words.add(key)
            targets = ()
        else:
            targets = row["operations"] if kind == "catalog_group" else (row["operation"],)
        for target in targets:
            if target not in operations:
                raise CatalogError(
                    f"relations row {index}: rule defined_operation: {target}"
                )
        if kind == "spelling":
            key = (row["context"], row["word"])
            if key in seen:
                raise CatalogError(
                    f"relations row {index}: rule unique_spelling_in_context: {key}"
                )
            seen.add(key)
    return Catalog(
        MappingProxyType(operations),
        tuple(_freeze(row) for row in document["relations"]),
        tuple(_freeze(row) for row in document.get("unexpressed", [])),
    )


def project_capability_words(catalog: Catalog) -> dict[str, dict[str, str]]:
    """Project inert display words in source-map order, never executable facts."""
    result = {}
    for name, context in (
        ("protocol_words", "protocol"),
        ("route_intents", "route"),
        ("type_capabilities", "type"),
    ):
        rows = sorted(
            (row for row in catalog.relations("family", context) if "word" in row),
            key=lambda row: row.get("order", 0),
        )
        result[name] = {row["word"]: row["family"] for row in rows}
    return result


def project_models_legacy_spellings(catalog: Catalog) -> dict[str, str]:
    """Project only explicitly recorded legacy spellings, without normalization."""
    return {
        row["word"]: row["operation"]
        for row in catalog.relations("spelling", "legacy")
        if row["operation"] in catalog.operations
    }


def project_media_job_intents(catalog: Catalog) -> list[dict[str, Any]]:
    """Rebuild the media job intents table from catalog operations and their relations."""
    rows = []
    for row in sorted(catalog.relations("admission"), key=lambda row: row["order"]):
        operation = catalog.operations[row["operation"]]
        rebuilt = {"intent": row["intent"], "modality": operation["modality"],
                   "submittable": operation["submittable_as_job"]}
        for binding in catalog.relations("binding"):
            if binding["operation"] != operation["id"]:
                continue
            if "execution" in binding["modes"]:
                rebuilt["execution_route"] = binding["path"]
            else:
                rebuilt["route"] = binding["path"]
                rebuilt["component"] = operation["request"]["schema"].removeprefix("openapi:")
        rows.append(rebuilt)
    return rows
