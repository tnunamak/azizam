"""Request-map rows: where each workflow input goes in a route's request.

The bundled ``registry/workflow_request_maps.yml`` states where each input goes;
``gateway.workflow_openapi.request_mapping`` reads it through this module. A row selects records
by precedence -- a native route and mode, an exact intent and mode, an exact
intent in either mode, the longest intent prefix, then the default row -- and
then places every settable input with the first part that matches: ``first``
(ordering), ``inputs``, ``sets``, ``asset``, ``default``.

Route-managed sets stay in Python. A row names one by word and this module
resolves it with the existing constant or helper, so no member list is copied
into the file. ``select`` returns ``None`` only when no row matches an intent and
mode; the bundled rows cover every route branch, so the bundled file never does.

The file loads once at import into read-only structures. ``request_mapping``
keeps the media-bearing container post-step in Python; this module does not
repeat it.
"""

from __future__ import annotations

import math

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from gateway.manifests import media_bearing
from gateway.strict_yaml import safe_load_strict
from gateway.workflow_view import settable_inputs

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "registry" / "workflow_request_maps.yml"

# The placement words a row may use.
IN_WORDS = ("body", "form", "file", "file+extension", "extension", "none")
# Set words: static words come from a code constant, per-record words from a helper.
STATIC_SET_WORDS = (
    "image_generate_managed",
    "image_edit_managed",
    "audio_route_managed",
    "video_route_managed",
    "video_reference",
)
RECORD_SET_WORDS = ("unbound", "route_images")
SET_WORDS = STATIC_SET_WORDS + RECORD_SET_WORDS

_ROW_MODES = ("sync", "async", "any")
_NATIVE_MODES = ("sync", "async")
_TOP_KEYS = frozenset({"schema_version", "maps", "execution"})
_ROW_KEYS = frozenset({"select", "rename", "first", "inputs", "sets", "asset", "default"})
_SELECTOR_KEYS = frozenset({"native", "intent", "intent_prefix", "any", "mode"})
_PLACEMENT_KEYS = frozenset({"in", "field", "combine"})


@dataclass(frozen=True)
class Placement:
    """Where one input goes: an ``in`` word, an optional field, an optional combine pair."""

    where: str
    field: str | None = None
    combine: tuple[str, str] | None = None


@dataclass(frozen=True)
class Row:
    """One loaded map row. ``index`` is its 1-based position in the file."""

    index: int
    selector: tuple[str, ...]
    mode: str | None
    first: str | None
    inputs: Mapping[str, Placement]
    sets: tuple[tuple[str, Placement], ...]
    asset: Placement | None
    default: Placement | None
    rename: Mapping[str, str]


@dataclass(frozen=True)
class RequestMaps:
    """The file's rows, read-only and loaded once."""

    path: Path
    rows: tuple[Row, ...]
    execution: ExecutionMaps

    def select(self, record: Any, mode: str) -> Row | None:
        """The row that claims ``record`` in ``mode``, or ``None`` when none does."""
        return _select(self, record, mode)

    def place(self, row: Row, record: Any, mode: str) -> dict[str, dict[str, Any]]:
        """Where each settable input of ``record`` goes under ``row``."""
        return _place(self, row, record, mode)


def _refusal(path: Path, index: int, message: str) -> ValueError:
    return ValueError(f"{path}: maps row {index}: {message}")


