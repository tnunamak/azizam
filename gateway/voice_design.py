"""Turn a text description of a voice into OmniVoice's voice-design attributes.

OmniVoice designs a voice from a fixed vocabulary, not free text: one gender, age, pitch, style
and accent at most. OpenAI's ``prompt`` is free text ("a warm, calm narrator"). This reads the
description for the attributes OmniVoice knows and says plainly which words it could not use, so
nothing is silently dropped and nothing is invented.
"""
from __future__ import annotations

import re

# category -> (attribute, words that select it), most specific first
_CATEGORIES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "gender": [
        ("female", ("female", "woman", "women", "girl", "lady", "feminine")),
        ("male", ("male", "man", "men", "boy", "guy", "masculine")),
    ],
    "age": [
        ("child", ("child", "kid", "childlike")),
        ("teenager", ("teenager", "teen", "teenage", "adolescent")),
        ("young adult", ("young adult", "young", "twenties", "thirties", "youthful")),
        ("middle-aged", ("middle-aged", "middle aged", "middle age", "forties", "fifties", "mature")),
        ("elderly", ("elderly", "old", "older", "senior", "aged")),
    ],
    "pitch": [
        ("very low pitch", ("very low", "very deep", "bass", "booming")),
        ("very high pitch", ("very high", "squeaky", "shrill")),
        ("low pitch", ("low", "deep", "baritone", "husky")),
        ("high pitch", ("high", "bright", "light", "soprano")),
        ("moderate pitch", ("moderate", "medium", "mid-range", "midrange", "mid pitch")),
    ],
    "style": [("whisper", ("whisper", "whispering", "whispered", "hushed"))],
    "accent": [
        ("american accent", ("american",)),
        ("british accent", ("british", "uk", "u.k.", "london")),
        ("australian accent", ("australian", "aussie")),
        ("canadian accent", ("canadian",)),
        ("indian accent", ("indian",)),
        ("chinese accent", ("chinese",)),
        ("japanese accent", ("japanese",)),
        ("korean accent", ("korean",)),
        ("portuguese accent", ("portuguese",)),
        ("russian accent", ("russian",)),
    ],
}
_ORDER = ("gender", "age", "pitch", "style", "accent")
_NOISE = frozenset({"a", "an", "the", "and", "with", "voice", "pitch", "accent", "sounding", "sounds", "like", "who", "is", "of", "in", "very", "tone", "speaker", "narrator", "person"})
VOCABULARY = ", ".join(attr for cat in _ORDER for attr, _ in _CATEGORIES[cat])


class DesignPromptError(ValueError):
    pass


def design_instruct(prompt: str) -> tuple[str, list[str]]:
    """Return (OmniVoice instruct string, words of the prompt that could not be used)."""
    text = prompt.lower()
    chosen: dict[str, str] = {}
    used: set[str] = set()
    for category in _ORDER:
        for attribute, words in _CATEGORIES[category]:
            hit = next((w for w in words if re.search(rf"(?<![a-z-]){re.escape(w)}(?![a-z-])", text)), None)
            if hit is not None:
                chosen[category] = attribute
                used.update(hit.split())
                break
    if not chosen:
        raise DesignPromptError(
            "The description names no voice attribute this gateway can design. Use words from: " + VOCABULARY + "."
        )
    ignored = [w for w in dict.fromkeys(re.findall(r"[a-z][a-z'-]*", text)) if w not in used and w not in _NOISE]
    return ", ".join(chosen[c] for c in _ORDER if c in chosen), ignored
