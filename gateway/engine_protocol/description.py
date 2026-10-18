"""Protocol descriptions: load, validate and compile ``registry/protocols/*.yml``.

A description is data (contract section 4). This module is the only reader: it
parses YAML (duplicate keys are an error), checks the closed JSON Schema, then
checks what a schema cannot (pointers, paths, mapping rules, budgets, which
parts may appear together) and compiles every map once. A bad file raises
``DescriptionError`` listing *every* problem with its place, so an author fixes
the file in one pass. It names no engine.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

from gateway.engine_protocol import mapping, pointer
from gateway.engine_protocol.budgets import DEFAULT_BUDGETS, Budgets
from gateway.engine_protocol.errors import DescriptionError

REGISTRY_DIR = Path(__file__).resolve().parent.parent.parent / "registry" / "protocols"
SCHEMA_PATH = REGISTRY_DIR / "protocol.schema.json"

JOB_STATES = ("pending", "running", "succeeded", "failed", "cancelled")
# Grammar keys under which a value is author data (names, constants, tables), not grammar.
_DATA_KEYS = frozenset({"table", "headers", "select", "states", "value", "default", "equals", "in", "operations"})
_CODE_NAMES = frozenset({
    "class_name", "script", "expression", "expr", "python", "template", "eval", "exec", "function",
    "lambda", "import", "module", "plugin", "callable", "hook", "jinja", "jq", "jmespath", "cel",
})
_CREDENTIAL_HEADER = re.compile(r"auth|cookie|token|secret|key|credential|password|session", re.IGNORECASE)
_TRANSPORT_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "content-type", "accept-encoding", "upgrade", "te"})
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_PLACEHOLDER_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")  # these are used with fullmatch: "$" would accept a trailing newline
_PATH_CHARS = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")


# ---------------------------------------------------------------------------
# The compiled form
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthSpec:
    kind: str                    # bearer | header | query | none
    name: str | None = None      # header name (header kind; bearer defaults to Authorization) or query parameter
    scheme: str | None = None    # word before the secret ("Bearer")


@dataclass(frozen=True)
class Advice:
    statuses: tuple[int, ...]
    code: str
    advice: str


@dataclass(frozen=True)
class QuerySpec:
    name: str
    segments: pointer.Segments
    default: Any = pointer.MISSING


@dataclass(frozen=True)
class PartSpec:
    name: str
    kind: str                    # field | json | file
    segments: pointer.Segments
    required: bool = False
    default: Any = pointer.MISSING


@dataclass(frozen=True)
class BodySpec:
    encoding: str = "none"       # none | json | multipart
    program: mapping.Program | None = None
    parts: tuple[PartSpec, ...] = ()


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    placeholders: tuple[str, ...] = ()
    query: tuple[QuerySpec, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    body: BodySpec = field(default_factory=BodySpec)


@dataclass(frozen=True)
class Terminal:
    sentinel: str | None = None
    event: str | None = None
    segments: pointer.Segments | None = None
    equals: Any = None


@dataclass(frozen=True)
class StreamSpec:
    when: Any                    # predicate over the request source
    framing: str                 # sse | ndjson
    terminal: Terminal | None = None
    program: mapping.Program | None = None


@dataclass(frozen=True)
class ResponseSpec:
    framing: str = "json"        # json | sse | ndjson | binary | none
    select: Mapping[str, pointer.Segments] = field(default_factory=dict)
    program: mapping.Program | None = None
    terminal: Terminal | None = None
    stream: StreamSpec | None = None


@dataclass(frozen=True)
class StatusSpec:
    segments: pointer.Segments
    states: Mapping[str, str]


@dataclass(frozen=True)
class SubmitSpec:
    request: RequestSpec
    id_segments: pointer.Segments


@dataclass(frozen=True)
class PollSpec:
    request: RequestSpec
    status: StatusSpec
    error_segments: pointer.Segments | None
    assets_segments: pointer.Segments | None
    interval_s: float
    max_wait_s: float


@dataclass(frozen=True)
class JobSpec:
    submit: SubmitSpec
    poll: PollSpec
    fetch: RequestSpec | None = None
    cancel: RequestSpec | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    op_class: str                # inventory | generate | control
    request: RequestSpec | None = None
    response: ResponseSpec | None = None
    job: JobSpec | None = None


@dataclass(frozen=True)
class ProtocolDescription:
    id: str
    title: str
    executor: str
    source: str
    operations: Mapping[str, Operation]
    auth: Mapping[str, AuthSpec] = field(default_factory=dict)
    errors: tuple[Advice, ...] = ()
    budgets: Budgets = DEFAULT_BUDGETS
    serves: tuple[str, ...] = ()
    label: str = ""
    binding: str = ""

    @property
    def adapter_id(self) -> str | None:
        return self.executor.split(":", 1)[1] if self.executor.startswith("adapter:") else None

    def advice_for(self, status: int) -> Advice | None:
        for row in self.errors:
            if status in row.statuses:
                return row
        return None


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key instead of letting the last one win."""


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "a mapping key must be a plain value", key_node.start_mark) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def parse_description_text(text: str, *, source: str, budgets: Budgets = DEFAULT_BUDGETS) -> ProtocolDescription:
    try:
        # Refuse aliases and anchors on the event stream, before the composer expands them. A
        # short alias chain otherwise multiplies into millions of nodes (about 10x per level).
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise DescriptionError(source, ["YAML aliases and anchors are not allowed; write each value out"])
        doc = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown line"
        what = getattr(exc, "problem", None) or str(exc)
        raise DescriptionError(source, [f"YAML at {where}: {what}"]) from None
    return parse_description(doc, source=source, budgets=budgets)