def _word(path: Path, index: int, value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _refusal(path, index, f"{label} must be a non-empty string")
    return value


def _set_word(path: Path, index: int, value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in SET_WORDS:
        raise _refusal(path, index, f"{label}: unknown set word {value!r}")
    return value


def _placement(path: Path, index: int, raw: Any, label: str) -> Placement:
    if not isinstance(raw, Mapping):
        raise _refusal(path, index, f"{label}: a placement must be a mapping")
    unknown = set(raw) - _PLACEMENT_KEYS
    if unknown:
        raise _refusal(path, index, f"{label}: unknown key {sorted(unknown)[0]!r}")
    where = raw.get("in")
    if where not in IN_WORDS:
        raise _refusal(path, index, f"{label}: unknown in word {where!r}")
    field = raw.get("field")
    if field is not None:
        field = _word(path, index, field, f"{label} field")
    combine = raw.get("combine")
    if combine is not None:
        if (
            not isinstance(combine, list)
            or len(combine) != 2
            or any(not isinstance(name, str) or not name for name in combine)
        ):
            raise _refusal(path, index, f"{label}: combine must name two inputs")
        if field is None:
            raise _refusal(path, index, f"{label}: combine needs a field")
        combine = (combine[0], combine[1])
    if where == "none" and (field is not None or combine is not None):
        raise _refusal(path, index, f"{label}: a none placement takes no field")
    return Placement(where=where, field=field, combine=combine)


def _optional_placement(path: Path, index: int, raw: Any, label: str) -> Placement | None:
    return None if raw is None else _placement(path, index, raw, label)


def _selector(path: Path, index: int, raw: Any) -> tuple[tuple[str, ...], str | None]:
    if not isinstance(raw, Mapping):
        raise _refusal(path, index, "select must be a mapping")
    unknown = set(raw) - _SELECTOR_KEYS
    if unknown:
        raise _refusal(path, index, f"select: unknown key {sorted(unknown)[0]!r}")
    kinds = [key for key in ("native", "intent", "intent_prefix", "any") if key in raw]
    if len(kinds) != 1:
        raise _refusal(path, index, "select needs exactly one of native, intent, intent_prefix, any")
    kind = kinds[0]
    mode = raw.get("mode")
    if "mode" in raw and mode not in _ROW_MODES:
        raise _refusal(path, index, f"select: unknown mode {mode!r}")
    if kind == "native":
        if raw["native"] is not True:
            raise _refusal(path, index, "select: native must be true")
        if mode not in _NATIVE_MODES:
            raise _refusal(path, index, "select: a native row needs mode sync or async")
        return ("native", mode), mode
    if kind == "intent":
        intent = _word(path, index, raw["intent"], "select intent")
        if mode not in _ROW_MODES:
            raise _refusal(path, index, "select: an intent row needs mode sync, async or any")
        return ("intent", intent, mode), mode
    if kind == "intent_prefix":
        if "mode" in raw:
            raise _refusal(path, index, "select: an intent_prefix row takes no mode")
        return ("intent_prefix", _word(path, index, raw["intent_prefix"], "select intent_prefix")), None
    if "mode" in raw:
        raise _refusal(path, index, "select: an any row takes no mode")
    if raw["any"] is not True:
        raise _refusal(path, index, "select: any must be true")
    return ("any",), None


def _row(path: Path, index: int, raw: Any) -> Row:
    if not isinstance(raw, Mapping):
        raise _refusal(path, index, "must be a mapping")
    unknown = set(raw) - _ROW_KEYS
    if unknown:
        raise _refusal(path, index, f"unknown key {sorted(unknown)[0]!r}")
    if "select" not in raw:
        raise _refusal(path, index, "a row needs a select")
    selector, mode = _selector(path, index, raw["select"])

    first = raw.get("first")
    if first is not None:
        first = _set_word(path, index, first, "first")

    inputs = raw.get("inputs", {})
    if not isinstance(inputs, Mapping):
        raise _refusal(path, index, "inputs must be a mapping")
    input_placements = MappingProxyType(
        {
            _word(path, index, name, "inputs name"): _placement(path, index, placement, f"inputs.{name}")
            for name, placement in inputs.items()
        }
    )

    raw_sets = raw.get("sets", [])
    if not isinstance(raw_sets, list):
        raise _refusal(path, index, "sets must be a list")
    sets: list[tuple[str, Placement]] = []
    for number, entry in enumerate(raw_sets, 1):
        if not isinstance(entry, Mapping) or set(entry) != {"word", "placement"}:
            raise _refusal(path, index, f"sets {number}: needs a word and a placement")
        sets.append(
            (
                _set_word(path, index, entry["word"], f"sets {number}"),
                _placement(path, index, entry["placement"], f"sets {number}"),
            )
        )

    rename = raw.get("rename", {})
    if not isinstance(rename, Mapping):
        raise _refusal(path, index, "rename must be a mapping")
    renames = MappingProxyType(
        {
            _word(path, index, name, "rename name"): _word(path, index, field, f"rename {name}")
            for name, field in rename.items()
        }
    )

    return Row(
        index=index,
        selector=selector,
        mode=mode,
        first=first,
        inputs=input_placements,
        sets=tuple(sets),
        asset=_optional_placement(path, index, raw.get("asset"), "asset"),
        default=_optional_placement(path, index, raw.get("default"), "default"),
        rename=renames,
    )


def load(path: str | Path = DEFAULT_PATH, *, records: Any = None) -> RequestMaps:
    """Read the file once and return its read-only rows, or refuse a malformed row."""
    path = Path(path)
    try:
        document = safe_load_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{path}: cannot read registry: {error}") from error
    except (yaml.YAMLError, ValueError) as error:
        raise ValueError(f"{path}: not valid YAML: {error}") from error
    if not isinstance(document, Mapping):
        raise ValueError(f"{path}: the file must be a mapping")
    if any(not isinstance(key, str) or not key for key in document):
        raise ValueError(f"{path}: top-level keys must be non-empty strings")
    unknown = set(document) - _TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown key {sorted(unknown)[0]!r}")
    version = document.get("schema_version")
    if type(version) is not int or version != 1:  # True and 1.0 compare equal to 1; refuse them.
        raise ValueError(f"{path}: schema_version must be 1")
    raw_rows = document.get("maps")
    if not isinstance(raw_rows, list):
        raise ValueError(f"{path}: maps must be a list")
    rows: list[Row] = []
    seen: dict[tuple[str, ...], int] = {}
    for index, raw in enumerate(raw_rows, 1):
        row = _row(path, index, raw)
        if row.selector in seen:
            raise _refusal(path, index, f"same selector as row {seen[row.selector]}")
        seen[row.selector] = index
        rows.append(row)
    execution = _ExecutionReader(path).read(document.get("execution"))
    if records is not None:
        execution.validate_records(records)
    return RequestMaps(path=path, rows=tuple(rows), execution=execution)


# --------------------------------------------------------------- request map


def _workflow_openapi() -> Any:
    """The helper module, imported at call time so moving ``request_mapping`` here cannot make a cycle."""
    from gateway import workflow_openapi

    return workflow_openapi


def _static_set(word: str) -> frozenset[str]:
    from gateway.audio_extensions import ROUTE_MANAGED_AUDIO_INPUTS
    from gateway.video_extensions import ROUTE_MANAGED_VIDEO_INPUTS

    openapi = _workflow_openapi()
    return {
        "image_generate_managed": openapi._IMAGE_GENERATE_MANAGED,
        "image_edit_managed": openapi._IMAGE_EDIT_MANAGED,
        "audio_route_managed": ROUTE_MANAGED_AUDIO_INPUTS,
        "video_route_managed": ROUTE_MANAGED_VIDEO_INPUTS,
        "video_reference": frozenset({openapi.VIDEO_REFERENCE_INPUT}),
    }[word]


def _members(
    word: str,
    record: Any,
    inputs: Mapping[str, Any],
    is_asset: Mapping[str, bool],
) -> tuple[str, ...]:
    """The record's inputs a set word names, in the order that word's members come."""
    openapi = _workflow_openapi()
    if word == "unbound":
        bound = openapi._bound_input_names(record)
        return tuple(name for name in inputs if name not in bound)
    if word == "route_images":
        return tuple(openapi._native_route_image_inputs(record, is_asset))
    members = _static_set(word)
    return tuple(name for name in inputs if name in members)


def _select(maps: RequestMaps, record: Any, mode: str) -> Row | None:
    native = _workflow_openapi().gateway_route(record)
    intent = record.intent
    for row in maps.rows:
        if row.selector[0] == "native" and native and row.mode == mode:
            return row
    for row in maps.rows:
        if row.selector[0] == "intent" and row.mode == mode and row.selector[1] == intent:
            return row
    for row in maps.rows:
        if row.selector[0] == "intent" and row.mode == "any" and row.selector[1] == intent:
            return row
    prefixes = [
        row for row in maps.rows if row.selector[0] == "intent_prefix" and intent.startswith(row.selector[1])
    ]
    if prefixes:
        return max(prefixes, key=lambda row: len(row.selector[1]))
    for row in maps.rows:
        if row.selector[0] == "any":
            return row
    return None


def _entry(name: str, placement: Placement, rename: Mapping[str, str]) -> dict[str, Any]:
    if placement.where == "none":
        return {"in": "none"}
    entry: dict[str, Any] = {
        "in": placement.where,
        "field": placement.field or rename.get(name, name),
    }
    if placement.combine is not None:
        entry["combine"] = list(placement.combine)
    return entry


def _place(maps: RequestMaps, row: Row, record: Any, mode: str) -> dict[str, dict[str, Any]]:
    inputs = settable_inputs(record.contract)
    is_asset = {
        name: media_bearing(spec) and (spec or {}).get("type") == "asset"
        for name, spec in inputs.items()
    }
    words = {word for word, _ in row.sets}
    if row.first is not None:
        words.add(row.first)
    resolved = {word: set(_members(word, record, inputs, is_asset)) for word in words}

    ordered: list[str] = []
    if row.first is not None:
        for name in _members(row.first, record, inputs, is_asset):
            if name in inputs and name not in ordered:
                ordered.append(name)
    for name in inputs:
        if name not in ordered:
            ordered.append(name)

    # A member of the ``first`` set takes that set's placement before any named input: the edit route
    # carries every route picture as a repeated part, whatever the input is called.
    first_placement = next((candidate for word, candidate in row.sets if word == row.first), None)
    out: dict[str, dict[str, Any]] = {}
    for name in ordered:
        placement = None
        if first_placement is not None and name in resolved[row.first]:
            placement = first_placement
        if placement is None:
            placement = row.inputs.get(name)
        if placement is None:
            for word, candidate in row.sets:
                if name in resolved[word]:
                    placement = candidate
                    break
        if placement is None and is_asset.get(name) and row.asset is not None:
            placement = row.asset
        if placement is None:
            placement = row.default
        if placement is None:
            raise ValueError(f"{maps.path}: row {row.index}: no placement matches input {name!r}")
        out[name] = _entry(name, placement, row.rename)
    return out


# ---------------------------------------------------------- execution data

@dataclass(frozen=True)
class ExecutionRow:
    model_prefix: str
    route_inputs: tuple[str, ...]
    sync: Mapping[str, Any] | None
    async_: Mapping[str, Any] | None
    native_alternate: Mapping[str, Any] | None


@dataclass(frozen=True)
class ExecutionMaps:
    path: Path
    field_rows: Mapping[str, Mapping[str, Any]]
    native_sync: Mapping[str, Any]
    sync_templates: Mapping[str, Mapping[str, Any]]
    async_templates: Mapping[str, Mapping[str, Any]]
    intents: Mapping[str, ExecutionRow]

    def row(self, intent: str) -> ExecutionRow | None:
        """The row for ``intent``; None means the intent has no templated routes (only a native or run route)."""
        return self.intents.get(intent)

    def validate_records(self, records: Any) -> None:
        for record in records:
            row = self.row(record.intent)
            if row is not None and row.sync is None and row.async_ is None \
                    and not _workflow_openapi().gateway_route(record):
                raise ValueError(
                    f"{self.path}: execution.intents.{record.intent}: "
                    f"record {record.id!r} needs a native binding when both routes are null"
                )

    def render_sync(self, record: Any, *, native: bool = False) -> dict[str, Any] | None:
        row = self.row(record.intent)
        if not native and row is None:
            return None
        template = self.native_sync if native else row.sync
        result = self._render(template, record, row)
        if native and row is not None and row.native_alternate is not None:
            result["alternate"] = self._render(row.native_alternate, record, row)
        return result

    def render_async(self, record: Any) -> dict[str, Any] | None:
        row = self.row(record.intent)
        return None if row is None else self._render(row.async_, record, row)

    def render_alternate(self, row: ExecutionRow, *, model: str) -> dict[str, Any] | None:
        return _render_execution(row.native_alternate, lambda _slot: model)

    def _render(self, template: Any, record: Any, row: ExecutionRow | None) -> Any:
        openapi = _workflow_openapi()

        def slot(value: Mapping[str, Any]) -> Any:
            source = value["from"]
            if source == "model":
                return row.model_prefix + openapi.short_id(record)
            if source == "native_path":
                return openapi.gateway_route(record)
            if source == "workflow_run_path":
                return f"/gateway/v1/workflows/{record.id}/run"
            return openapi.request_mapping(record, value["mode"])

        return _render_execution(template, slot)


def _freeze_execution(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_execution(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_execution(child) for child in value)
    return value


def _render_execution(value: Any, slot: Any) -> Any:
    if isinstance(value, Mapping):
        if "from" in value:
            return slot(value)
        return {key: _render_execution(child, slot) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_render_execution(child, slot) for child in value]
    return value


class _ExecutionReader:
    """Validate only closed wire structures; row/template names are opaque data."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def fail(self, label: str, message: str) -> None:
        raise ValueError(f"{self.path}: {label}: {message}")

    def obj(self, value: Any, label: str, required: set[str], optional: set[str] | None = None) -> Mapping:
        if not isinstance(value, Mapping):
            self.fail(label, "must be a mapping")
        if any(not isinstance(key, str) or not key for key in value):
            self.fail(label, "keys must be non-empty strings")
        unknown = set(value) - required - (optional or set())
        if unknown:
            self.fail(label, f"unknown key {sorted(unknown)[0]!r}")
        missing = required - set(value)
        if missing:
            self.fail(label, f"missing key {sorted(missing)[0]!r}")
        return value

    def text(self, value: Any, label: str, *, empty: bool = False) -> str:
        if not isinstance(value, str) or (not empty and not value.strip()):
            self.fail(label, "must be a string" if empty else "must be a non-empty string")
        return value

    def words(self, value: Any, label: str, *, nonempty: bool = False) -> list[str]:
        if not isinstance(value, list) or (nonempty and not value):
            self.fail(label, "must be a non-empty list" if nonempty else "must be a list")
        for word in value:
            self.text(word, label)
        if len(set(value)) != len(value):
            self.fail(label, "duplicate values")
        return value

    def named(self, value: Any, label: str) -> Mapping:
        if not isinstance(value, Mapping):
            self.fail(label, "must be a mapping")
        for key in value:
            self.text(key, label)
        return value

    def slot(self, value: Any, label: str, source: str, *, mode: str | None = None) -> None:
        keys = {"from", "mode"} if source == "fields" else {"from"}
        value = self.obj(value, label, keys)
        if value["from"] != source:
            self.fail(label, f"expected slot from {source!r}")
        if source == "fields":
            if value["mode"] not in ("sync", "async"):
                self.fail(label, "mode must be sync or async")
            if mode is not None and value["mode"] != mode:
                self.fail(label, f"mode must be {mode}")

    def path_value(self, value: Any, label: str, *, source: str | None = None) -> None:
        if isinstance(value, Mapping):
            if source is None:
                self.fail(label, "a slot is not allowed here")
            self.slot(value, label, source)
            return
        self.text(value, label)
        if (not value.startswith("/") or value.startswith("//") or "\\" in value
                or "?" in value or "#" in value or any(char.isspace() for char in value)
                or any(part in (".", "..") for part in value.split("/"))):
            self.fail(label, "must be a resource-relative path")
        # Braces in these API placeholders stay literal, never interpolation.
        literal = value.replace("{job_id}", "").replace("{video_id}", "")
        if "{" in literal or "}" in literal or "$" in literal:
            self.fail(label, "unsupported path placeholder")

    def response(self, value: Any, label: str) -> None:
        value = self.obj(value, label, {"kind"}, {"media_type", "poll", "content"})
        kind = value["kind"]
        if kind == "json":
            self.obj(value, label, {"kind"})
        elif kind == "binary":
            self.obj(value, label, {"kind"}, {"media_type"})
            if "media_type" in value:
                self.text(value["media_type"], label + ".media_type")
        elif kind == "media_job":
            self.obj(value, label, {"kind", "poll", "content"})
            for key in ("poll", "content"):
                self.path_value(value[key], label + "." + key)
        else:
            self.fail(label + ".kind", "unsupported response kind")

    def sync(self, value: Any, label: str, *, native: bool = False) -> None:
        required = {"method", "path", "content_type", "fields", "output", "response"}
        value = self.obj(value, label, required, set() if native else {"model", "extensions_field"})
        if value["method"] != "POST":
            self.fail(label + ".method", "must be POST")
        if native:
            self.slot(value["path"], label + ".path", "native_path")
        else:
            self.path_value(value["path"], label + ".path", source="workflow_run_path")
        for key in ("content_type", "output", "extensions_field"):
            if key in value:
                self.text(value[key], label + "." + key)
        if value["content_type"] not in ("application/json", "multipart/form-data"):
            self.fail(label + ".content_type", "unsupported request content type")
        if native and value["content_type"] != "multipart/form-data":
            self.fail(label + ".content_type", "native route must be multipart/form-data")
        self.slot(value["fields"], label + ".fields", "fields", mode="sync")
        if "model" in value:
            self.slot(value["model"], label + ".model", "model")
        self.response(value["response"], label + ".response")

    def alternate(self, value: Any, label: str) -> None:
        value = self.obj(value, label, {"method", "path", "model", "note"})
        if value["method"] != "POST":
            self.fail(label + ".method", "must be POST")
        self.path_value(value["path"], label + ".path")
        self.slot(value["model"], label + ".model", "model")
        self.text(value["note"], label + ".note")

    def async_template(self, value: Any, label: str) -> None:
        value = self.obj(value, label, {
            "kind", "submit", "poll", "content", "cancel", "statuses", "terminal", "fields",
        }, {"output"})
        # Published transport discriminators, not workflow identities.
        transports = {"media_job": ("body", "async", "POST"),
                      "respond_async": ("headers", "sync", "POST"),
                      "video": (None, "async", "DELETE")}
        kind = self.text(value["kind"], label + ".kind")
        if kind not in transports:
            self.fail(label + ".kind", "unsupported async transport")
        payload, mode, cancel_method = transports[kind]
        submit = self.obj(value["submit"], label + ".submit", {"method", "path", "content_type"},
                          {"body", "model", "headers"})
        if submit["method"] != "POST":
            self.fail(label + ".submit.method", "must be POST")
        self.path_value(submit["path"], label + ".submit.path")
        expected_type = "multipart/form-data" if payload == "headers" else "application/json"
        if submit["content_type"] != expected_type:
            self.fail(label + ".submit.content_type", f"must be {expected_type}")
        if "model" in submit:
            self.slot(submit["model"], label + ".submit.model", "model")
        for key in ("body", "headers"):
            if (key in submit) != (payload == key):
                self.fail(label + ".submit." + key, "incompatible async transport shape")
        if payload == "body":
            body = self.obj(submit["body"], label + ".submit.body", {"intent", "request"})
            for key in body:
                self.text(body[key], label + ".submit.body." + key)
        if payload == "headers":
            headers = self.obj(submit["headers"], label + ".submit.headers", {"Prefer"})
            if headers["Prefer"] != "respond-async":
                self.fail(label + ".submit.headers.Prefer", "must be respond-async")
        for key, optional, method in (
            ("poll", set(), "GET"), ("content", {"output"}, "GET"),
            ("cancel", {"note"}, cancel_method),
        ):
            endpoint = self.obj(value[key], label + "." + key, {"method", "path"}, optional)
            if endpoint["method"] != method:
                self.fail(label + "." + key + ".method", f"must be {method}")
            self.path_value(endpoint["path"], label + "." + key + ".path")
            for extra in optional & set(endpoint):
                self.text(endpoint[extra], label + "." + key + "." + extra)
        statuses = self.words(value["statuses"], label + ".statuses", nonempty=True)
        terminal = self.words(value["terminal"], label + ".terminal", nonempty=True)
        if not set(terminal) <= set(statuses):
            self.fail(label + ".terminal", "must be a subset of statuses")
        self.slot(value["fields"], label + ".fields", "fields", mode=mode)
        if "output" in value:
            self.text(value["output"], label + ".output")

    def field(self, value: Any, label: str) -> None:
        value = self.obj(value, label, {"unless_all_inputs", "value"})
        self.words(value["unless_all_inputs"], label + ".unless_all_inputs")
        field = self.obj(value["value"], label + ".value",
                         {"name", "type", "description", "in", "field"},
                         {"minimum", "maximum", "label"})
        for key in ("name", "type", "description", "in", "field", "label"):
            if key in field:
                self.text(field[key], label + ".value." + key)
        if field["type"] not in ("string", "integer", "number", "boolean"):
            self.fail(label + ".value.type", "unsupported scalar type")
        if field["in"] not in ("body", "form"):
            self.fail(label + ".value.in", "must be body or form")
        for key in ("minimum", "maximum"):
            if key in field:
                number = field[key]
                if (type(number) not in (int, float) or not math.isfinite(number)
                        or field["type"] not in ("integer", "number")
                        or (field["type"] == "integer" and type(number) is not int)):
                    self.fail(label + ".value." + key, "must be a finite bound for the scalar type")
        if "minimum" in field and "maximum" in field and field["minimum"] > field["maximum"]:
            self.fail(label + ".value", "minimum exceeds maximum")

    def read(self, raw: Any) -> ExecutionMaps:
        raw = self.obj(raw, "execution", {
            "field_rows", "native_sync", "sync_templates", "async_templates", "intents",
        })
        fields = self.named(raw["field_rows"], "execution.field_rows")
        for name, value in fields.items():
            self.field(value, f"execution.field_rows.{name}")
        self.sync(raw["native_sync"], "execution.native_sync", native=True)
        sync = self.named(raw["sync_templates"], "execution.sync_templates")
        async_ = self.named(raw["async_templates"], "execution.async_templates")
        for name, value in sync.items():
            self.sync(value, f"execution.sync_templates.{name}")
        for name, value in async_.items():
            self.async_template(value, f"execution.async_templates.{name}")
        sync, async_ = _freeze_execution(sync), _freeze_execution(async_)
        intents: dict[str, ExecutionRow] = {}
        for intent, value in self.named(raw["intents"], "execution.intents").items():
            label = f"execution.intents.{intent}"
            value = self.obj(value, label, {"model_prefix", "route_inputs", "sync", "async", "native_alternate"})
            prefix = self.text(value["model_prefix"], label + ".model_prefix", empty=True)
            refs = self.words(value["route_inputs"], label + ".route_inputs")
            output_names: set[str] = set()
            for ref in refs:
                if ref not in fields:
                    self.fail(label + ".route_inputs", f"unknown field row {ref!r}")
                name = fields[ref]["value"]["name"]
                if name in output_names:
                    self.fail(label + ".route_inputs", f"duplicate output field {name!r}")
                output_names.add(name)
            resolved = []
            for key, templates in (("sync", sync), ("async", async_)):
                ref = value[key]
                if ref is not None:
                    self.text(ref, label + "." + key)
                    if ref not in templates:
                        self.fail(label + "." + key, f"unknown template {ref!r}")
                resolved.append(templates[ref] if ref is not None else None)
            alternate = value["native_alternate"]
            if alternate is not None:
                self.alternate(alternate, label + ".native_alternate")
            intents[intent] = ExecutionRow(
                prefix, tuple(refs), resolved[0], resolved[1], _freeze_execution(alternate),
            )
        if not intents:
            self.fail("execution.intents", "must declare at least one intent")
        return ExecutionMaps(
            self.path, _freeze_execution(fields), _freeze_execution(raw["native_sync"]),
            sync, async_, MappingProxyType(intents),
        )


_MAPS = load(DEFAULT_PATH)


def select(record: Any, mode: str) -> Row | None:
    """The bundled row that claims ``record`` in ``mode``, or ``None``."""
    return _MAPS.select(record, mode)


def place(row: Row, record: Any, mode: str) -> dict[str, dict[str, Any]]:
    """Where each settable input of ``record`` goes under the bundled ``row``."""
    return _MAPS.place(row, record, mode)



def execution_row(intent: str) -> ExecutionRow | None:
    """Exact lookup; None means the intent has no templated sync or async route."""
    return _MAPS.execution.row(intent)


def validate_execution_records(records: Any) -> None:
    """Refuse incomplete execution data before publishing the current records."""
    _MAPS.execution.validate_records(records)


def render_alternate(row: ExecutionRow, *, model: str) -> dict[str, Any] | None:
    return _MAPS.execution.render_alternate(row, model=model)


def render_sync(record: Any) -> dict[str, Any] | None:
    """Render the exact intent row's sync route, or None when absent."""
    return _MAPS.execution.render_sync(record)


def render_async(record: Any) -> dict[str, Any] | None:
    """Render the exact intent row's async route, or None when absent."""
    return _MAPS.execution.render_async(record)
