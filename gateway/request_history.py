"""Request history: what an app sent, and what came back, kept for a short time.

docs/design/REQUEST_RETENTION.md sections 3.5 (parts and the secret floor), 3.6 (storage) and 4 (reader scope).
This module holds the kind's rules and its store. It decides nothing about policy: ``retention`` does that, and
the capture layer (``request_history_capture``) calls it once per request.

The secret floor is code, not data: a credential-named field is removed at any depth, a URL keeps only its scheme,
host, path and query names, a large media string is not copied, and a body the gateway does not parse is recorded
as omitted. Only the request headers and reply headers named below are kept.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from python_multipart.multipart import MultipartParser, parse_options_header

from gateway import retention

KIND = retention.REQUEST_HISTORY
#: What a credential-named field, or a removed URL part, reads as in a kept record.
REMOVED = "[removed]"
#: A base64 string longer than this is media, not text (section 3.5, rule 5).
MEDIA_STRING_LIMIT = 64 * 1024
#: Request headers kept by name. Nothing that carries a credential is on this list (rule 2).
REQUEST_HEADERS = frozenset({"content-type", "accept", "x-azizam-prompts"})
#: Reply headers kept by name.
RESPONSE_HEADERS = frozenset({"content-type"})

_CREDENTIAL_SEGMENTS = frozenset({"secret", "password", "passwd", "credential", "credentials", "authorization", "apikey"})
_CREDENTIAL_PAIRS = frozenset({("api", "key"), ("access", "token"), ("refresh", "token"), ("id", "token"), ("auth", "token")})
_SEGMENT_BOUNDARY = re.compile(r"[-_\s.]+|(?<=[a-z0-9])(?=[A-Z])")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")

_TABLE = """
CREATE TABLE IF NOT EXISTS kept_requests (
  record_id TEXT PRIMARY KEY,
  request_id TEXT,
  kind TEXT NOT NULL,
  person_id TEXT NOT NULL,
  app_id TEXT,
  origin_kind TEXT NOT NULL,
  origin_id TEXT,
  engine_id TEXT,
  debug_window_id TEXT,
  created_at INTEGER NOT NULL,
  kept_until INTEGER,
  parts_json TEXT NOT NULL,
  omissions_json TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  linked_asset_ids TEXT NOT NULL DEFAULT '[]',
  fact_revisions TEXT NOT NULL DEFAULT '[]',
  summary_json TEXT NOT NULL
)
"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS kept_requests_person_time ON kept_requests(person_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS kept_requests_deadline ON kept_requests(kept_until)",
    "CREATE INDEX IF NOT EXISTS kept_requests_request ON kept_requests(request_id)",
)


# ------------------------------------------------------------------ the secret floor


def segments(name: str) -> list[str]:
    """A name split into lower-case words, in snake, kebab, dotted or camel case."""
    return [part.lower() for part in _SEGMENT_BOUNDARY.split(str(name)) if part]


def is_credential_name(name: str) -> bool:
    """Rule 8: a credential segment anywhere in the name, or a pair such as api_key. ``max_tokens`` is kept."""
    parts = segments(name)
    if _CREDENTIAL_SEGMENTS.intersection(parts):
        return True
    return any(pair in _CREDENTIAL_PAIRS for pair in zip(parts, parts[1:]))


