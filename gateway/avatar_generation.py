"""How a person's generated avatar is asked for: the fixed prompt, its seed, and the image route.

Nothing here names an engine or a model. The route is the first image capability the person may use right
now, read from the catalog the gateway already keeps. The prompt is a fixed template: its only input is a
seed, so no name, address or photo can reach it.
"""
from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from gateway.capabilities import Capability

# Bump it when the template changes. A picture made from an older template is not shown (gateway/avatars.py).
TEMPLATE_VERSION = "1"
IMAGE_INTENT = "image.generate"
# The style is the only part of the prompt the seed chooses. A new attempt is a new seed, so a new style.
STYLES = (
    "flat vector",
    "soft watercolour",
    "paper cut-out",
    "geometric",
    "pastel gradient",
    "ink line art",
    "stained glass",
    "low-poly",
)
_TEMPLATE = (
    "A round avatar illustration: one simple abstract emblem, centred, {style} style, "
    "bold clean shapes, soft even light, plain background. "
    "No face, no person, no text, no letters, no numbers."
)


def build_prompt(seed: int) -> str:
    """The prompt for one attempt. It holds the seed's style and nothing about the person asking."""
    return _TEMPLATE.format(style=STYLES[seed % len(STYLES)])


def new_seed() -> int:
    return secrets.randbelow(1_000_000)


def pick_route(capabilities: Iterable[Capability]) -> str | None:
    """The model id of the first image capability that is available and permitted, or None.

    The catalog lists capabilities in a fixed order (their sort key), so the same catalog picks the same route.
    """
    usable = [
        capability for capability in capabilities
        if capability.intent == IMAGE_INTENT and capability.model and capability.available
        and capability.policy_available
    ]
    for capability in sorted(usable, key=lambda capability: capability.sort_key):
        return capability.model
    return None
