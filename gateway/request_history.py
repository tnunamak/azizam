"""Request history: what an app sent, and what came back, kept for a short time.

docs/design/REQUEST_RETENTION.md sections 3.5 (parts and the secret floor), 3.6 (storage) and 4 (reader scope).
This module holds the kind's rules and its store. It decides nothing about policy: ``retention`` does that, and
the capture layer (``request_history_capture``) calls it once per request.

The secret floor is code, not data: a credential-named field is removed at any depth, a URL keeps its scheme, host and
port, and its path except a segment that reads as a credential or a token (its query and fragment become markers), a
large media string is not copied, and a body the gateway does not parse is recorded as omitted. Only the request
headers and reply headers named below are kept.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import string
import unicodedata
import uuid
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from python_multipart.multipart import MultipartParser, parse_options_header

from gateway import retention

log = logging.getLogger("gateway.request_history")
KIND = retention.REQUEST_HISTORY
#: What a credential-named field, or a removed URL part, reads as in a kept record.
REMOVED = "[removed]"
#: A base64 string longer than this is media, not text (section 3.5, rule 5).
MEDIA_STRING_LIMIT = 64 * 1024
#: How deeply JSON may nest before the secret floor omits it: a whole body, or a string that holds JSON (such as a
#: tool call's ``arguments``). No request the routes accept nests this deep, and the limit keeps the cleaner's recursion
#: far from Python's own limit.
JSON_DEPTH_LIMIT = 64
#: How many values one record's nested JSON may visit, in all (R6-2). The decoded text is bounded by the record's byte
#: limit, so the two together bound the work a body of many small JSON strings can cause.
JSON_NODE_LIMIT = 200_000
#: Request headers kept by name. Nothing that carries a credential is on this list (rule 2).
REQUEST_HEADERS = frozenset({"content-type", "accept", "x-azizam-prompts"})
#: Reply headers kept by name.
RESPONSE_HEADERS = frozenset({"content-type"})

_CREDENTIAL_SEGMENTS = frozenset({"secret", "password", "passwd", "credential", "authorization", "apikey"})
_CREDENTIAL_PAIRS = frozenset({("api", "key"), ("access", "token"), ("refresh", "token"), ("id", "token"), ("auth", "token")})
# One tokenizer for every name the secret floor reads. Brackets split too: a nested form value arrives as
# ``headers[Authorization]`` (the OpenAI SDK flattens it), and ``extensions[ns][name]`` is the gateway's own form.
_SEGMENT_BOUNDARY = re.compile(r"[-_\s.\[\]]+|(?<=[a-z0-9])(?=[A-Z])")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
#: What a URL scheme may hold after its first letter: letters, digits, ``+``, ``.`` and ``-`` (RFC 3986).
_SCHEME_CHARACTERS = frozenset(string.ascii_letters + string.digits + "+.-")
_SCHEME_LETTER = re.compile(r"[A-Za-z]")
#: A run of text with no whitespace in it. A URL is read within one run (R9-1).
_RUN = re.compile(r"\S+")
#: What may come right before a scheme and start a new token (R9-1). The start of a run also starts one.
_TOKEN_STARTERS = frozenset(",;()[]{}<>\"'`")
#: A path segment is a token (R8-1) when it is 20 or more characters from this set, with a letter and a digit.
_TOKEN_CHARACTERS = re.compile(r"[A-Za-z0-9_-]{20,}")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
#: Prose punctuation that may follow a path segment (R9-2). It is set aside before the token check, and kept.
_PROSE_PUNCTUATION = "\"'`)]}>.,;!?"
_NON_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")  # every non-token character separates; a dot does not, so a file name stays whole
_LONGEST_SCHEME = 20  # schemes in use are far shorter; a 20-character one is a value read as a scheme (R10-1)
#: What a URL reads as when it holds an ``@``, cannot be split into its parts, or holds a ``://`` that starts no token
#: (R9-1). The whole URL or token is replaced, so no part of it is kept.
URL_REMOVED = "[url removed]"

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
  summary_json TEXT NOT NULL,
  parts_kept TEXT NOT NULL DEFAULT '[]',
  byte_size INTEGER NOT NULL DEFAULT 0
)
"""
#: Columns added after the first table shape. A database made before them gains them on connect, and their values are
#: derived from the stored parts, so a list reads metadata alone (REQUEST_RETENTION.md 3.6).
_ADDED_COLUMNS = (("parts_kept", "TEXT NOT NULL DEFAULT '[]'"), ("byte_size", "INTEGER NOT NULL DEFAULT 0"))
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS kept_requests_person_time ON kept_requests(person_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS kept_requests_deadline ON kept_requests(kept_until)",
    "CREATE INDEX IF NOT EXISTS kept_requests_request ON kept_requests(request_id)",
)


# ------------------------------------------------------------------ the secret floor


def segments(name: str) -> list[str]:
    """A name split into lower-case words, in snake, kebab, dotted or camel case."""
    return [part.lower() for part in _SEGMENT_BOUNDARY.split(str(name)) if part]


