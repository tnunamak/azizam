"""Wire protocols the fake upstream can speak, as data.

A protocol entry names the paths a scenario may match and how the protocol
frames a stream of events. Code branches on these fields only. The keys are
registry ``protocol`` values (and the two non-provider edges S2 needs); a key
that shares a name with an engine is still a wire-protocol discriminator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Protocol:
    name: str
    # Path templates; ``{name}`` matches one non-empty path segment.
    paths: tuple[str, ...]
    # How an ``{"event": ...}`` body part is framed on the wire.
    framing: str  # "sse-data" | "sse-typed" | "ndjson" | "none"
    stream_media_type: str = ""
    # Bytes a ``{"done": true}`` part writes, when the protocol has a terminator.
    stream_end: bytes = b""

    def allows(self, path: str) -> bool:
        return any(_template_regex(template).fullmatch(path) for template in self.paths)

    def frame(self, event: Any) -> bytes:
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        if self.framing == "sse-data":
            return f"data: {payload}\n\n".encode()
        if self.framing == "sse-typed":
            return f"event: {event['type']}\ndata: {payload}\n\n".encode()
        if self.framing == "ndjson":
            return f"{payload}\n".encode()
        raise ValueError(f"protocol {self.name!r} has no event framing")


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", template)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


PROTOCOLS: dict[str, Protocol] = {
    protocol.name: protocol
    for protocol in (
        Protocol(
            name="openai-compatible",
            paths=(
                "/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/models",
                "/v1/models/{model}", "/v1/responses", "/v1/audio/speech",
                "/v1/audio/transcriptions", "/v1/audio/translations", "/v1/images/generations",
                "/v1/images/edits", "/v1/rerank", "/health", "/v1/health",
            ),
            framing="sse-data",
            stream_media_type="text/event-stream",
            stream_end=b"data: [DONE]\n\n",
        ),
        Protocol(
            name="anthropic-messages",
            paths=("/v1/messages", "/v1/messages/count_tokens", "/v1/models", "/v1/models/{model}"),
            framing="sse-typed",
            stream_media_type="text/event-stream",
        ),
        Protocol(
            name="ollama",
            paths=(
                "/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/api/tags",
                "/api/show", "/api/ps", "/api/version",
            ),
            framing="ndjson",
            stream_media_type="application/x-ndjson",
        ),
        Protocol(
            name="comfyui",
            paths=(
                "/prompt", "/history", "/history/{prompt_id}", "/view", "/object_info",
                "/object_info/{node_class}", "/queue", "/interrupt", "/upload/image",
                "/system_stats", "/free",
            ),
            framing="none",
        ),
        Protocol(
            name="litellm-admin",
            paths=(
                "/key/generate", "/key/info", "/key/list", "/key/delete", "/key/update",
                "/key/block", "/key/unblock", "/user/new", "/user/info", "/model/info",
                "/v1/model/info", "/health/liveliness", "/health/readiness",
            ),
            framing="none",
        ),
        Protocol(
            name="oidc-idp",
            paths=("/.well-known/openid-configuration", "/token", "/jwks", "/userinfo", "/authorize"),
            framing="none",
        ),
    )
}