# ---------------------------------------------------------------------------
# Schema and semantic checks
# ---------------------------------------------------------------------------

_SCHEMA: dict[str, Any] | None = None


def _schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA


def _place(parts: Any) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(file)"


def _scan_code_names(node: Any, path: tuple, problems: list[str], flagged: list[tuple]) -> None:
    """A grammar key that names code is refused by name. Author data (tables, constants) is not scanned."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                problems.append(f"{_place(path)}: keys must be strings, found {key!r}")
                continue
            if key in _CODE_NAMES and not (path and path[-1] in _DATA_KEYS):
                problems.append(f"{_place(path)}: field '{key}' names code; descriptions are data only")
                flagged.append((path, key))
                continue
            if key in _DATA_KEYS:
                continue
            _scan_code_names(value, path + (key,), problems, flagged)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_code_names(value, path + (index,), problems, flagged)


def parse_description(doc: Any, *, source: str, budgets: Budgets = DEFAULT_BUDGETS) -> ProtocolDescription:
    """Validate ``doc`` and compile it. Raises ``DescriptionError`` listing every problem."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise DescriptionError(source, ["the file must hold one mapping at the top level"])
    flagged: list[tuple] = []
    # `operations` keys are names: scan inside each operation, not its name.
    shadow = copy.deepcopy(doc)
    _scan_code_names({k: v for k, v in doc.items() if k != "operations"}, (), problems, flagged)
    for name, op in (doc.get("operations") or {}).items() if isinstance(doc.get("operations"), dict) else ():
        _scan_code_names(op, ("operations", name), problems, flagged)
    for path, key in flagged:
        node: Any = shadow
        for step in path:
            node = node[step]
        node.pop(key, None)
    validator = jsonschema.Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(shadow), key=lambda e: [str(p) for p in e.absolute_path])
    for error in errors:
        problems.append(f"{_place(error.absolute_path)}: {error.message}")
    if problems:
        raise DescriptionError(source, problems)
    parsed = _Compiler(doc, source, budgets).compile()
    return parsed


