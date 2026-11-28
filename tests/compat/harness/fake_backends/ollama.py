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


def fake_ollama_chat_stream_chunk(
    *,
    model: str,
    content: str = "",
    done: bool = False,
    created_at: str = "2026-05-15T00:00:00Z",
) -> Dict[str, Any]:
    """Build one streaming ``/api/chat`` NDJSON object."""

    return fake_ollama_chat(
        model=model,
        content=content,
        done=done,
        created_at=created_at,
    )


def fake_ollama_generate(
    *,
    model: str,
    response: str,
    done: bool = True,
    created_at: str = "2026-05-15T00:00:00Z",
) -> Dict[str, Any]:
    """Build a non-streaming ``/api/generate`` response."""

    return {
        "model": model,
        "created_at": created_at,
        "response": response,
        "done": done,
        "done_reason": "stop" if done else None,
    }


def fake_ollama_generate_stream_chunk(
    *,
    model: str,
    response: str = "",
    done: bool = False,
    created_at: str = "2026-05-15T00:00:00Z",
) -> Dict[str, Any]:
    """Build one streaming ``/api/generate`` NDJSON object."""

    return fake_ollama_generate(
        model=model,
        response=response,
        done=done,
        created_at=created_at,
    )


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


def fake_ollama_show(*, model: str) -> Dict[str, Any]:
    """Build an ``/api/show`` model metadata response."""

    return {
        "modelfile": f"FROM {model}",
        "parameters": "",
        "template": "{{ .Prompt }}",
        "details": {
            "format": "gguf",
            "family": "llama",
            "parameter_size": "8B",
            "quantization_level": "Q4_K_M",
        },
        "model_info": {"general.architecture": "llama"},
    }


def fake_ollama_ps(*, models: Iterable[str]) -> Dict[str, Any]:
    """Build an ``/api/ps`` running-model response."""

    return {
        "models": [
            {
                "name": model,
                "model": model,
                "size": 4_000_000_000,
                "digest": "sha256:" + "0" * 64,
                "expires_at": "2026-05-15T00:05:00Z",
                "details": {
                    "format": "gguf",
                    "family": "llama",
                    "parameter_size": "8B",
                    "quantization_level": "Q4_K_M",
                },
            }
            for model in models
        ]
    }


def fake_ollama_embed(*, vector: Iterable[float] = (0.1, 0.2, 0.3)) -> Dict[str, Any]:
    """Build a current ``/api/embed`` response."""

    return {"embeddings": [list(vector)]}


def fake_ollama_embeddings(*, vector: Iterable[float] = (0.1, 0.2, 0.3)) -> Dict[str, Any]:
    """Build a legacy ``/api/embeddings`` response."""

    return {"embedding": list(vector)}
