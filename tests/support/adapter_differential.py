"""Behaviour capture for the media and speech engine clients (W6b differential harness).

A *scenario* drives the real gateway app over HTTP against a scenario-driven fake
engine and records one JSON *trace*: every request the gateway sent to an engine,
the response the caller got, and what the gateway wrote (route receipts, media job
rows, stored assets). The trace is compared with a committed golden. Run the same
scenarios in two checkouts and the traces must be equal, byte for byte, after the
documented normalisation of volatile values (see ``Normalizer``).

This module has no import-time dependency on the gateway, so the extraction CLI
(``python -m tests.support.adapter_differential``) runs under any Python 3.

Pieces:

* ``Normalizer``      replaces only the volatile values (ids, times, signatures, temp paths)
* ``describe_body``   turns wire bytes into a stable, readable, exact description
* ``Recorder``        taps the httpx transport (engine requests) and ``asyncio.sleep``
* ``dump_sqlite``     dumps every table of a store, for receipts and media jobs
* ``describe_assets`` lists the stored asset files (suffix, size, sha256)
* ``compare_golden``  golden compare with a readable diff; update and dump modes
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

TRACE_SCHEMA = 1
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "adapter_differential"
BOUNDARY_TOKEN = "<boundary>"
OUT_ENV = "AZIZAM_DIFFERENTIAL_OUT"
UPDATE_ENV = "AZIZAM_UPDATE_GOLDEN"
JUNIT_PROPERTY = "adapter_trace"

# --------------------------------------------------------------------------- normaliser

_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z])")
_HEX32 = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])")
_REQ_ID = re.compile(r"(?<![0-9A-Za-z])req_[0-9a-f]{32}(?![0-9A-Za-z])")
_JOB_ID = re.compile(r"(?<![0-9A-Za-z])(mj|vidjob|imgjob|audjob)_[0-9a-f]{32}(?![0-9A-Za-z])")
_ISO_TIME = re.compile(
    r"(?<![0-9])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?(?![0-9])"
)
_SIG = re.compile(r"(?<=[?&])sig=[0-9a-f]+")
_EXPIRES = re.compile(r"(?<=[?&])expires=\d+")
_NEUTRAL_HTTP_DATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT"
)

# A numeric value under one of these keys is a clock reading of this run. The rule is by
# name: ``*_at`` and ``*_at_ms`` are timestamps by the gateway's own naming, and the few
# other names were found by diffing raw dumps of two runs and reading the code that
# writes them. Everything else is compared exactly (see the normaliser tests).
VOLATILE_TIME_SUFFIXES = ("_at", "_at_ms", "_at_s", "_deadline")
VOLATILE_TIME_KEYS = frozenset({"created", "expires", "mtime", "ctime", "timestamp", "first_seen"})
# Keys that are a duration measured on the real clock (milliseconds or seconds).
VOLATILE_DURATION_KEYS = frozenset({
    "duration_ms", "latency_ms", "elapsed_ms", "total_ms", "ttfb_ms", "queue_ms", "upstream_ms",
    "generation_ms", "first_byte_ms",
})


# ``request_hash`` is a digest of the request for a seeded or idempotency-keyed image job; for every
# other job it is a random token (``uuid4().hex * 2`` or ``secrets.token_hex(32)``).
DETERMINISTIC_IDEMPOTENCY = frozenset({"seeded_request", "idempotency_key"})
_REQUEST_HASH = re.compile(r'("request_hash"\s*:\s*)"[0-9a-f]{64}"')
_INPUT_HASH = re.compile(r'("input_hash"\s*:\s*)"[0-9a-f]{64}"')
_DETERMINISTIC_JOB = re.compile(r'"idempotency"\s*:\s*"(?:seeded_request|idempotency_key)"')

# A digest over fields that include a route-generated random request id cannot be normalised
# by value. ``input_hash`` of a media job covers its metadata and input, which carry such ids.
# (``request_hash`` is random only for an unseeded request: ``idempotency == "request"``.)
VOLATILE_DIGEST_KEYS = frozenset({"input_hash"})


def _time_key(key: str | None) -> bool:
    return bool(key) and (key in VOLATILE_TIME_KEYS or key.endswith(VOLATILE_TIME_SUFFIXES))


def _key_pattern() -> str:
    names = sorted(VOLATILE_TIME_KEYS | VOLATILE_DURATION_KEYS)
    suffixes = "|".join(re.escape(s) for s in VOLATILE_TIME_SUFFIXES)
    return rf'"(?:{"|".join(names)}|[A-Za-z0-9_]*(?:{suffixes}))"'


_NUMBER_AFTER_KEY = re.compile(rf'({_key_pattern()}\s*:\s*)(-?\d+(?:\.\d+)?)(?![\d.])')


class Normalizer:
    """Replace values that change from run to run with stable tokens.

    Ids are replaced by order of first appearance within one trace, per kind, so
    ``<req1>`` always means the same request id inside a trace and two different ids
    stay two different tokens. A changed id *count* or *order* still changes the trace.

    What it replaces (and nothing else):

    * ``req_<32 hex>`` request ids                      -> ``<req:N>``
    * ``mj_/vidjob_/imgjob_/audjob_<32 hex>`` job ids   -> ``<job:N>``
    * dashed uuids and bare 32-hex ids (client ids)     -> ``<uuid:N>`` / ``<hex32:N>``
    * ISO 8601 date-times and HTTP dates                -> ``<iso-time>`` / ``<http-date>``
    * ``sig=`` and ``expires=`` of signed URLs          -> ``sig=<sig>`` / ``expires=<expires>``
    * given temporary directory roots                   -> ``<tmp>``
    * ``input_hash`` of a job, and ``request_hash`` unless the job is seeded or idempotency-keyed
      (then it is a digest of the request)                -> ``<digest>`` / ``<random-hash>``
      (digests over, or equal to, route-generated random ids)
    * numbers under time keys (``*_at``, ``*_at_ms``, ``created``, ``expires`` ...) and duration
      keys (``latency_ms`` ...), in parsed JSON and in JSON text -> ``"<time>"`` / ``"<duration>"``.
      Only numbers: a null stays null, so "was set" versus "was not set" still shows.
    """

    def __init__(self, tmp_roots: Iterable[str | Path] = ()) -> None:
        # Keys whose list of strings the gateway builds by iterating a set: the order is a
        # hash-seed artefact that differs between processes, so such a list is compared sorted.
        # Empty by default; a scenario opts a key in, naming the code that builds it.
        self.unordered_keys: frozenset[str] = frozenset()
        self._seen: dict[str, dict[str, int]] = {}
        roots = sorted({str(root).rstrip("/") for root in tmp_roots if str(root).strip("/")}, key=len, reverse=True)
        self._roots = roots

    def token(self, kind: str, value: str) -> str:
        table = self._seen.setdefault(kind, {})
        if value not in table:
            table[value] = len(table) + 1
        return f"<{kind}:{table[value]}>"

    def text(self, value: str) -> str:
        for root in self._roots:
            value = value.replace(root, "<tmp>")
        value = _SIG.sub("sig=<sig>", value)
        value = _EXPIRES.sub("expires=<expires>", value)
        value = _REQ_ID.sub(lambda m: self.token("req", m.group(0)), value)
        value = _JOB_ID.sub(lambda m: self.token("job", m.group(0)), value)
        value = _UUID.sub(lambda m: self.token("uuid", m.group(0)), value)
        value = _HEX32.sub(lambda m: self.token("hex32", m.group(0)), value)
        value = _ISO_TIME.sub("<iso-time>", value)
        value = _NEUTRAL_HTTP_DATE.sub("<http-date>", value)
        value = _NUMBER_AFTER_KEY.sub(self._number_after_key, value)
        value = _INPUT_HASH.sub(r'\1"<digest>"', value)
        if not _DETERMINISTIC_JOB.search(value):
            value = _REQUEST_HASH.sub(r'\1"<random-hash>"', value)
        return value

    @staticmethod
    def _number_after_key(match: "re.Match[str]") -> str:
        key = match.group(1).split(":", 1)[0].strip().strip('"')
        return match.group(1) + ('"<duration>"' if key in VOLATILE_DURATION_KEYS else '"<time>"')

    def value(self, value: Any, key: str | None = None) -> Any:
        """Normalise a parsed JSON value; ``key`` is the key it sits under."""
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            if _time_key(key):
                return "<time>"
            if key in VOLATILE_DURATION_KEYS:
                return "<duration>"
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            random_request = value.get("idempotency") not in DETERMINISTIC_IDEMPOTENCY
            out: dict[str, Any] = {}
            for k, v in value.items():
                if k in VOLATILE_DIGEST_KEYS and isinstance(v, str):
                    out[self.text(str(k))] = "<digest>"
                elif k == "request_hash" and random_request and isinstance(v, str):
                    out[self.text(str(k))] = "<random-hash>"
                else:
                    out[self.text(str(k))] = self.value(v, str(k))
            return out
        if isinstance(value, (list, tuple)):
            items = [self.value(item, key) for item in value]
            if key in self.unordered_keys and all(isinstance(item, str) for item in items):
                items.sort()
            return items
        return value


# --------------------------------------------------------------------------- bodies

_JSON_TYPES = ("application/json", "+json")
_TEXT_PREFIXES = ("text/",)
_TEXT_TYPES = ("application/x-www-form-urlencoded", "application/xml", "application/javascript")


def _content_type_parts(content_type: str | None) -> tuple[str, dict[str, str]]:
    if not content_type:
        return "", {}
    main, *params = [part.strip() for part in content_type.split(";")]
    out: dict[str, str] = {}
    for param in params:
        if "=" in param:
            name, _, val = param.partition("=")
            out[name.strip().lower()] = val.strip().strip('"')
    return main.lower(), out


def binary_summary(content: bytes) -> dict[str, Any]:
    return {"kind": "binary", "len": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "magic": content[:8].hex()}


def sort_json_keys(text: str) -> str:
    """The same JSON text with object keys sorted, keeping its separator style (compact or spaced)."""
    spaced = '": ' in text
    return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False,
                      separators=(", ", ": ") if spaced else (",", ":"))


def describe_body(content: bytes | None, content_type: str | None, norm: Normalizer, *,
                  unordered_json: bool = False) -> dict[str, Any]:
    """An exact, readable description of body bytes.

    ``unordered_json`` sorts the keys of a JSON body first, for a document the gateway builds by
    iterating a set (its key order follows the process hash seed). Use it only for such a body.

    Text and JSON keep their exact characters (so key order and spacing show), with
    volatile values normalised. Binary is its length, sha256 and first bytes.
    Multipart bodies are split into parts; the random boundary becomes a fixed token.
    """
    if not content:
        return {"kind": "empty"}
    main, params = _content_type_parts(content_type)
    if main.startswith("multipart/") and params.get("boundary"):
        return _describe_multipart(content, params["boundary"], norm)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return binary_summary(content)
    textual = (main.startswith(_TEXT_PREFIXES) or main in _TEXT_TYPES or main.endswith(_JSON_TYPES) or main == "")
    if textual or _looks_json(text):
        kind = "json" if (main.endswith(_JSON_TYPES) or _looks_json(text)) else "text"
        if unordered_json and kind == "json":
            text = sort_json_keys(text)
        return {"kind": kind, "text": norm.text(text)}
    return binary_summary(content)


def describe_sse_audio(content: bytes, norm: Normalizer) -> dict[str, Any]:
    """A speech SSE stream with each run of ``speech.audio.delta`` events joined into one audio blob.

    The gateway cuts a transcoded stream wherever an ffmpeg pipe read lands, which depends on
    timing, so the number and sizes of the delta events are not behaviour. The audio they carry,
    and every other event in order, are compared exactly.
    """
    events: list[Any] = []
    pending = bytearray()
    for block in content.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        if block.startswith("data: "):
            try:
                payload = json.loads(block[6:])
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "speech.audio.delta" and isinstance(payload.get("audio"), str):
                pending += base64.b64decode(payload["audio"])
                continue
        if pending:
            events.append({"type": "speech.audio.delta (joined)", "audio": binary_summary(bytes(pending))})
            pending = bytearray()
        events.append(norm.text(block))
    if pending:
        events.append({"type": "speech.audio.delta (joined)", "audio": binary_summary(bytes(pending))})
    return {"kind": "sse-audio", "events": events}


def _looks_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def _describe_multipart(content: bytes, boundary: str, norm: Normalizer) -> dict[str, Any]:
    token = BOUNDARY_TOKEN.encode()
    marker = b"--" + boundary.encode()
    pieces = content.split(marker)
    parts: list[dict[str, Any]] = []
    preamble = norm.text(pieces[0].decode("latin-1")) if pieces[0] else ""
    epilogue = ""
    for index, piece in enumerate(pieces[1:], start=1):
        if piece.startswith(b"--"):  # closing delimiter; what follows is the epilogue
            epilogue = norm.text(piece[2:].decode("latin-1"))
            break
        # piece is CRLF headers CRLFCRLF body CRLF
        body_part = piece[2:] if piece.startswith(b"\r\n") else piece
        head, sep, rest = body_part.partition(b"\r\n\r\n")
        if not sep:
            parts.append({"malformed": binary_summary(piece)})
            continue
        trailing = rest.endswith(b"\r\n")
        payload = rest[:-2] if trailing else rest
        header_text = head.decode("latin-1")
        part_type = ""
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-type:"):
                part_type = line.split(":", 1)[1].strip()
        parts.append({
            "headers": norm.text(header_text.replace(boundary, BOUNDARY_TOKEN)),
            "body": describe_body(payload, part_type or None, norm),
            "crlf_after_body": trailing,
        })
    _ = token
    return {"kind": "multipart", "boundary": BOUNDARY_TOKEN, "preamble": preamble,
            "parts": parts, "epilogue": epilogue}


_DROPPED_REQUEST_HEADERS = frozenset({"user-agent"})
_DROPPED_RESPONSE_HEADERS = frozenset({"date", "server"})
_TIME_RESPONSE_HEADERS = frozenset({"last-modified", "expires"})
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"})


def describe_request_headers(headers: Iterable[tuple[str, str]], norm: Normalizer) -> dict[str, str]:
    """Lower-cased, sorted. Credentials become ``<present>``; ``user-agent`` (a library version) is dropped."""
    out: dict[str, str] = {}
    for name, value in headers:
        lowered = name.lower()
        if lowered in _DROPPED_REQUEST_HEADERS:
            continue
        if lowered in _SECRET_HEADERS:
            out[lowered] = "<present>"
            continue
        if lowered == "content-type":
            main, params = _content_type_parts(value)
            if main.startswith("multipart/") and params.get("boundary"):
                value = value.replace(params["boundary"], BOUNDARY_TOKEN)
        out[lowered] = norm.text(value)
    return dict(sorted(out.items()))


def settle_content_length(headers: dict[str, str], raw: bytes, body: Mapping[str, Any]) -> None:
    """Make ``content-length`` independent of the length of normalised values.

    Normalisation can change a body's length (a temp directory path becomes ``<tmp>``, an epoch
    becomes ``"<time>"``), and a temp path's length depends on the run (process ids have different
    digit counts). When it did, the header is replaced by the length of the normalised text, so a
    change in any other byte of the body still changes it. Otherwise the real header stays.
    """
    text = body.get("text")
    if "content-length" in headers and isinstance(text, str) and len(text.encode("utf-8")) != len(raw):
        headers["content-length"] = f"<{len(text.encode('utf-8'))} after normalisation>"


def describe_response_headers(headers: Iterable[tuple[str, str]], norm: Normalizer) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers:
        lowered = name.lower()
        if lowered in _DROPPED_RESPONSE_HEADERS:
            continue
        if lowered in _TIME_RESPONSE_HEADERS:
            out[lowered] = "<http-date>"
            continue
        out[lowered] = norm.text(value)
    return dict(sorted(out.items()))


def sorted_query(query: str | bytes, norm: "Normalizer | None" = None) -> list[list[str]]:
    """Query pairs sorted by key then value. With ``norm`` the raw query is normalised first,
    so ``sig=`` and ``expires=`` of a signed URL and ids in values are replaced."""
    from urllib.parse import parse_qsl

    text = query.decode("latin-1") if isinstance(query, bytes) else query
    if norm is not None:
        text = norm.text("?" + text)[1:]
    return sorted([k, v] for k, v in parse_qsl(text, keep_blank_values=True))


# --------------------------------------------------------------------------- stores

_JSON_COLUMN = re.compile(r"^\s*[\[{]")


def dump_sqlite(path: Path, norm: Normalizer, *, skip_tables: Iterable[str] = ()) -> dict[str, list[dict[str, Any]]]:
    """Every row of every table, JSON columns parsed, volatile values normalised.

    Rows come in rowid order, so the order the gateway wrote them shows. Plain
    ``SELECT *`` is used, so a new column in a store shows up as a trace change.
    Tables without rows are left out: which empty tables exist depends on what ran before in the process.
    """
    skip = set(skip_tables)
    out: dict[str, list[dict[str, Any]]] = {}
    if not Path(path).exists():
        return out
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            if table in skip:
                continue
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            columns = [col[0] for col in cursor.description]
            rows = []
            fetched = cursor.fetchall()
            if not fetched:
                continue  # a table other tests created and left empty is not part of this scenario
            for raw in fetched:
                row: dict[str, Any] = {}
                for column, cell in zip(columns, raw):
                    if isinstance(cell, bytes):
                        cell = binary_summary(cell)
                    elif isinstance(cell, str) and _JSON_COLUMN.match(cell):
                        try:
                            cell = json.loads(cell)
                        except ValueError:
                            pass
                    row[column] = norm.value(cell, column)
                rows.append(row)
            out[table] = rows
    finally:
        conn.close()
    return out


def describe_assets(directory: Path, norm: Normalizer) -> list[dict[str, Any]]:
    """Stored asset files: suffix, size and sha256 of the bytes, sorted by digest of content.

    Names are content digests, so a file is identified by what it holds. Sidecar
    metadata files (``*.json``) are parsed and normalised instead of hashed.
    """
    out: list[dict[str, Any]] = []
    if not Path(directory).is_dir():
        return out
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        rel = str(path.relative_to(directory))
        entry: dict[str, Any] = {"name": norm.text(rel), "suffix": path.suffix, "size": len(data)}
        if path.suffix == ".json":
            try:
                entry["json"] = norm.value(json.loads(data))
            except ValueError:
                entry["sha256"] = hashlib.sha256(data).hexdigest()
        else:
            entry["sha256"] = hashlib.sha256(data).hexdigest()
        out.append(entry)
    return sorted(out, key=lambda item: (item["suffix"], item.get("sha256", ""), item["name"]))


# --------------------------------------------------------------------------- golden compare

def dumps(trace: Mapping[str, Any]) -> str:
    return json.dumps(trace, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def first_difference(expected: Any, actual: Any, path: str = "$") -> tuple[str, Any, Any] | None:
    """The first (path, expected, actual) at which two JSON values differ, in key order."""
    if type(expected) is not type(actual):
        return path, expected, actual
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key}", "<absent>", actual[key]
            if key not in actual:
                return f"{path}.{key}", expected[key], "<absent>"
            found = first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        for index in range(max(len(expected), len(actual))):
            if index >= len(expected):
                return f"{path}[{index}]", "<absent>", actual[index]
            if index >= len(actual):
                return f"{path}[{index}]", expected[index], "<absent>"
            found = first_difference(expected[index], actual[index], f"{path}[{index}]")
            if found:
                return found
        return None
    return None if expected == actual else (path, expected, actual)


def _top_entry(path: str) -> str:
    """``$.engine_requests[3].body`` -> ``$.engine_requests[3]``."""
    match = re.match(r"^(\$\.[A-Za-z_]+(?:\[\d+\])?)", path)
    return match.group(1) if match else path


def _entry(trace: Any, path: str) -> Any:
    node = trace
    for key, index in re.findall(r"\.([A-Za-z_]+)|\[(\d+)\]", path):
        try:
            node = node[key] if key else node[int(index)]
        except (KeyError, IndexError, TypeError):
            return "<absent>"
    return node


def _text_difference(want: str, got: str) -> str:
    """Where two body texts differ: the first differing JSON path if both parse, else the first differing character."""
    lines: list[str] = []
    try:
        found = first_difference(json.loads(want), json.loads(got))
    except ValueError:
        found = None
    if found:
        lines.append(f"  first differing JSON value at {found[0]}:")
        lines.append(f"    golden: {json.dumps(found[1], ensure_ascii=False)[:300]}")
        lines.append(f"    actual: {json.dumps(found[2], ensure_ascii=False)[:300]}")
    offset = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
    lines.append(f"  first differing character at offset {offset} (golden {len(want)} chars, actual {len(got)} chars):")
    lines.append(f"    golden: ...{want[max(0, offset - 60):offset + 60]!r}")
    lines.append(f"    actual: ...{got[max(0, offset - 60):offset + 60]!r}")
    return "\n".join(lines)


def readable_diff(name: str, expected: Mapping[str, Any], actual: Mapping[str, Any]) -> str:
    found = first_difference(expected, actual)
    if not found:
        return ""
    path, want, got = found
    entry_path = _top_entry(path)
    want_entry = json.dumps(_entry(expected, entry_path), indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    got_entry = json.dumps(_entry(actual, entry_path), indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    diff = "\n".join(difflib.unified_diff(want_entry, got_entry, f"golden {entry_path}", f"actual {entry_path}", lineterm="", n=4))
    detail = _text_difference(want, got) if path.endswith(".text") and isinstance(want, str) and isinstance(got, str) else ""
    return (f"{name}: trace differs from its golden at {path}\n"
            f"  golden: {json.dumps(want, ensure_ascii=False)[:300]}\n"
            f"  actual: {json.dumps(got, ensure_ascii=False)[:300]}\n"
            + (detail + "\n" if detail else "") + f"\n{diff[:6000]}")


def compare_golden(name: str, trace: Mapping[str, Any], *, fixture_dir: Path | None = None,
                   environ: Mapping[str, str] | None = None) -> str:
    """Write the dump (if asked), update the golden (if asked), else compare.

    Returns an empty string when the trace equals its golden, else the readable diff.
    The trace is passed through JSON first, so a golden and a trace compare as parsed documents.
    """
    env = os.environ if environ is None else environ
    directory = fixture_dir or FIXTURE_DIR
    text = dumps(trace)
    out_dir = env.get(OUT_ENV)
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / f"{name}.json").write_text(text, encoding="utf-8")
    golden = directory / f"{name}.json"
    if env.get(UPDATE_ENV) == "1":
        directory.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8")
        return ""
    if not golden.exists():
        return (f"{name}: golden {golden} is missing. Record it with the junit dump flow in "
                f"docs/verification/ENGINE_W6B_DIFFERENTIAL.md and review it.")
    expected = json.loads(golden.read_text(encoding="utf-8"))
    actual = json.loads(text)
    if expected == actual:
        return ""
    return readable_diff(name, expected, actual)


# --------------------------------------------------------------------------- CLI

def extract_junit(junit_xml: Path, out_dir: Path) -> list[str]:
    """Write each ``adapter_trace.<scenario>`` suite property of a junit report to ``<out_dir>/<scenario>.json``.

    The hermetic runner cannot pass environment variables in or write host paths, but
    ``--junit-xml-out PATH`` copies the report out. Every scenario test records its
    trace as the suite property ``adapter_trace.<scenario>``, so the report carries all traces.
    """
    import xml.etree.ElementTree as ET

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for prop in ET.parse(junit_xml).getroot().iter("property"):
        if not (prop.get("name") or "").startswith(JUNIT_PROPERTY + "."):
            continue
        trace = json.loads(prop.get("value") or prop.text or "")
        target = out_dir / f"{trace['scenario']}.json"
        target.write_text(dumps(trace), encoding="utf-8")
        written.append(target.name)
    return sorted(written)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "extract":
        written = extract_junit(Path(argv[1]), Path(argv[2]))
        print(f"wrote {len(written)} traces to {argv[2]}")
        return 0 if written else 1
    print("usage: python -m tests.support.adapter_differential extract <junit.xml> <out-dir>", file=sys.stderr)
    return 2


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
