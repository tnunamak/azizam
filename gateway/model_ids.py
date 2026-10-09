"""Model identifiers that are safe to show outside the gateway host."""

import re

_PATH_START = re.compile(r"^(?:/|~[\\/]|[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/])")
_MODEL_FILE = re.compile(r"\.(?:gguf|safetensors|ckpt|onnx|bin)$", re.IGNORECASE)


def is_path_like_model_id(value: str) -> bool:
    """True for an absolute, home-relative or drive path, or a bare model file."""
    text = value.strip()
    return bool(_PATH_START.match(text) or _MODEL_FILE.search(text))


def display_model_id(value: str) -> str:
    """Return the identifier unchanged, or only the file name for a path.

    A llama.cpp engine reports the GGUF file it loaded, so a receipt can carry
    a directory on the gateway host. Stored receipts stay as written; members
    and API clients see the short file name.
    """
    if not is_path_like_model_id(value):
        return value
    parts = [part for part in re.split(r"[\\/]", value.strip()) if part]
    return parts[-1] if parts else value
