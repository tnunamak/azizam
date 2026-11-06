"""Per-protocol allowlists and normalizers for recorded transcripts (TEST_STRATEGY 4.7).

The allowlists are data (``allowlists.json``). This module only applies them, by
leaf kind and never by engine or protocol name. A response field the allowlist
does not name is dropped; a field it names with the wrong shape fails the
recording, so a surprise never lands in a committed file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_DATA = json.loads((Path(__file__).parent / "allowlists.json").read_text(encoding="utf-8"))
REQUEST_HEADERS: dict[str, list[str]] = _DATA["request_headers"]
RESPONSE_HEADERS: list[str] = _DATA["response_headers"]
RESPONSE_SHAPES: dict[str, Any] = _DATA["response_shapes"]
REPLAY_HOST = "https://replay.invalid"
_SCALARS = (str, int, float, bool, type(None))


class NormalizeError(ValueError):
    pass


def request_headers(protocol: str, headers: dict[str, str]) -> dict[str, str]:
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: lower[k] for k in REQUEST_HEADERS[protocol] if k in lower}


def response_headers(headers: dict[str, str]) -> dict[str, str]:
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: lower[k] for k in RESPONSE_HEADERS if k in lower}


class Normalizer:
    """One recording's normalizer. Ids map one-to-one to rp-id-N, in first-seen order."""

    def __init__(self, protocol: str) -> None:
        if protocol not in RESPONSE_SHAPES:
            raise NormalizeError(f"no allowlist for protocol {protocol!r}")
        self.protocol = protocol
        self.ids: dict[str, str] = {}

    def response(self, value: Any) -> Any:
        return self._project(value, RESPONSE_SHAPES[self.protocol], "$")

    def _project(self, value: Any, shape: Any, where: str) -> Any:
        if isinstance(shape, dict):
            if not isinstance(value, dict):
                raise NormalizeError(f"{where}: object expected")
            return {key: self._project(value[key], child, f"{where}.{key}") for key, child in shape.items() if key in value}
        if isinstance(shape, list):
            if not isinstance(value, list):
                raise NormalizeError(f"{where}: list expected")
            if not shape:
                if value:
                    raise NormalizeError(f"{where}: expected an empty list (upstream is not idle)")
                return []
            return [self._project(item, shape[0], f"{where}[]") for item in value]
        if not isinstance(value, _SCALARS):
            raise NormalizeError(f"{where}: scalar expected")
        if shape == "id" and value is not None:
            if not isinstance(value, str) or not value:
                raise NormalizeError(f"{where}: id must be a non-empty string")
            return self.ids.setdefault(value, f"rp-id-{len(self.ids) + 1}")
        if shape == "model":
            # An upstream may echo the model's file path; only the last segment is kept.
            return value.replace("\\", "/").rsplit("/", 1)[-1] if isinstance(value, str) else value
        if shape == "time":
            return 0 if isinstance(value, (int, float)) and not isinstance(value, bool) else "2000-01-01T00:00:00Z"
        if shape == "url":
            url = urlsplit(str(value))
            if url.scheme not in ("http", "https") or not url.hostname:
                raise NormalizeError(f"{where}: unsupported URL")
            return REPLAY_HOST + (url.path or "/")
        return value


def parse_stream(framing: str, body: bytes) -> list[dict[str, Any]]:
    """Split a recorded stream into scenario body parts: ``{"event": ...}`` and ``{"done": true}``.

    Chunk boundaries on the wire are not kept. Anything that is not a complete
    event (a comment, a keep-alive, a partial record) fails the recording.
    """
    text = body.decode("utf-8")
    parts: list[dict[str, Any]] = []
    if framing == "ndjson":
        for line in text.split("\n"):
            if line.strip():
                parts.append({"event": json.loads(line)})
        return parts
    if framing not in ("sse-data", "sse-named-event"):
        raise NormalizeError(f"framing {framing!r} carries no events")
    if text and not text.endswith("\n\n"):
        raise NormalizeError("stream ends inside an event")
    for block in text.split("\n\n"):
        if not block:
            continue
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            else:
                raise NormalizeError("stream holds a line that is not an event field")
        payload = "\n".join(data)
        if payload == "[DONE]":
            parts.append({"done": True})
            continue
        event = json.loads(payload)
        if framing == "sse-named-event" and (not isinstance(event, dict) or event.get("type") != name):
            raise NormalizeError("event name differs from the payload type")
        parts.append({"event": event})
    return parts
