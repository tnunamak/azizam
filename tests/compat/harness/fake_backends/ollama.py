"""Canonical Ollama native API response shapes.

References:
- https://docs.ollama.com/api
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def fake_ollama_chat(
    *,
    model: str,
    content: str,
    done: bool = True,
    created_at: str = "2026-05-15T00:00:00Z",
) -> Dict[str, Any]:
    """Build a non-streaming ``/api/chat`` response."""

    return {
        "model": model,
        "created_at": created_at,
        "message": {"role": "assistant", "content": content},
        "done": done,
        "done_reason": "stop" if done else None,
    }


def fake_ollama_tags(
    *,
    models: Iterable[str],
    size: int = 4_000_000_000,
) -> Dict[str, Any]:
    """Build a ``/api/tags`` model listing response."""

    items: List[Dict[str, Any]] = []
    for name in models:
        items.append(
            {
                "name": name,
                "model": name,
                "modified_at": "2026-05-15T00:00:00Z",
                "size": size,
                "digest": "sha256:" + "0" * 64,
                "details": {
                    "format": "gguf",
                    "family": "llama",
                    "parameter_size": "8B",
                    "quantization_level": "Q4_K_M",
                },
            }
        )
    return {"models": items}


def fake_ollama_embed(*, vector: Iterable[float] = (0.1, 0.2, 0.3)) -> Dict[str, Any]:
    """Build a current ``/api/embed`` response."""

    return {"embeddings": [list(vector)]}


def fake_ollama_embeddings(*, vector: Iterable[float] = (0.1, 0.2, 0.3)) -> Dict[str, Any]:
    """Build a legacy ``/api/embeddings`` response."""

    return {"embedding": list(vector)}