class _Compiler:
    def __init__(self, doc: dict, source: str, budgets: Budgets) -> None:
        self.doc = doc
        self.source = source
        self.budgets = budgets
        self.problems: list[str] = []

    def bad(self, place: str, message: str) -> None:
        self.problems.append(f"{place}: {message}")

    def compile(self) -> ProtocolDescription:
        doc = self.doc
        executor = doc["executor"]
        auth = {cls: AuthSpec(spec["kind"], spec.get("name"), spec.get("scheme")) for cls, spec in (doc.get("auth") or {}).items()}
        for cls, spec in auth.items():
            if spec.kind in ("header", "query") and not spec.name:
                self.bad(f"auth.{cls}", f"kind '{spec.kind}' needs 'name'")
        advice = self._advice(doc.get("errors") or [])
        if doc.get("serves") and not executor.startswith("adapter:"):
            self.bad("serves", "only a reviewed adapter answers for a record that names no protocol; an http-v1 description is selected by name")
        operations = {name: self._operation(name, op, executor) for name, op in doc["operations"].items()}
        if self.problems:
            raise DescriptionError(self.source, self.problems)
        return ProtocolDescription(
            id=doc["id"], title=doc.get("title", doc["id"]), executor=executor, source=self.source,
            operations={k: v for k, v in operations.items() if v is not None}, auth=auth, errors=advice, budgets=self.budgets,
            serves=tuple(doc.get("serves") or ()), label=str(doc.get("label") or ""),
            binding=str(doc.get("binding") or ""),
        )

    def _advice(self, rows: list) -> tuple[Advice, ...]:
        out: list[Advice] = []
        seen: dict[int, str] = {}
        for index, row in enumerate(rows):
            statuses = tuple(row["status"]) if isinstance(row["status"], list) else (row["status"],)
            for status in statuses:
                if status in seen:
                    self.bad(f"errors[{index}]", f"status {status} is already covered by '{seen[status]}'")
                seen[status] = row["code"]
            out.append(Advice(statuses, row["code"], row["advice"]))
        return tuple(out)

    # --- operations ----------------------------------------------------------------------------

    def _operation(self, name: str, op: dict, executor: str) -> Operation | None:
        place = f"operations.{name}"
        has = [key for key in ("request", "job") if key in op]
        if executor.startswith("adapter:"):
            if has or "response" in op:
                self.bad(place, f"an adapter executor declares names only: the adapter owns the wire, so '{(has or ['response'])[0]}' is not allowed")
            return Operation(name, op["class"])
        if "request" in op and "job" in op:
            self.bad(place, "declare either 'request' or 'job', not both")
            return None
        if not has:
            self.bad(place, "an http-v1 operation needs a 'request' or a 'job'")
            return None
        if "job" in op:
            if "response" in op:
                self.bad(place, "a job declares its own responses; remove 'response'")
            return Operation(name, op["class"], job=self._job(op["job"], f"{place}.job"))
        request = self._request(op["request"], f"{place}.request")
        response = self._response(op.get("response") or {}, f"{place}.response", request)
        return Operation(name, op["class"], request=request, response=response)

    # --- requests ------------------------------------------------------------------------------

    def _path(self, path: str, place: str) -> tuple[str, ...]:
        names: list[str] = []
        for raw in _PLACEHOLDER.findall(path):
            if not _PLACEHOLDER_NAME.fullmatch(raw):
                self.bad(place, f"placeholder {{{raw}}} must be a plain lower-case name")
            names.append(raw)
        bare = _PLACEHOLDER.sub("x", path)
        if not path.startswith("/") or "//" in path or not _PATH_CHARS.fullmatch(bare):
            self.bad(place, f"path {path!r} must be a relative path that starts with / (no host, query, fragment, space or backslash)")
        elif any(segment in (".", "..") for segment in bare.split("/")):
            self.bad(place, f"path {path!r} must not hold . or .. segments")
        return tuple(names)

    def _request(self, spec: dict, place: str) -> RequestSpec:
        method = spec["method"]
        names = self._path(spec["path"], f"{place}.path")
        headers: dict[str, str] = {}
        for header, value in (spec.get("headers") or {}).items():
            if _CREDENTIAL_HEADER.search(header):
                self.bad(f"{place}.headers", f"'{header}' is a credential-like header; the engine record holds credentials, a description cannot")
            elif header.lower() in _TRANSPORT_HEADERS:
                self.bad(f"{place}.headers", f"'{header}' is set by the transport")
            elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header):
                self.bad(f"{place}.headers", f"'{header}' is not a valid header name")
            headers[header] = value
        query: list[QuerySpec] = []
        for index, item in enumerate(spec.get("query") or []):
            segments = self._src(item["from"], mapping.REQUEST_ROOTS, f"{place}.query[{index}].from")
            if segments is not None:
                query.append(QuerySpec(item["name"], segments, item.get("default", pointer.MISSING)))
        body = self._body(spec.get("body"), method, f"{place}.body")
        return RequestSpec(method, spec["path"], names, tuple(query), headers, body)

    def _src(self, text: str, roots: frozenset[str], place: str) -> pointer.Segments | None:
        try:
            segments = pointer.parse_pointer(text)
        except ValueError as exc:
            self.bad(place, f"{text!r} is not a valid pointer: {exc}")
            return None
        if segments[0] not in roots:
            self.bad(place, "a source pointer must start with " + ", ".join("/" + r for r in sorted(roots)) + f"; found {text!r}")
            return None
        return segments

    def _body(self, spec: dict | None, method: str, place: str) -> BodySpec:
        if spec is None:
            return BodySpec()
        encoding = spec.get("encoding", "json")
        if encoding != "none" and method in ("GET", "DELETE"):
            self.bad(place, f"a {method} request cannot carry a body")
        if encoding == "none":
            for key in ("map", "parts", "base"):
                if key in spec:
                    self.bad(place, f"'{key}' needs a body encoding")
            return BodySpec()
        if encoding == "json":
            if "parts" in spec:
                self.bad(place, "'parts' is for multipart bodies")
            rules = spec.get("map", [])
            base = spec.get("base", "empty" if "map" in spec else "passthrough")
            program = mapping.compile_program(rules, base=base, roots=mapping.REQUEST_ROOTS, place=f"{place}.map",
                                              problems=self.problems, budgets=self.budgets)
            return BodySpec("json", program)
        # multipart
        for key in ("map", "base"):
            if key in spec:
                self.bad(place, f"a multipart body is built from 'parts', not '{key}'")
        parts: list[PartSpec] = []
        if not spec.get("parts"):
            self.bad(place, "a multipart body needs 'parts'")
        for index, part in enumerate(spec.get("parts") or []):
            pplace = f"{place}.parts[{index}]"
            roots = frozenset({"files"}) if part["kind"] == "file" else frozenset({"body", "params"})
            segments = self._src(part["from"], roots, f"{pplace}.from")
            if part["kind"] == "file" and segments is not None and len(segments) != 2:
                self.bad(f"{pplace}.from", "a file part reads one named file, /files/<name>")
                segments = None
            if segments is not None:
                parts.append(PartSpec(part["name"], part["kind"], segments, bool(part.get("required", False)),
                                      part.get("default", pointer.MISSING)))
        return BodySpec("multipart", None, tuple(parts))

    # --- responses -----------------------------------------------------------------------------

    def _terminal(self, spec: dict | None, framing: str, place: str) -> Terminal | None:
        if spec is None:
            return None
        has_pair = "path" in spec or "equals" in spec
        kinds = [k for k in ("sentinel", "event") if k in spec] + (["path+equals"] if has_pair else [])
        if len(kinds) != 1:
            self.bad(place, "a terminal rule is exactly one of: sentinel, event, or path with equals")
            return None
        if has_pair and not ("path" in spec and "equals" in spec):
            self.bad(place, "a path terminal needs both 'path' and 'equals'")
            return None
        if ("sentinel" in spec or "event" in spec) and framing != "sse":
            self.bad(place, "sentinel and event terminals are for SSE")
            return None
        segments = None
        if has_pair:
            try:
                segments = pointer.parse_pointer(spec["path"])
            except ValueError as exc:
                self.bad(place, f"'path' is not a valid pointer: {exc}")
                return None
        return Terminal(spec.get("sentinel"), spec.get("event"), segments, spec.get("equals"))

    def _select(self, select: dict, place: str) -> dict[str, pointer.Segments]:
        out: dict[str, pointer.Segments] = {}
        for name, text in select.items():
            try:
                out[name] = pointer.parse_pointer(text, wildcard=True)
            except ValueError as exc:
                self.bad(f"{place}.{name}", f"{text!r} is not a valid pointer: {exc}")
        return out

    def _response(self, spec: dict, place: str, request: RequestSpec) -> ResponseSpec:
        framing = spec.get("framing", "json")
        select = self._select(spec.get("select") or {}, f"{place}.select")
        if framing in ("binary", "none") and any(k in spec for k in ("select", "map", "terminal", "stream")):
            self.bad(place, f"a {framing} response has no select, map, terminal or stream")
        program = None
        if "map" in spec:
            if framing not in ("json", "sse", "ndjson"):
                self.bad(place, "'map' needs a JSON, sse or ndjson response")
            program = mapping.compile_program(spec["map"], base=spec.get("base", "empty"), roots=mapping.RESPONSE_ROOTS,
                                              place=f"{place}.map", problems=self.problems, budgets=self.budgets)
        terminal = self._terminal(spec.get("terminal"), framing, f"{place}.terminal")
        if terminal is not None and framing not in ("sse", "ndjson"):
            self.bad(place, "'terminal' is for streamed responses")
        stream = None
        if "stream" in spec:
            if framing != "json":
                self.bad(place, "'stream' picks a streamed framing for a response whose normal framing is json")
            sspec = spec["stream"]
            splace = f"{place}.stream"
            when = mapping.compile_predicate(sspec["when"], mapping.REQUEST_ROOTS, f"{splace}.when", self.problems)
            sprogram = None
            if "map" in sspec:
                sprogram = mapping.compile_program(sspec["map"], base=sspec.get("base", "empty"), roots=mapping.RESPONSE_ROOTS,
                                                   place=f"{splace}.map", problems=self.problems, budgets=self.budgets)
            sterminal = self._terminal(sspec.get("terminal"), sspec["framing"], f"{splace}.terminal")
            stream = StreamSpec(when, sspec["framing"], sterminal, sprogram)
        return ResponseSpec(framing, select, program, terminal, stream)

    # --- jobs ----------------------------------------------------------------------------------

    def _job(self, spec: dict, place: str) -> JobSpec | None:
        submit_req = self._request(spec["submit"]["request"], f"{place}.submit.request")
        try:
            id_segments = pointer.parse_pointer(spec["submit"]["id"])
        except ValueError as exc:
            self.bad(f"{place}.submit.id", f"not a valid pointer: {exc}")
            id_segments = ("id",)
        poll = spec["poll"]
        pplace = f"{place}.poll"
        poll_req = self._request(poll["request"], f"{pplace}.request")
        if "id" not in poll_req.placeholders:
            self.bad(f"{pplace}.request.path", "a poll path must use {id}")
        states = dict(poll["status"]["states"])
        for upstream, state in states.items():
            if state not in JOB_STATES:
                self.bad(f"{pplace}.status.states.{upstream}", f"'{state}' is not a job state (use one of {', '.join(JOB_STATES)})")
        if "succeeded" not in states.values():
            self.bad(f"{pplace}.status.states", "must map at least one upstream value to succeeded")
        status = StatusSpec(self._ptr(poll["status"]["path"], f"{pplace}.status.path"), states)
        error = self._ptr(poll["error"], f"{pplace}.error") if "error" in poll else None
        assets = self._ptr(poll["assets"], f"{pplace}.assets", wildcard=True) if "assets" in poll else None
        fetch = cancel = None
        if "fetch" in spec:
            fetch = self._request(spec["fetch"]["request"], f"{place}.fetch.request")
            if "{asset}" in spec["fetch"]["request"]["path"] and assets is None:
                self.bad(f"{place}.fetch.request.path", "{asset} needs 'assets' under poll")
            if "id" not in fetch.placeholders and "asset" not in fetch.placeholders:
                self.bad(f"{place}.fetch.request.path", "a fetch path must use {id} or {asset}")
        if "cancel" in spec:
            cancel = self._request(spec["cancel"]["request"], f"{place}.cancel.request")
            if "id" not in cancel.placeholders:
                self.bad(f"{place}.cancel.request.path", "a cancel path must use {id}")
        return JobSpec(SubmitSpec(submit_req, id_segments), PollSpec(poll_req, status, error, assets, float(poll["interval_s"]), float(poll["max_wait_s"])), fetch, cancel)

    def _ptr(self, text: str, place: str, *, wildcard: bool = False) -> pointer.Segments:
        try:
            return pointer.parse_pointer(text, wildcard=wildcard)
        except ValueError as exc:
            self.bad(place, f"{text!r} is not a valid pointer: {exc}")
            return ("x",)


