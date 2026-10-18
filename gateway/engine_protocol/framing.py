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
        self._buf = b""

    def feed(self, chunk: bytes) -> list[SseEvent]:
        buf = self._buf + chunk if self._buf else chunk
        held = b""
        if b"\r" in buf:
            if buf.endswith(b"\r"):
                buf, held = buf[:-1], b"\r"  # the LF of a CRLF may open the next chunk
            buf = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        events: list[SseEvent] = []
        start = 0
        find = buf.find
        while True:
            end = find(b"\n\n", start)
            if end < 0:
                break
            if end - start > self._max:
                raise BudgetExceeded("max_event_bytes", f"An SSE event is larger than {self._max} bytes.")
            event = self._parse(buf[start:end])
            if event is not None:
                events.append(event)
            start = end + 2
        rest = buf[start:]
        if len(rest) > self._max:
            raise BudgetExceeded("max_event_bytes", f"An SSE event is larger than {self._max} bytes.")
        self._buf = rest + held
        return events

    def finish(self) -> list[SseEvent]:
        """Events the end of the stream completes: a held CR is a line ending, so ``data: a\\r\\r`` is whole.

        Bytes of an event with no blank line after it stay held (``pending`` is then true): that stream was cut.
        """
        if not self._buf.endswith(b"\r"):
            return []
        held, self._buf = self._buf[:-1], b""
        return self.feed(held + b"\n")

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
        self._buf = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        lines = (self._buf + chunk).split(b"\n") if self._buf else chunk.split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > self._max:
            raise BudgetExceeded("max_event_bytes", f"An NDJSON line is larger than {self._max} bytes.")
        out: list[bytes] = []
        for line in lines:
            if len(line) > self._max:
                raise BudgetExceeded("max_event_bytes", f"An NDJSON line is larger than {self._max} bytes.")
            line = line.rstrip(b"\r")
            if line:
                out.append(line)
        return out

    def finish(self) -> list[bytes]:
        """The last line when the stream ended without a final newline."""
        rest, self._buf = self._buf.rstrip(b"\r"), b""
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