def allowed_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Rule 2: request headers by name, from the fixed list only."""
    return {name.lower(): value for name, value in headers.items() if name.lower() in REQUEST_HEADERS}


def allowed_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items() if name.lower() in RESPONSE_HEADERS}


def _scrub_url(value: str) -> tuple[str, bool] | None:
    """Rule 4: scheme, host, port and path stay; user info and query values become [removed]; fragments go."""
    if not value.startswith(("http://", "https://")) or any(char.isspace() for char in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return REMOVED, True
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    userinfo = f"{REMOVED}@" if parts.username is not None or parts.password is not None else ""
    query = "&".join(f"{name}={REMOVED}" for name, _ in parse_qsl(parts.query, keep_blank_values=True))
    changed = bool(userinfo or parts.query or parts.fragment)
    return urlunsplit((parts.scheme, userinfo + netloc, parts.path, query, "")), changed


def _redact_text(value: str, found: set[str]) -> Any:
    if value.startswith("data:"):
        found.add("media")
        header, _, payload = value[5:].partition(",")
        media_type = header.split(";", 1)[0] or "text/plain"
        if ";base64" in header:
            size = len(payload.rstrip("=")) * 3 // 4
        else:
            size = len(payload.encode("utf-8"))
        return {"omitted": "media", "media_type": media_type, "bytes": size}
    if len(value) > MEDIA_STRING_LIMIT and _BASE64.fullmatch(value):
        found.add("media")
        return {"omitted": "media", "media_type": "unknown", "bytes": len(value) * 3 // 4}
    scrubbed = _scrub_url(value)
    if scrubbed is None:
        return value
    text, changed = scrubbed
    if changed:
        found.add("url_parts_removed")
    return text


def _redact(value: Any, found: set[str]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if is_credential_name(str(key)):
                result[key] = REMOVED
                found.add("credential_field")
            else:
                result[key] = _redact(item, found)
        return result
    if isinstance(value, list):
        return [_redact(item, found) for item in value]
    if isinstance(value, str):
        return _redact_text(value, found)
    return value


def redact_json(value: Any) -> tuple[Any, list[str]]:
    """Apply the secret floor to a parsed body. Returns the kept value and the omissions it made."""
    found: set[str] = set()
    return _redact(value, found), sorted(found)


# ------------------------------------------------------------------ one body, as it streams


def _boundary(content_type: str | None) -> bytes | None:
    _, params = parse_options_header(content_type or "")
    boundary = params.get(b"boundary") if params else None
    return boundary if boundary else None


class BodyCapture:
    """One request or reply body, read as it streams past. Only a JSON object and a multipart form are parsed.

    Anything else is counted and recorded as an unsupported container (rule 7). A JSON body over ``limit`` bytes
    is counted and recorded as omitted, not truncated, so a kept record is never a partial one.
    """

    def __init__(self, content_type: str | None, *, limit: int) -> None:
        self.media_type = (content_type or "").split(";", 1)[0].strip().lower()
        self.limit = limit
        self.total = 0
        self._buffer = bytearray()
        self._overflow = False
        self._failed = False
        self._fields: dict[str, bytes] = {}
        self._field_bytes = 0
        self._files: list[dict[str, Any]] = []
        self._part: dict[str, Any] | None = None
        self._parser: MultipartParser | None = None
        self._mode = "other"
        self._memo: tuple[Any, list[str]] | None = None
        #: The body's own control values, before redaction: the parsed JSON object, or a multipart form's text fields.
        self.control_source: dict[str, Any] | None = None
        if self.media_type == "application/json":
            self._mode = "json"
        elif self.media_type == "multipart/form-data" and _boundary(content_type):
            self._mode = "multipart"
            self._parser = MultipartParser(_boundary(content_type), callbacks=self._callbacks())

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self._mode == "json" and not self._overflow:
            if len(self._buffer) + len(chunk) > self.limit:
                self._overflow = True
                self._buffer = bytearray()
            else:
                self._buffer.extend(chunk)
        elif self._mode == "multipart" and not self._failed and self._parser is not None:
            try:
                self._parser.write(chunk)
            except Exception:
                self._failed = True

    def result(self) -> tuple[Any, list[str]]:
        if self._memo is None:
            self._memo = self._read()
        return self._memo

    def _read(self) -> tuple[Any, list[str]]:
        if self._mode == "json" and not self._overflow:
            try:
                parsed = json.loads(bytes(self._buffer))
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, dict):
                self.control_source = parsed
                return redact_json(parsed)
        elif self._mode == "json":
            return {"omitted": "record_size", "bytes": self.total}, ["record_size"]
        elif self._mode == "multipart" and self._overflow:
            return {"omitted": "record_size", "bytes": self.total}, ["record_size"]
        elif self._mode == "multipart" and not self._failed and self._parser is not None:
            try:
                self._parser.finalize()
                texts = {name: value.decode("utf-8", "replace") for name, value in self._fields.items()}
                self.control_source = texts
                fields, found = redact_json(texts)
                return {"fields": fields, "files": self._files}, found
            except Exception:
                pass
        return {"omitted": "unsupported_container", "media_type": self.media_type or "unknown",
                "bytes": self.total}, ["unsupported_container"]

    # -- multipart: one part at a time, so a file's bytes are counted, never kept

    def _callbacks(self) -> dict[str, Any]:
        def begin() -> None:
            self._part = {"header_name": bytearray(), "header_value": bytearray(), "name": None,
                          "filename": None, "media_type": None, "bytes": 0, "value": bytearray()}

        def header_field(data: bytes, start: int, end: int) -> None:
            self._part["header_name"].extend(data[start:end])

        def header_value(data: bytes, start: int, end: int) -> None:
            self._part["header_value"].extend(data[start:end])

        def header_end() -> None:
            name = bytes(self._part["header_name"]).decode("latin-1").strip().lower()
            value = bytes(self._part["header_value"]).decode("latin-1")
            self._part["header_name"] = bytearray()
            self._part["header_value"] = bytearray()
            if name == "content-disposition":
                _, params = parse_options_header(value)
                self._part["name"] = params.get(b"name", b"").decode("utf-8", "replace") if params else None
                filename = params.get(b"filename") if params else None
                self._part["filename"] = filename.decode("utf-8", "replace") if filename is not None else None
            elif name == "content-type":
                self._part["media_type"] = value.split(";", 1)[0].strip().lower()

        def part_data(data: bytes, start: int, end: int) -> None:
            piece = data[start:end]
            self._part["bytes"] += len(piece)
            if self._part["filename"] is None:
                self._field_bytes += len(piece)
                if self._field_bytes > self.limit:
                    self._overflow = True
                elif not self._overflow:
                    self._part["value"].extend(piece)

        def part_end() -> None:
            part = self._part
            if part["name"] is None:
                return
            if part["filename"] is not None:
                self._files.append({"field": part["name"],
                                    "media_type": part["media_type"] or "application/octet-stream",
                                    "bytes": part["bytes"]})
            else:
                self._fields[part["name"]] = bytes(part["value"])

        return {"on_part_begin": begin, "on_header_field": header_field, "on_header_value": header_value,
                "on_header_end": header_end, "on_part_data": part_data, "on_part_end": part_end}


def capture_request(headers: Mapping[str, str], body: BodyCapture) -> tuple[dict[str, Any], list[str]]:
    """The ``accepted`` part: the request's allowed headers and its body as the gateway received it."""
    kept, omissions = body.result()
    return {"headers": allowed_request_headers(headers), "body": kept}, omissions


