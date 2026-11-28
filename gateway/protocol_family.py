"""The id of the protocol description for the chat-completions HTTP shape, read from the shipped descriptions.

The description that declares ``catalog.intent: chat.completions`` owns the id a stored provider record uses for that
shape. Code that asks "does this provider speak that shape" reads the id here instead of spelling it.
"""
from __future__ import annotations

from functools import lru_cache

from gateway.engine_protocol import description

_CHAT_INTENT = "chat.completions"


@lru_cache(maxsize=1)
def _chat_description() -> description.ProtocolDescription:
    shipped = description.load_registry(description.REGISTRY_DIR)
    matches = [doc for doc in shipped.values() if doc.catalog_intent == _CHAT_INTENT]
    if len(matches) != 1:
        raise RuntimeError(f"exactly one shipped protocol description must carry catalog.intent {_CHAT_INTENT}; found {len(matches)}")
    return matches[0]


def chat_protocol_id() -> str:
    """The id of the description for the chat-completions shape: the name a stored provider record uses."""
    return _chat_description().id
