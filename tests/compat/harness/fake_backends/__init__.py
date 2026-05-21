"""Fake backend response builders for SDK-shape contract checks.

These produce canonical SDK-shaped payloads with no network. Probes
that want to verify SDK envelope handling can feed these into an SDK's
parser (or compare to recorded fixtures) without standing up the
gateway, ComfyUI, or any LLM backend.
"""

from .openai_chat import (
    fake_chat_completion,
    fake_chat_completion_chunk,
    fake_chat_completion_stream,
)
from .ollama import (
    fake_ollama_chat,
    fake_ollama_embed,
    fake_ollama_embeddings,
    fake_ollama_tags,
)

__all__ = [
    "fake_chat_completion",
    "fake_chat_completion_chunk",
    "fake_chat_completion_stream",
    "fake_ollama_chat",
    "fake_ollama_embed",
    "fake_ollama_embeddings",
    "fake_ollama_tags",
]