def capture_reply(status: int, headers: Mapping[str, str], body: BodyCapture) -> tuple[dict[str, Any], list[str]]:
    """The ``received`` part: the status, the allowed reply headers and the reply's body."""
    kept, omissions = body.result()
    return {"status": int(status), "headers": allowed_response_headers(headers), "body": kept}, omissions


def summarize(route: str, accepted_body: Any, status: int | None, latency_ms: int) -> dict[str, Any]:
    """The structural summary a kept record keeps after its receipt is gone: route, model, status and latency."""
    model = accepted_body.get("model") if isinstance(accepted_body, Mapping) else None
    return {"route": route, "model": model[:200] if isinstance(model, str) else None,
            "status": status, "latency_ms": int(latency_ms)}


# ------------------------------------------------------------------ the store


def new_record_id() -> str:
    return f"kr_{uuid.uuid4().hex}"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The media-jobs database, where kept requests live beside Library prompts (section 3.6)."""
    from gateway import prompt_memory

    conn = prompt_memory._connect(path)
    conn.execute(_TABLE)
    for statement in _INDEXES:
        conn.execute(statement)
    return conn


def insert(record: Mapping[str, Any], *, path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO kept_requests(record_id, request_id, kind, person_id, app_id, origin_kind, origin_id, "
            "engine_id, debug_window_id, created_at, kept_until, parts_json, omissions_json, policy_version, "
            "linked_asset_ids, fact_revisions, summary_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, '[]', '[]', ?)",
            (record["record_id"], record.get("request_id"), KIND, record["person_id"], record.get("app_id"),
             record["origin_kind"], record.get("origin_id"), record["created_at"], record.get("kept_until"),
             json.dumps(record["parts"], ensure_ascii=False, sort_keys=True),
             json.dumps(record["omissions"], sort_keys=True), record["policy_version"],
             json.dumps(record["summary"], ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()
