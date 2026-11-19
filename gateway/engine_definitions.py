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

from gateway.capability_ids import MODEL_KIND_LABELS, offered_model_kinds

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
DEFINITIONS_PATH = _REGISTRY_DIR / "engine_definitions.json"
SCHEMA_PATH = _REGISTRY_DIR / "engine_definitions.schema.json"


# How a list probe reads one JSON answer as a list of ids. The vocabulary is closed: a definition names one of these, and
# ``gateway.engine_check.read_list`` has one reader for each. A shape that is not here is refused when the file loads.
LIST_SHAPES = ("array_of_strings", "array_of_objects", "object_keys", "tuple_first", "nested")
# The shapes that read ids out of objects, so the definition must say which keys hold the id.
_SHAPES_WITH_ID_KEYS = frozenset({"array_of_objects"})


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
            # A capability names the kinds it serves, so the form offers only what the ticked capabilities serve.
            item["capabilities"] = [
                {"id": cap_id, "label": capabilities[cap_id]["label"],
                 "kinds": offered_model_kinds(capabilities[cap_id]["record_capabilities"])}
                for cap_id in row["capabilities"]
            ]
            out.append(item)
        tests = {
            test_id: {"sent": test["sent"], "cost": test["cost"]}
            for test_id, test in self.data["real_tests"].items()
        }
        model_kinds = [{"id": kind, "label": label} for kind, label in MODEL_KIND_LABELS.items()]
        return {"format_version": self.data["format_version"], "definitions": out, "real_tests": tests,
                "model_kinds": model_kinds}


def _where(path: Any) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(top level)"


def _valid_pointer(pointer: Any) -> bool:
    """An RFC 6901 JSON pointer: empty, or segments each led by ``/`` with ``~`` only as ``~0`` or ``~1``."""
    return isinstance(pointer, str) and (pointer == "" or (pointer.startswith("/") and re.search(r"~(?![01])", pointer) is None))


def _list_probe_errors(where: str, definition: Mapping[str, Any]) -> list[str]:
    from gateway.engine_readiness import safe_probe_path

    errors: list[str] = []
    seen: set[str] = set()
    for probe in definition["probes"].get("lists") or ():
        here = f"{where}.probes.lists.{probe['id']}"
        if probe["id"] in seen:
            errors.append(f"{where}.probes.lists: two probes share an id ({probe['id']!r})")
        seen.add(probe["id"])
        if safe_probe_path(probe["path"]) is None:
            errors.append(f"{here}.path: {probe['path']!r} is not a plain server-root path")
        capability = probe.get("capability")
        if capability and capability not in definition["capabilities"]:
            errors.append(f"{here}.capability: the definition does not offer {capability!r}")
        read = probe["list"]
        shape = read["shape"]
        if shape not in LIST_SHAPES:
            errors.append(f"{here}.list.shape: {shape!r} is not a list shape; use one of {', '.join(LIST_SHAPES)}")
            continue
        for key in ("select", "default_select"):
            if key in read and not _valid_pointer(read[key]):
                errors.append(f"{here}.list.{key}: {read[key]!r} is not a JSON pointer (it starts with /)")
        if shape in _SHAPES_WITH_ID_KEYS and not read.get("id_keys"):
            errors.append(f"{here}.list.id_keys: the shape {shape} needs the keys that hold the id")
        if shape == "nested" and not read.get("inner"):
            errors.append(f"{here}.list.inner: the shape nested needs the key of the list inside each entry")
        if shape == "tuple_first" and "select" not in read:
            errors.append(f"{here}.list.select: the shape tuple_first needs a pointer to the tuple")
        if "where" in read and shape not in _SHAPES_WITH_ID_KEYS | {"tuple_first", "nested"}:
            errors.append(f"{here}.list.where: the shape {shape} reads no objects, so it has nothing to filter")
        if "default_select" in read and probe["offers"] != "voice":
            errors.append(f"{here}.list.default_select: only a voice list names a default")
    return errors


# The clips a multipart real test may upload. Each is generated in code (gateway.engine_check.SAMPLE_CLIPS), never recorded.
UPLOAD_SAMPLES = ("tone_wav",)
# What each kind of real test says it expects, and what only that kind carries.
_EXPECT_KEYS = {
    "binary_post": ("status", "content_type_prefix", "min_bytes"),
    "multipart_post": ("status", "json_string"),
    "get_probe": ("status",),
}
# What a kind of test may also say about what it expects, without having to.
_OPTIONAL_EXPECT_KEYS = {"get_probe": ("json_key",)}


