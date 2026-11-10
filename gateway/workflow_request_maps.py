"""Request-map rows: where each workflow input goes in a route's request.

The bundled ``registry/workflow_request_maps.yml`` states as data what
``gateway.workflow_openapi._route_mapping`` states as code. A row selects records
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

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from gateway.manifests import media_bearing
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
_TOP_KEYS = frozenset({"schema_version", "maps"})
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


def load(path: str | Path = DEFAULT_PATH) -> RequestMaps:
    """Read the file once and return its read-only rows, or refuse a malformed row."""
    path = Path(path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: not valid YAML: {error}") from error
    if not isinstance(document, Mapping):
        raise ValueError(f"{path}: the file must be a mapping")
    unknown = set(document) - _TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown key {sorted(unknown)[0]!r}")
    if document.get("schema_version") != 1:
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
    return RequestMaps(path=path, rows=tuple(rows))


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

    out: dict[str, dict[str, Any]] = {}
    for name in ordered:
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


_MAPS = load(DEFAULT_PATH)


def select(record: Any, mode: str) -> Row | None:
    """The bundled row that claims ``record`` in ``mode``, or ``None``."""
    return _MAPS.select(record, mode)


def place(row: Row, record: Any, mode: str) -> dict[str, dict[str, Any]]:
    """Where each settable input of ``record`` goes under the bundled ``row``."""
    return _MAPS.place(row, record, mode)
