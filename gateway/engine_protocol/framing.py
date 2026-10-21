"""Incremental framing: SSE and NDJSON decoders and a multipart encoder.

The decoders take bytes in any split (a chunk may end inside an event, or
between the CR and LF of a line ending) and return only whole events. Neither
parses JSON: that is the executor's choice, made only when a map or a terminal
rule needs it, so an unmapped stream is relayed without a parse (divan 5d1bed8).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from gateway.engine_protocol.budgets import DEFAULT_BUDGETS
from gateway.engine_protocol.errors import BudgetExceeded


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str | None
    data: bytes


class SseDecoder:
    def __init__(self, max_event_bytes: int = DEFAULT_BUDGETS.max_event_bytes) -> None:
        self._max = max_event_bytes
        self._buf = bytearray()
        self._scan = 0
        self._held_cr = False

    def feed(self, chunk: bytes) -> list[SseEvent]:
        # Normalize only newly arrived bytes, never the growing pending event.
        if self._held_cr:
            chunk = b"\r" + chunk
        self._held_cr = chunk.endswith(b"\r")
        if self._held_cr:
            chunk = chunk[:-1]
        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        self._buf.extend(chunk)
        events: list[SseEvent] = []
        start = 0
        while True:
            end = self._buf.find(b"\n\n", self._scan)
            if end < 0:
                self._scan = max(start, len(self._buf) - 1)
                break
            if end - start > self._max:
                raise BudgetExceeded("max_event_bytes", f"An SSE event is larger than {self._max} bytes.")
            event = self._parse(bytes(self._buf[start:end]))
            if event is not None:
                events.append(event)
            start = end + 2
            self._scan = start
        if start:
            del self._buf[:start]
            self._scan -= start
        if len(self._buf) > self._max:
            raise BudgetExceeded("max_event_bytes", f"An SSE event is larger than {self._max} bytes.")
        return events

    def finish(self) -> list[SseEvent]:
        """A held CR completes a line; an event still needs its blank line."""
        return self.feed(b"\n") if self._held_cr else []

    @staticmethod
    def _parse(block: bytes) -> SseEvent | None:
        if block[:6] == b"data: " and b"\n" not in block:  # the usual event: one data line
            return SseEvent(None, block[6:])
        data: list[bytes] = []
        name: str | None = None
        for line in block.split(b"\n"):
            if not line or line[0] == 58:  # blank, or a ":" comment
                continue
            field, _, value = line.partition(b":")
            if value[:1] == b" ":
                value = value[1:]
            if field == b"data":
                data.append(value)
            elif field == b"event":
                name = value.decode("utf-8", "replace")
        if not data and name is None:
            return None
        return SseEvent(name, data[0] if len(data) == 1 else b"\n".join(data))

    def pending(self) -> bool:
        """True when bytes of an unfinished event are held."""
        return bool(self._buf.strip(b"\r\n"))


class NdjsonDecoder:
    def __init__(self, max_line_bytes: int) -> None:
        self._max = max_line_bytes
        self._buf = bytearray()
        self._scan = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        out: list[bytes] = []
        start = 0
        while True:
            end = self._buf.find(b"\n", self._scan)
            if end < 0:
                self._scan = len(self._buf)
                break
            if end - start > self._max:
                raise BudgetExceeded("max_event_bytes", f"An NDJSON line is larger than {self._max} bytes.")
            line = bytes(self._buf[start:end]).rstrip(b"\r")
            if line:
                out.append(line)
            start = end + 1
            self._scan = start
        if start:
            del self._buf[:start]
            self._scan -= start
        if len(self._buf) > self._max:
            raise BudgetExceeded("max_event_bytes", f"An NDJSON line is larger than {self._max} bytes.")
        return out

    def finish(self) -> list[bytes]:
        """The last line when the stream ended without a final newline."""
        rest = bytes(self._buf).rstrip(b"\r")
        self._buf.clear()
        self._scan = 0
        return [rest] if rest else []


@dataclass(frozen=True, slots=True)
class Part:
    name: str
    data: bytes
    filename: str | None = None
    content_type: str | None = None


def _quote(value: str) -> str:
    return value.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def encode_multipart(parts: list[Part], boundary: str | None = None) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body (RFC 7578) and its Content-Type header value."""
    boundary = boundary or secrets.token_hex(16)
    marker = ("--" + boundary).encode()
    out: list[bytes] = []
    for part in parts:
        if marker in part.data:
            raise ValueError(f"The multipart boundary appears in the data of part '{part.name}'.")
        disposition = f'Content-Disposition: form-data; name="{_quote(part.name)}"'
        if part.filename is not None:
            disposition += f'; filename="{_quote(part.filename)}"'
        head = disposition + "\r\n"
        if part.content_type:
            head += f"Content-Type: {part.content_type}\r\n"
        out.append(marker + b"\r\n" + head.encode() + b"\r\n" + part.data + b"\r\n")
    out.append(marker + b"--\r\n")
    return b"".join(out), f"multipart/form-data; boundary={boundary}"