def _forms(word: str) -> set[str]:
    """What a word may mean: as written without its digits, and without one trailing ``s``. ``password1`` and
    ``secrets`` read as ``password`` and ``secret``; ``access`` reads as itself, as it is a credential word."""
    end = len(word)
    while end and word[end - 1].isdecimal():  # one backwards scan: re ``\d+$`` retried from every digit, quadratic
        end -= 1
    stem = word[:end]
    forms = {stem}
    if len(stem) > 1 and stem.endswith("s"):
        forms.add(stem[:-1])
    return forms


#: Default_Ignorable_Code_Point characters that are not format characters (category Cf), as code point ranges (R6-4).
#: They show as nothing, as a format character does. U+2065 and U+FFF0-U+FFF8 are unassigned, and the property covers them.
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2065, 0x2065), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF),
)


def _is_ignorable(char: str) -> bool:
    """A character that shows as nothing: a format character (category Cf), or a default-ignorable code point."""
    code = ord(char)
    return unicodedata.category(char) == "Cf" or any(low <= code <= high for low, high in _DEFAULT_IGNORABLE_RANGES)


def is_credential_name(name: str) -> bool:
    """Rule 8: a credential word anywhere in the name, or a pair such as api_key. ``max_tokens`` is kept.
    Characters that show as nothing (see ``_is_ignorable``, R6-4) are dropped first, then the name is NFKC-normalised,
    so a fullwidth ``ｐａｓｓｗｏｒｄ`` reads as ``password``."""
    visible = "".join(char for char in str(name) if not _is_ignorable(char))
    words = [_forms(part) for part in segments(unicodedata.normalize("NFKC", visible))]
    if any(_CREDENTIAL_SEGMENTS & forms for forms in words):
        return True
    return any((first, second) in _CREDENTIAL_PAIRS
               for left, right in zip(words, words[1:]) for first in left for second in right)