# ---------------------------------------------------------------------------
# The registry directory
# ---------------------------------------------------------------------------

_MEMO: dict[Path, tuple[tuple, dict[str, ProtocolDescription], dict[str, str]]] = {}
_LOG = logging.getLogger(__name__)


def user_directory() -> Path:
    """Operator descriptions live with state, rather than in the container image."""
    from gateway import admin_store
    return Path(os.environ.get("AZIZAM_PROTOCOLS_DIR") or admin_store.DB_PATH.parent / "protocols")


def registry_problems() -> list[dict[str, str]]:
    return [{"file": name, "reason": reason} for _, _, issues in tuple(_MEMO.values()) for name, reason in issues.items()]


def _load_directory(directory: Path) -> tuple[dict[str, ProtocolDescription], dict[str, str]]:
    files = sorted(directory.glob("*.yml"))
    try:
        signature = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files) + (("schema", SCHEMA_PATH.stat().st_mtime_ns),)
    except OSError:
        # A concurrently replaced file is retried on the next request.
        signature = ()
    cached = _MEMO.get(directory)
    if signature and cached is not None and cached[0] == signature:
        return dict(cached[1]), dict(cached[2])
    loaded: dict[str, ProtocolDescription] = {}
    problems: dict[str, str] = {}
    for path in files:
        try:
            parsed = parse_description_text(path.read_text(encoding="utf-8"), source=path.name)
            if parsed.id != path.stem:
                raise DescriptionError(path.name, ["The file name must equal the description id."])
            loaded[parsed.id] = parsed
        except (DescriptionError, OSError, UnicodeError) as error:
            _LOG.warning("Cannot load protocol description %s: %s", path, error)
            problems[path.name] = "The description is invalid or unreadable. Validate the file before enabling it."
    _MEMO[directory] = (signature, loaded, problems)
    return dict(loaded), dict(problems)


