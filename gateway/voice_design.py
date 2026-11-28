"""Read a voice description against the design vocabulary its engine declares.

OpenAI's ``prompt`` is free text ("a warm, calm narrator"). Some engines design a voice only
from a fixed vocabulary. Such an engine's data (``voice_design.vocabulary``) lists ordered
categories of attributes and the words that select each. The first matching attribute of each
category is kept; the words that select nothing are reported, so nothing is silently dropped and
nothing is invented. An engine that declares no vocabulary receives the description unchanged.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The engine request when the data has no ``request`` template.
_DEFAULT_REQUEST = {"input": "{input}", "instructions": "{instructions}", "seed": "{seed}"}


class DesignPromptError(ValueError):
    pass


def design_instruct(prompt: str, settings: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    """Return (the engine's instruction string, words of the prompt that could not be used)."""
    settings = settings or {}
    vocabulary = settings.get("vocabulary")
    if not vocabulary:
        return prompt, []
    text = prompt.lower()
    chosen: list[str] = []
    used: set[str] = set()
    for attributes in vocabulary.values():
        for attribute, words in attributes.items():
            hit = next((w for w in (str(w).lower() for w in words)
                        if re.search(rf"(?<![a-z-]){re.escape(w)}(?![a-z-])", text)), None)
            if hit is not None:
                chosen.append(str(attribute))
                used.update(hit.split())
                break
    if not chosen:
        terms = ", ".join(str(attribute) for attributes in vocabulary.values() for attribute in attributes)
        raise DesignPromptError(
            "The description names no voice attribute this gateway can design. Use words from: " + terms + "."
        )
    ignore = {str(w).lower() for w in settings.get("ignore") or ()}
    ignored = [w for w in dict.fromkeys(re.findall(r"[a-z][a-z'-]*", text)) if w not in used and w not in ignore]
    return str(settings.get("separator", ", ")).join(chosen), ignored


def design_request(settings: Mapping[str, Any] | None, *, script: str, instructions: str, seed: int) -> dict[str, Any]:
    """The engine's speech request body: the data's ``request`` template with the three values filled in.

    A template value that is exactly ``{input}``, ``{instructions}`` or ``{seed}`` is replaced; every
    other value (a fixed voice, a response format) is sent as written, in the template's key order.
    """
    template = (settings or {}).get("request") or _DEFAULT_REQUEST
    values = {"{input}": script, "{instructions}": instructions, "{seed}": seed}
    return {str(key): values.get(value, value) if isinstance(value, str) else value for key, value in template.items()}
