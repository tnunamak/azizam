"""``WIDTHxHEIGHT``: the one reading of an OpenAI image ``size`` string, for every place that takes one."""
from __future__ import annotations

import re
from typing import Any

SIZE_REFUSAL = "size must be 'auto' or WIDTHxHEIGHT with positive integers"
_DIMENSIONS = re.compile(r"[0-9]+x[0-9]+")


def read_size(text: Any) -> tuple[int, int] | None | bool:
    """``WIDTHxHEIGHT`` in the spellings that worked before strict validation (``512X768``, ``512 x 768``).

    None: no size or ``auto``; False: not a size."""
    if text is None or text == "auto":
        return None
    normalized = re.sub(r"\s*[xX]\s*", "x", text.strip()) if isinstance(text, str) else ""
    if not _DIMENSIONS.fullmatch(normalized):
        return False
    try:
        width, height = (int(part) for part in normalized.split("x"))
    except ValueError:  # digits past Python's integer-text limit
        return False
    return (width, height) if width > 0 and height > 0 else False