def load_registry(directory: Path | None = None) -> dict[str, ProtocolDescription]:
    """Load valid files independently. User files replace seeds, including invalid overrides."""
    if directory is not None:
        return _load_directory(Path(directory))[0]
    directories = [REGISTRY_DIR, user_directory()]
    # Retain diagnostics only for the active registry roots.
    for previous in list(_MEMO):
        if previous not in directories:
            del _MEMO[previous]
    loaded: dict[str, ProtocolDescription] = {}
    for root in directories:
        records, issues = _load_directory(root)
        for file in issues:
            loaded.pop(Path(file).stem, None)
        loaded.update(records)
    return loaded


def get_description(protocol_id: str) -> ProtocolDescription | None:
    return load_registry().get(protocol_id)


def main() -> int:
    """Validate a description without booting the gateway or accessing its secrets."""
    import argparse
    parser = argparse.ArgumentParser(description="Validate an http-v1 protocol description")
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        doc = parse_description_text(args.file.read_text(encoding="utf-8"), source=args.file.name)
        if doc.id != args.file.stem:
            raise DescriptionError(args.file.name, ["The file name must equal the description id."])
    except (DescriptionError, OSError, UnicodeError) as error:
        print(error)
        return 1
    print(f"{args.file.name}: valid ({len(doc.operations)} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
