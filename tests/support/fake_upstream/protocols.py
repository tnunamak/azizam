"""Wire protocols the fake upstream can speak, loaded from ``protocols.json``.

A protocol entry names the paths a scenario may match and how the protocol
frames a stream of events. Code branches on these fields only. The keys are
registry ``protocol`` values (and the two non-provider edges S2 needs); a key
that shares a name with an engine is still a wire-protocol discriminator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Protocol:
    name: str
    # Path templates; ``{name}`` matches one non-empty path segment.
    paths: tuple[str, ...]
    # How an ``{"event": ...}`` body part is framed on the wire.
    framing: str  # "sse-data" | "sse-named-event" | "ndjson" | "none"
    stream_media_type: str = ""
    # Bytes a ``{"done": true}`` part writes, when the protocol has a terminator.
    stream_end: bytes = b""

    def allows(self, path: str) -> bool:
        return any(_template_regex(template).fullmatch(path) for template in self.paths)

    def frame(self, event: Any) -> bytes:
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        if self.framing == "sse-data":
            return f"data: {payload}\n\n".encode()
        if self.framing == "sse-named-event":
            # The event name is the payload's own ``type`` field.
            name = event.get("type") if isinstance(event, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError(f"protocol {self.name!r} frames a named event: the event needs a string 'type'")
            return f"event: {name}\ndata: {payload}\n\n".encode()
        if self.framing == "ndjson":
            return f"{payload}\n".encode()
        raise ValueError(f"protocol {self.name!r} has no event framing")


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", template)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


PROTOCOLS: dict[str, Protocol] = {
    name: Protocol(
        name=name,
        paths=tuple(entry["paths"]),
        framing=entry["framing"],
        stream_media_type=entry.get("stream_media_type", ""),
        stream_end=entry.get("stream_end", "").encode(),
    )
    for name, entry in json.loads((Path(__file__).parent / "protocols.json").read_text(encoding="utf-8")).items()
}