def real_test_id(catalog: Any, definition: Mapping[str, Any], capability_id: str) -> str | None:
    """The real test this kind of server runs for a capability: its own choice, else the capability's, else none."""
    chosen = (definition.get("real_test_for") or {}).get(capability_id)
    return chosen or catalog.capability(capability_id).get("real_test")


def _real_test_errors(real_tests: Mapping[str, Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    for test_id, test in real_tests.items():
        kind = test.get("kind", "json_post")
        expect = test.get("expect")
        if kind in _EXPECT_KEYS:
            for key in _EXPECT_KEYS[kind]:
                if not isinstance(expect, Mapping) or key not in expect:
                    errors.append(f"real_tests.{test_id}.expect.{key}: a {kind} test says what it expects, including {key}")
            if isinstance(expect, Mapping):
                allowed = _EXPECT_KEYS[kind] + _OPTIONAL_EXPECT_KEYS.get(kind, ())
                errors += [f"real_tests.{test_id}.expect.{key}: a {kind} test does not use it" for key in expect if key not in allowed]
        elif expect is not None:
            errors.append(f"real_tests.{test_id}.expect: only a {' or '.join(_EXPECT_KEYS)} test has expect; this one is {kind}")
        path = test.get("path")
        if kind == "get_probe":
            from gateway.engine_readiness import safe_probe_path

            if not isinstance(path, str) or safe_probe_path(path) is None:
                errors.append(f"real_tests.{test_id}.path: a get_probe test reads a plain server-root path")
        elif path is not None:
            errors.append(f"real_tests.{test_id}.path: only a get_probe test reads a path; this one is {kind}")
        upload = test.get("file")
        if kind == "multipart_post":
            if not isinstance(upload, Mapping):
                errors.append(f"real_tests.{test_id}.file: a multipart_post test uploads a file, so it says which")
            elif upload.get("sample") not in UPLOAD_SAMPLES:
                errors.append(f"real_tests.{test_id}.file.sample: {upload.get('sample')!r} is not a sample clip; use one of {', '.join(UPLOAD_SAMPLES)}")
        elif upload is not None:
            errors.append(f"real_tests.{test_id}.file: only a multipart_post test uploads a file; this one is {kind}")
    return errors


def _dispatch_errors(dispatch: Mapping[str, Any]) -> list[str]:
    """The record a definition writes dispatches through an installed description: the description must exist and
    declare the operations the offering's selection names."""
    from gateway.engine_protocol import description

    doc = description.load_registry().get(dispatch["protocol"])
    if doc is None or doc.executor != "http-v1":
        return [f"protocol: no installed http-v1 description named {dispatch['protocol']!r}"]
    errors: list[str] = []
    selection = dispatch["offering"]["selection"]
    if selection.get("mode") == "switch":
        switch = selection.get("switch") or {}
        call = doc.operations.get(switch.get("call"))
        if call is None or call.op_class != "control":
            errors.append(f"offering.selection.switch.call: {switch.get('call')!r} is not a control operation of {doc.id}")
        if switch.get("observe_selected") not in (dispatch.get("observations") or {}):
            errors.append(f"offering.selection.switch.observe_selected: {switch.get('observe_selected')!r} is not in observations")
    for name, observation in (dispatch.get("observations") or {}).items():
        op = (observation or {}).get("op")
        if op is not None and (doc.operations.get(op) is None):
            errors.append(f"observations.{name}.op: {doc.id} declares no operation {op!r}")
    return errors


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
    errors += _real_test_errors(data["real_tests"])
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
        if "dispatch" in definition["record"]:
            errors += [f"{where}.record.dispatch.{message}" for message in _dispatch_errors(definition["record"]["dispatch"])]
        if "process_template" in definition:
            from gateway.process_template import template_errors

            errors += [f"{where}.process_template.{message}" for message in template_errors(definition["process_template"])]
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
        for cap_id, test in (definition.get("real_test_for") or {}).items():
            if cap_id not in definition["capabilities"]:
                errors.append(f"{where}.real_test_for: {cap_id!r} is not one of this kind's capabilities")
            if test not in data["real_tests"]:
                errors.append(f"{where}.real_test_for.{cap_id}: no real test named {test!r}")
        if definition["probes"]["primary"]["method"] != "GET":
            # The primary read doubles as the engine's readiness read (gateway/engine_readiness.py), so it must not change state.
            errors.append(f"{where}.probes.primary.method: the primary probe is the readiness read and must be GET")
        errors += _list_probe_errors(where, definition)
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