def allowed_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Rule 2: request headers by name, from the fixed list only."""
    return {name.lower(): value for name, value in headers.items() if name.lower() in REQUEST_HEADERS}


def allowed_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items() if name.lower() in RESPONSE_HEADERS}


def _url_runs(text: str) -> list[tuple[int, int, bool]]:
    """Each URL in ``text``, as (start, end, ambiguous). The text is read in runs of non-whitespace. A URL starts at a
    scheme (a letter, scheme characters, then ``://``) that begins a token: the start of a run, or a character after
    ``_TOKEN_STARTERS`` (R9-1). It runs to the end of its token, so ``https://a.test/x,https://b.test/y`` is two URLs.
    A ``://`` that begins no token cannot be split into a scheme and a host, so its token is ambiguous and is replaced
    whole. Each character is read once, so a long run of letters costs time in proportion to its length."""
    runs: list[tuple[int, int, bool]] = []
    if "://" in text:
        for run in _RUN.finditer(text):
            runs.extend(_run_urls(text, run.start(), run.end()))
    return runs


def _run_urls(text: str, lo: int, hi: int) -> list[tuple[int, int, bool]]:
    """The URLs and ambiguous tokens in ``text[lo:hi]``, a run of non-whitespace (see ``_url_runs``)."""
    separator = text.find("://", lo, hi)
    if separator == -1:
        return []
    starts: list[int] = []  # each scheme that begins a token, so starts a URL
    strays: list[int] = []  # each ``://`` that begins no token
    while separator != -1:
        begin = separator
        while begin > lo and text[begin - 1] in _SCHEME_CHARACTERS:
            begin -= 1
        letter = _SCHEME_LETTER.search(text, begin, separator)
        if letter is not None and (letter.start() == lo or text[letter.start() - 1] in _TOKEN_STARTERS):
            starts.append(letter.start())
        else:
            strays.append(separator)
        separator = text.find("://", separator + 3, hi)
    edges = sorted({lo, hi, *starts})  # the tokens are the spans between neighbouring edges
    ambiguous = {bisect_right(edges, stray) - 1 for stray in strays}
    begins = set(starts)
    runs: list[tuple[int, int, bool]] = []
    for index, (left, right) in enumerate(zip(edges, edges[1:])):
        if index in ambiguous:
            runs.append((left, right, True))
        elif left in begins:
            runs.append((left, right, False))
    return runs


def _is_token_segment(segment: str) -> bool:
    """Rule 4 for one path segment (R8-1). A segment with a colon is a token, as an ``<id>:<secret>`` pair is. So is a
    segment of 20 or more characters from ``[A-Za-z0-9_-]`` that holds a letter and a digit, as a webhook token or a
    UUID is. Only the shape is read, so a short slug and a numeric id are kept. ``_scrub_segment`` passes it the
    decoded segment without its trailing prose punctuation (R9-2, R9-3)."""
    if ":" in segment:
        return True
    return (_TOKEN_CHARACTERS.fullmatch(segment) is not None
            and _LETTER.search(segment) is not None and _DIGIT.search(segment) is not None)


def _scrub_segment(segment: str) -> str:
    """Rule 4 for one path segment. A credential name becomes ``[removed]``, as before. Otherwise the segment is read
    after percent-decoding (R9-3), with its trailing prose punctuation set aside (R9-2). A token core becomes
    ``[removed]`` and the punctuation after it is kept. Any other segment is kept as it was written."""
    if is_credential_name(segment):
        return REMOVED
    decoded = unquote(segment)
    core = decoded.rstrip(_PROSE_PUNCTUATION)
    # Each piece between non-token characters is read on its own, so a decoded ``/``, a bracket or a ``*`` beside a
    # token does not hide it (R10-2, R11-1). A dot does not separate, so a dotted file name is not a token.
    if ":" in core or any(_is_token_segment(piece) for piece in _NON_TOKEN.split(core)):
        return REMOVED + decoded[len(core):]
    return segment


def _scrub_url(url: str) -> tuple[str, bool]:
    """Rule 4 for one URL (R7-1). An ``@`` or ``%40`` anywhere replaces the whole URL, as does one that cannot be split
    into a scheme and a non-empty ASCII host. Otherwise the scheme, host and port are kept, each path segment is read by
    ``_scrub_segment`` (R8-1, R9-2, R9-3), and a query or fragment becomes a marker, ``?[removed]`` or ``#[removed]``.
    The second value says whether any part was removed."""
    if "@" in url or "%40" in url:
        return URL_REMOVED, True
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a port that is not a number from 0 to 65535
    except ValueError:
        return URL_REMOVED, True
    if not parts.scheme or not parts.hostname or not parts.hostname.isascii():
        return URL_REMOVED, True
    if len(parts.scheme) >= _LONGEST_SCHEME:  # a value written before ``://`` reads as a scheme; no real one is this long
        return URL_REMOVED, True
    segments = parts.path.split("/")
    kept = [_scrub_segment(segment) for segment in segments]
    text = f"{parts.scheme}://{parts.netloc}{'/'.join(kept)}"
    if parts.query:
        text += f"?{REMOVED}"
    if parts.fragment:
        text += f"#{REMOVED}"
    return text, kept != segments or bool(parts.query or parts.fragment)


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
    return _scrub_urls(value, found)


def _scrub_urls(text: str, found: set[str]) -> str:
    """Rule 4 for every URL in a value or in a longer string, such as a pasted signed link. Each URL is cleaned by
    ``_scrub_url``, an ambiguous token is replaced whole (R9-1), and the text between them is kept as it is."""
    pieces: list[str] = []
    end = 0
    for start, stop, ambiguous in _url_runs(text):
        cleaned, changed = (URL_REMOVED, True) if ambiguous else _scrub_url(text[start:stop])
        if changed:
            found.add("url_parts_removed")
        pieces.append(text[end:start] + cleaned)
        end = stop
    pieces.append(text[end:])
    return "".join(pieces)


class _TooDeep(Exception):
    """JSON nests past JSON_DEPTH_LIMIT. Raised deep in the cleaner, and caught where the JSON holding it is omitted."""


class _OverBudget(Exception):
    """A record's JSON budget is spent. Raised where the next unit of work would start, and caught where the string that
    held the JSON is omitted."""


class WorkBudget:
    """The work one record may spend on JSON held inside strings (R6-2): the text it decodes, in bytes, and the values
    it visits. One budget is shared by every body of a record, so the total stays bounded however many wrappers the
    record holds. A string spends it when it parses as a JSON object, array or literal that is cleaned further. A
    literal around plain text is cleaned as text and costs nothing here. Before a string is parsed it must fit in what
    is left (R7-3), so one too long to fit is omitted unparsed, even when it would have been plain text."""

    def __init__(self, *, max_bytes: int, max_nodes: int = JSON_NODE_LIMIT) -> None:
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes
        self.decoded_bytes = 0
        self.nodes = 0

    def fits(self, text: str) -> bool:
        """Whether ``text`` can be decoded within the byte limit. Nothing is spent."""
        return self.decoded_bytes + len(text.encode("utf-8", "surrogatepass")) <= self.max_bytes

    def decode(self, text: str) -> None:
        size = len(text.encode("utf-8", "surrogatepass"))
        if self.decoded_bytes + size > self.max_bytes:
            raise _OverBudget
        self.decoded_bytes += size

    def visit(self) -> None:
        if self.nodes >= self.max_nodes:
            raise _OverBudget
        self.nodes += 1


def _redact_string(text: str, found: set[str], depth: int, budget: WorkBudget, *, tool_arguments: bool = False) -> Any:
    """A string whose first mark, after whitespace, is ``{``, ``[`` or ``"`` is read as JSON, and the value it holds is
    cleaned and stored compact. A JSON string literal whose content is JSON, or another literal, is cleaned as that
    content and stored as a literal again, so a value wrapped in several levels of encoding is cleaned at every level
    (R6-2). A literal around plain text has its decoded text cleaned as text, and is stored as a literal again (R7-2).
    A string that does not parse is cleaned as text, since prose may start with a bracket or a quote. A tool call's
    ``arguments`` is always JSON, so one that does not parse is omitted. A string nested past JSON_DEPTH_LIMIT is
    omitted, and so is one the record's budget cannot hold, which is checked before the string is parsed (R7-3)."""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[", '"')):
        return _redact_text(text, found)
    if not budget.fits(text):
        found.add("json_budget")
        return {"omitted": "json_budget"}
    try:
        parsed = json.loads(text)
    except RecursionError:
        found.add("json_depth")
        return {"omitted": "json_depth"}
    except ValueError:
        if tool_arguments and not stripped.startswith('"'):
            found.add("unreadable_json")
            return {"omitted": "unreadable_json"}
        return _redact_text(text, found)
    if isinstance(parsed, str):
        if not parsed.lstrip().startswith(("{", "[", '"')):
            cleaned = _redact_text(parsed, found)
            return json.dumps(cleaned, ensure_ascii=False) if isinstance(cleaned, str) else cleaned
        if depth > JSON_DEPTH_LIMIT:
            found.add("json_depth")
            return {"omitted": "json_depth"}
        budget.decode(text)  # fits was checked before the parse, so this only spends the length
        inner = _redact_string(parsed, found, depth + 1, budget, tool_arguments=tool_arguments)
        return json.dumps(inner, ensure_ascii=False) if isinstance(inner, str) else inner
    omissions: set[str] = set()
    try:
        budget.decode(text)
        cleaned = _redact(parsed, omissions, depth, budget, nested=True)
    except _OverBudget:
        found.add("json_budget")
        return {"omitted": "json_budget"}
    except (_TooDeep, RecursionError):
        found.add("json_depth")
        return {"omitted": "json_depth"}
    found.update(omissions)
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def _redact(value: Any, found: set[str], depth: int, budget: WorkBudget, *, nested: bool = False) -> Any:
    """The secret floor, applied to a parsed value. ``depth`` is the nesting level of ``value``: a container past
    JSON_DEPTH_LIMIT raises ``_TooDeep``, which omits the JSON that holds it. ``nested`` is set for JSON read out of a
    string, whose values count against the record's budget (R6-2). The body's own values do not."""
    if nested:
        budget.visit()
    if isinstance(value, Mapping):
        if depth > JSON_DEPTH_LIMIT:
            raise _TooDeep
        result: dict[str, Any] = {}
        for key, item in value.items():
            if is_credential_name(str(key)):
                result[key] = REMOVED
                found.add("credential_field")
            elif key == "arguments" and isinstance(item, str):
                result[key] = _redact_string(item, found, depth + 1, budget, tool_arguments=True)
            else:
                result[key] = _redact(item, found, depth + 1, budget, nested=nested)
        return result
    if isinstance(value, list):
        if depth > JSON_DEPTH_LIMIT:
            raise _TooDeep
        return [_redact(item, found, depth + 1, budget, nested=nested) for item in value]
    if isinstance(value, str):
        return _redact_string(value, found, depth, budget)
    return value


def redact_json(value: Any, *, budget: WorkBudget | None = None) -> tuple[Any, list[str]]:
    """Apply the secret floor to a parsed body. Returns the kept value and the omissions it made. A body nested past
    JSON_DEPTH_LIMIT is omitted whole, so the record keeps a marker in its place. A caller that cleans several bodies
    of one record passes one ``budget`` to all of them. Without one, the body gets a budget of a record's size."""
    found: set[str] = set()
    if budget is None:
        budget = WorkBudget(max_bytes=int(retention.bundled().data["kinds"][KIND]["max_record_bytes"]))
    try:
        return _redact(value, found, 1, budget), sorted(found)
    except (_TooDeep, RecursionError):
        return {"omitted": "json_depth"}, ["json_depth"]


# ------------------------------------------------------------------ one body, as it streams


def _boundary(content_type: str | None) -> bytes | None:
    _, params = parse_options_header(content_type or "")
    boundary = params.get(b"boundary") if params else None
    return boundary if boundary else None


def _json_object(raw: bytes) -> dict[str, Any] | None:
    """The body as a JSON object, or None when it is not one."""
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


class BodyCapture:
    """One request or reply body, read as it streams past.

    A JSON object and a multipart form are kept as content. Anything else is counted and recorded as an unsupported
    container (rule 7). Every body is read for its control, as the routes read it: a route parses a JSON body whatever
    its media type says, and reads a form-urlencoded body's fields. A body over ``limit`` bytes is counted and recorded
    as omitted, not truncated, so a kept record is never a partial one.
    """

    def __init__(self, content_type: str | None, *, limit: int, budget: WorkBudget | None = None) -> None:
        self.media_type = (content_type or "").split(";", 1)[0].strip().lower()
        self.limit = limit
        #: The record's JSON budget, shared with the record's other bodies. A body on its own gets one of its own.
        self._budget = budget if budget is not None else WorkBudget(max_bytes=limit)
        self.total = 0
        self._buffer = bytearray()
        self._overflow = False
        self._failed = False
        self._fields: dict[str, bytes] = {}
        self._field_bytes = 0
        self._files: list[dict[str, Any]] = []
        self._part: dict[str, Any] = {}
        self._parser: MultipartParser | None = None
        self._mode = "other"
        self._memo: tuple[Any, list[str]] | None = None
        #: The body's own control values, before redaction: the parsed JSON object (in any media type), or a multipart
        #: form's text fields.
        self.control_source: dict[str, Any] | None = None
        #: A form-urlencoded body's fields, read as the routes read them. None for any other media type.
        self.form_source: dict[str, str] | None = None
        #: Multipart field names sent as files. A control field sent as a file is not a readable control.
        self.control_files: set[str] = set()
        self._readable = True
        if self.media_type == "application/json":
            self._mode = "json"
        elif self.media_type == "multipart/form-data" and _boundary(content_type):
            self._mode = "multipart"
            try:
                self._parser = MultipartParser(_boundary(content_type), callbacks=self._callbacks())
            except Exception:
                # A form the parser cannot start is a form whose controls cannot be read.
                self._failed = True

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self._mode != "multipart" and not self._overflow:
            # Every body that is not a form is buffered up to the limit, so its control can be read whole.
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

    def control_readable(self) -> bool:
        """Whether the body's control could be read in full. A body over the record limit could hold a control past
        what was read, and so could a JSON or multipart body that was cut short or did not parse. A body within the
        limit is read for its control whatever its media type, so it is readable."""
        self.result()
        return self._readable

    def _read(self) -> tuple[Any, list[str]]:
        if self._mode == "multipart":
            return self._read_multipart()
        if self._overflow:
            # Over the limit, so the control is unknown. A route reads a JSON control in any media type.
            self._readable = False
            if self._mode == "json":
                return {"omitted": "record_size", "bytes": self.total}, ["record_size"]
        elif self._mode == "json":
            try:
                parsed = json.loads(bytes(self._buffer))
            except (ValueError, RecursionError):
                parsed = None
                self._readable = self.total == 0
            if isinstance(parsed, dict):
                self.control_source = parsed
                return redact_json(parsed, budget=self._budget)
        else:
            # Not kept as content (rule 7), but read for its control the way the route reads it.
            raw = bytes(self._buffer)
            self.control_source = _json_object(raw)
            if self.media_type == "application/x-www-form-urlencoded":
                self.form_source = dict(parse_qsl(raw.decode("latin-1"), keep_blank_values=True))
        return {"omitted": "unsupported_container", "media_type": self.media_type or "unknown",
                "bytes": self.total}, ["unsupported_container"]

    def _read_multipart(self) -> tuple[Any, list[str]]:
        if self._overflow:
            self._readable = False
            return {"omitted": "record_size", "bytes": self.total}, ["record_size"]
        if not self._failed and self._parser is not None:
            try:
                self._parser.finalize()
                texts = {name: value.decode("utf-8", "replace") for name, value in self._fields.items()}
                self.control_source = texts
                fields, found = redact_json(texts, budget=self._budget)
                return {"fields": fields, "files": self._files}, found
            except Exception:
                pass
        self._readable = False
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
                self.control_files.add(part["name"])
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


#: How much of what a request asked a list row shows. The preview is cut when the record is kept, from the kept body, so a
#: list never loads a body (REQUEST_RETENTION.md section 17, slice 5b1 r2).
PREVIEW_CHARACTERS = 120
_REMOVED_VALUE = re.compile(r"\s*(?:" + re.escape(REMOVED) + "|" + re.escape(URL_REMOVED) + ")")


def _words_of(content: Any) -> str | None:
    """One message's words: its string content, or the text parts of a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [part.get("text") if isinstance(part, Mapping) else part for part in content]
        return " ".join(text for text in texts if isinstance(text, str)) or None
    return None


def _asked(body: Mapping[str, Any]) -> str | None:
    """What the request asked, as the kept body holds it: the first user message, the input, or the prompt."""
    messages = body.get("messages")
    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, Mapping) and message.get("role") == "user" and (words := _words_of(message.get("content"))):
            return words
    model_input = body.get("input")
    if isinstance(model_input, str):
        return model_input
    for item in model_input if isinstance(model_input, list) else ():
        if isinstance(item, str):
            return item  # an embeddings input: the first of its strings
        if isinstance(item, Mapping) and item.get("role") == "user" and (words := _words_of(item.get("content"))):
            return words
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    fields = body.get("fields")  # a multipart form keeps its text fields here
    if isinstance(fields, Mapping) and isinstance(fields.get("prompt"), str):
        return fields["prompt"]
    return None


def preview_of(accepted_body: Any) -> str | None:
    """One line of what the request asked, with no removed value in it; None when it asked nothing in words."""
    asked = _asked(accepted_body) if isinstance(accepted_body, Mapping) else None
    if asked is None:
        return None
    words = " ".join(_REMOVED_VALUE.sub(" ", asked).split())
    if len(words) > PREVIEW_CHARACTERS:
        words = words[: PREVIEW_CHARACTERS - 1].rstrip() + "…"
    return words or None


def summarize(route: str, accepted_body: Any, status: int | None, latency_ms: int) -> dict[str, Any]:
    """The structural summary a kept record keeps after its receipt is gone: route, model, status, latency and the
    preview of what was asked."""
    model = _model_of(accepted_body)
    return {"route": route, "model": model[:200] if isinstance(model, str) else None,
            "status": status, "latency_ms": int(latency_ms), "preview": preview_of(accepted_body)}


def _model_of(accepted_body: Any) -> Any:
    """The model a request named: its JSON ``model``, or the ``model`` field of a multipart form (transcription, edits)."""
    if not isinstance(accepted_body, Mapping):
        return None
    if "model" in accepted_body:
        return accepted_body["model"]
    fields = accepted_body.get("fields")
    return fields.get("model") if isinstance(fields, Mapping) else None


# ------------------------------------------------------------------ the store


def new_record_id() -> str:
    return f"kr_{uuid.uuid4().hex}"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The media-jobs database, where kept requests live beside Library prompts (section 3.6)."""
    from gateway import prompt_memory

    conn = prompt_memory._connect(path)
    conn.execute(_TABLE)
    _add_missing_columns(conn)
    for statement in _INDEXES:
        conn.execute(statement)
    return conn


def _derived(parts_json: str, omissions_json: str, summary_json: str) -> tuple[str, int]:
    """The two columns a list reads without the bodies: the names of the kept parts, and the stored bytes of a record."""
    try:
        names = sorted(json.loads(parts_json))
    except (ValueError, TypeError):
        names = []
    return json.dumps(names), sum(len(text.encode("utf-8")) for text in (parts_json, omissions_json, summary_json))


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    present = {row[1] for row in conn.execute("PRAGMA table_info(kept_requests)")}
    missing = [(name, ddl) for name, ddl in _ADDED_COLUMNS if name not in present]
    if not missing:
        return
    for name, ddl in missing:
        try:
            conn.execute(f"ALTER TABLE kept_requests ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            # Another connection added it between the check and this statement: the column is there.
            if "duplicate column name" not in str(exc):
                raise
    for record_id, parts_json, omissions_json, summary_json in conn.execute(
            "SELECT record_id, parts_json, omissions_json, summary_json FROM kept_requests").fetchall():
        parts_kept, byte_size = _derived(parts_json, omissions_json, summary_json)
        conn.execute("UPDATE kept_requests SET parts_kept=?, byte_size=? WHERE record_id=?",
                     (parts_kept, byte_size, record_id))
    conn.commit()


@contextmanager
def write_transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """One write transaction on the media-jobs database, held until the block ends (REQUEST_RETENTION.md 3.6).

    Open it inside the household's write lock (admin.db), never before: every writer takes admin.db first, so the two
    databases are never waited on in opposite orders. A block that raises rolls back.
    """
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_record(conn: sqlite3.Connection, record: Mapping[str, Any]) -> None:
    """Add one kept request inside the caller's transaction (see ``write_transaction``)."""
    parts_json = json.dumps(record["parts"], ensure_ascii=False, sort_keys=True)
    omissions_json = json.dumps(record["omissions"], sort_keys=True)
    summary_json = json.dumps(record["summary"], ensure_ascii=False, sort_keys=True)
    parts_kept, byte_size = _derived(parts_json, omissions_json, summary_json)
    conn.execute(
        "INSERT INTO kept_requests(record_id, request_id, kind, person_id, app_id, origin_kind, origin_id, "
        "engine_id, debug_window_id, created_at, kept_until, parts_json, omissions_json, policy_version, "
        "linked_asset_ids, fact_revisions, summary_json, parts_kept, byte_size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?)",
        (record["record_id"], record.get("request_id"), KIND, record["person_id"], record.get("app_id"),
         record["origin_kind"], record.get("origin_id"), record["created_at"], record.get("kept_until"),
         parts_json, omissions_json, record["policy_version"], summary_json, parts_kept, byte_size),
    )


def insert(record: Mapping[str, Any], *, path: Path | None = None) -> None:
    with write_transaction(path) as conn:
        insert_record(conn, record)


# ------------------------------------------------------------------ reads, deletion and erasure (section 12)


def _rows(sql: str, params: tuple, *, path: Path | None) -> list[dict[str, Any]]:
    conn = connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


#: The columns a list reads. The bodies (parts_json, omissions_json) are not among them, so a list never loads one.
_METADATA = ("record_id, request_id, kind, person_id, app_id, origin_kind, origin_id, created_at, kept_until, "
             "parts_kept, summary_json")


def page_for_person(person_id: str, *, after: int, now: int, before: tuple[int, str] | None, limit: int,
                    path: Path | None = None) -> list[dict[str, Any]]:
    """One page of the person's kept requests that are not expired and not erased, newest first, cut in SQL.

    ``before`` is the (created_at, record_id) of the last row of the previous page. Ownership is applied to the
    page by the caller, so a page can hold rows the viewer may not read; the caller reads further pages until it
    has enough rows it may read.
    """
    sql = (f"SELECT {_METADATA} FROM kept_requests WHERE person_id=? AND kind=? AND created_at>? "
           "AND (kept_until IS NULL OR kept_until>?)")
    params: list[Any] = [person_id, KIND, after, now]
    if before is not None:
        sql += " AND (created_at<? OR (created_at=? AND record_id<?))"
        params += [before[0], before[0], before[1]]
    sql += " ORDER BY created_at DESC, record_id DESC LIMIT ?"
    params.append(int(limit))
    return _rows(sql, tuple(params), path=path)


def get_row(record_id: str, *, path: Path | None = None) -> dict[str, Any] | None:
    rows = _rows("SELECT * FROM kept_requests WHERE record_id=? AND kind=?", (record_id, KIND), path=path)
    return rows[0] if rows else None


def delete_row(record_id: str, *, path: Path | None = None) -> bool:
    conn = connect(path)
    try:
        cursor = conn.execute("DELETE FROM kept_requests WHERE record_id=? AND kind=?", (record_id, KIND))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _admin_connection(admin_path: Path) -> sqlite3.Connection:
    from gateway import retention

    return retention.connect(admin_path, timeout=retention.RETENTION_LOCK_TIMEOUT_SECONDS)


def _delete_rows(person_id: str, cutoff: int, *, path: Path | None) -> int:
    """Delete a person's kept requests created at or before ``cutoff`` (``*``: everyone's). Returns how many."""
    conn = connect(path)
    try:
        if person_id == "*":
            cursor = conn.execute("DELETE FROM kept_requests WHERE kind=? AND created_at<=?", (KIND, cutoff))
        else:
            cursor = conn.execute("DELETE FROM kept_requests WHERE kind=? AND person_id=? AND created_at<=?",
                                  (KIND, person_id, cutoff))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def _clear_intent(person_id: str, cutoff: int, *, admin_path: Path) -> None:
    """Clear a person's erasure intent once its rows are gone."""
    from gateway import retention

    admin = _admin_connection(admin_path)
    try:
        admin.execute("BEGIN IMMEDIATE")
        if _has_table(admin, retention.ERASURE_INTENT_TABLE):
            admin.execute(
                f"DELETE FROM {retention.ERASURE_INTENT_TABLE} WHERE person_id=? AND grp=? AND cutoff_at<=?",
                (person_id, retention.GROUP_REQUEST_HISTORY, cutoff))
        admin.commit()
    finally:
        admin.close()


def settle_erasure(person_id: str, cutoff: int, *, admin_path: Path, path: Path | None = None) -> int:
    """Delete a person's kept requests created at or before ``cutoff`` (``*``: everyone's), then clear the intent.

    The rows go first. If that fails, the intent stays, and maintenance settles it again (section 12).
    """
    deleted = _delete_rows(person_id, cutoff, path=path)
    _clear_intent(person_id, cutoff, admin_path=admin_path)
    return deleted


def delete_all_for_person(person_id: str, *, admin_path: Path, path: Path | None = None, now: int) -> dict[str, Any]:
    """Delete everything a person kept, and fence the group: a request that started before now cannot write after it.

    The fence is written first. If it cannot be written (the store is busy), nothing is erased and the error is raised.
    Once it is written the records are hidden, so a row delete that then cannot run is left to maintenance: the result
    says ``pending``.
    """
    from gateway import admin_store, retention

    admin = _admin_connection(admin_path)
    try:
        admin.execute("BEGIN IMMEDIATE")
        admin_store._record_prompt_erasure(admin, person_id, now, groups=(retention.GROUP_REQUEST_HISTORY,))
        admin.commit()
    finally:
        admin.close()
    try:
        deleted = _delete_rows(person_id, now, path=path)
    except sqlite3.OperationalError:
        log.warning("request history erasure settles at maintenance: the rows could not be deleted now", exc_info=False)
        return {"deleted": 0, "pending": True}
    try:
        _clear_intent(person_id, now, admin_path=admin_path)
    except sqlite3.OperationalError:
        # The rows are gone, so the count is the real one. The intent stays, and maintenance clears it (section 12).
        log.warning("request history erasure intent settles at maintenance: the store is busy", exc_info=False)
    return {"deleted": deleted, "pending": False}


def set_member_choice(person_id: str, *, keep: bool, retention_id: str, parts: list[str], admin_path: Path,
                      path: Path | None = None, now: int) -> dict[str, Any]:
    """Save a person's request history choice. Turning it off erases the group's records, as memory-off does (section 12)."""
    from gateway import admin_store, retention

    record = admin_store.put_prompt_memory_preference(
        person_id, remember=keep, retention=retention_id, updated_by=person_id, now=now,
        group=retention.GROUP_REQUEST_HISTORY, parts=parts, path=admin_path)
    if not keep:
        try:
            settle_erasure(person_id, int(record["updated_at"]), admin_path=admin_path, path=path)
        except sqlite3.OperationalError:
            # The choice and its erasure intent are saved, and the intent holds until maintenance deletes the rows.
            log.warning("request history erasure settles at maintenance: the rows could not be deleted now",
                        exc_info=False)
    else:
        # The period now in force shortens the person's existing deadlines; it never lengthens them (section 12).
        cap = retention.bundled().household_defaults(retention.REQUEST_HISTORY)["retention_cap"]
        seconds = retention.seconds_map()[retention.effective_period(retention_id, cap, retention.REQUEST_HISTORY)]
        if seconds is not None:
            tighten(seconds, person_id=person_id, admin_path=admin_path, path=path)
    return record


def household_policy(admin_path: Path | None) -> dict[str, Any]:
    """The owner's values for request history: the kill switch, which is off for everyone whatever a member chose, and
    the group's default and cap. ``decide`` reads them at capture, and ``GET /me/api/retention`` reports the switch."""
    from gateway import admin_store

    row = admin_store.get_operator_setting(retention.SETTING_KEY, path=admin_path or admin_store.DB_PATH)
    return retention.household_view(row.get("value") if row else None, retention.GROUP_REQUEST_HISTORY)


#: Members who dismissed the notice that request history is available (admin store; see ``admin_store._SCHEMA``).
NOTICE_TABLE = "request_history_notice_seen"


def notice_due(person_id: str, *, admin_path: Path) -> bool:
    """Whether to tell a member that request history is available (REQUEST_RETENTION.md section 12, item 3).

    It is due for a member whose history switch is still off and whom nothing has changed: the row the schema 17
    upgrade wrote, with no last writer (``retention_migration.migrate_group_keys``). Turning the switch on or off
    answers it, and so does dismissing it.
    """
    conn = _admin_connection(admin_path)
    try:
        row = conn.execute(f"SELECT remember, updated_by FROM {retention.MEMBER_TABLE} WHERE user_id=? AND grp=?",
                           (person_id, retention.GROUP_REQUEST_HISTORY)).fetchone()
        if row is None or row[0] or row[1]:
            return False
        if not _has_table(conn, NOTICE_TABLE):
            return True
        return conn.execute(f"SELECT 1 FROM {NOTICE_TABLE} WHERE person_id=?", (person_id,)).fetchone() is None
    finally:
        conn.close()


def mark_notice_seen(person_id: str, *, admin_path: Path, now: int) -> None:
    """Dismiss the notice. The member's choice does not change."""
    conn = _admin_connection(admin_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"INSERT OR IGNORE INTO {NOTICE_TABLE}(person_id, seen_at) VALUES (?, ?)", (person_id, int(now)))
        conn.commit()
    finally:
        conn.close()


def tighten(period_seconds: int, *, person_id: str, admin_path: Path | None = None, path: Path | None = None) -> int:
    """A shorter period shortens existing deadlines of the person's records. A longer one never extends them (12)."""
    conn = connect(path)
    try:
        cursor = conn.execute(
            "UPDATE kept_requests SET kept_until=created_at+? WHERE person_id=? AND kind=? "
            "AND (kept_until IS NULL OR kept_until>created_at+?)",
            (int(period_seconds), person_id, KIND, int(period_seconds)))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def maintain(*, admin_path: Path, path: Path | None = None, now: int) -> dict[str, int]:
    """Expiry and the request-history erasure intents. Each pass repeats until it succeeds."""
    from gateway import retention

    conn = connect(path)
    try:
        cursor = conn.execute("DELETE FROM kept_requests WHERE kept_until IS NOT NULL AND kept_until<=?", (now,))
        conn.commit()
        expired = cursor.rowcount
        # Over a person's byte cap, the oldest of that person's records go first (FIFO). The running total is taken from
        # the stored sizes, newest first, so only persons over the cap are touched.
        cap = byte_cap()
        cursor = conn.execute(
            "DELETE FROM kept_requests WHERE kind=? AND record_id IN ("
            " SELECT record_id FROM ("
            "  SELECT record_id, SUM(byte_size) OVER (PARTITION BY person_id ORDER BY created_at DESC, record_id DESC"
            "   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running"
            "  FROM kept_requests WHERE kind=? AND person_id IN ("
            "   SELECT person_id FROM kept_requests WHERE kind=? GROUP BY person_id HAVING SUM(byte_size) > ?))"
            " WHERE running > ?)",
            (KIND, KIND, KIND, cap, cap))
        conn.commit()
        over_cap = cursor.rowcount
    finally:
        conn.close()
    admin = _admin_connection(admin_path)
    try:
        intents = []
        if _has_table(admin, retention.ERASURE_INTENT_TABLE):
            intents = admin.execute(
                f"SELECT person_id, cutoff_at FROM {retention.ERASURE_INTENT_TABLE} WHERE grp=?",
                (retention.GROUP_REQUEST_HISTORY,)).fetchall()
    finally:
        admin.close()
    settled = 0
    for person_id, cutoff in intents:
        settle_erasure(person_id, int(cutoff), admin_path=admin_path, path=path)
        settled += 1
    return {"expired": expired, "over_cap": over_cap, "settled_intents": settled}


def byte_cap() -> int:
    """The stored bytes one person's kept requests may hold: the bundled value (REQUEST_RETENTION.md 12)."""
    return int(retention.bundled().data["kinds"][KIND]["max_bytes_per_person"])
