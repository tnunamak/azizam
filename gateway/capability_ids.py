"""Capability ids and the one mapping from a route intent to a capability.

A leaf module: the catalog (``model_presentation``) and the access-plan
evaluator (``access_plans``) both read it, so neither imports the other.
"""

from collections.abc import Iterable

CAPABILITY_LABELS = {
    "chat": "Chat", "image": "Images", "video": "Video", "speech": "Voice",
    "transcribe": "Transcription", "music": "Music", "sfx": "Sound effects",
    "audio": "Audio", "decide": "Decisions", "embed": "Document search", "rerank": "Reranking",
    "mesh": "3D models",
}
# The kinds a registered model may declare: the one capability it serves, with
# its builder label. The kinds they share with the catalog take the catalog's
# word. The catalog and engine pages use the same capability label.
# The catalog binds a kinded model only the capabilities of its kind. A model
# with no kind, or a kind not listed here, keeps every capability its engine
# offers. These are the families registered models bind.
MODEL_KIND_LABELS = {
    "chat": CAPABILITY_LABELS["chat"], "embed": CAPABILITY_LABELS["embed"],
    "speech": CAPABILITY_LABELS["speech"], "transcribe": CAPABILITY_LABELS["transcribe"],
}
MODEL_KINDS = tuple(MODEL_KIND_LABELS)
PROTOCOL_CAPABILITIES = {
    "llm.execution": "chat", "typesafe-systemone": "decide",
    "chat": "chat", "completions": "chat", "chat.completions": "chat", "vision": "chat",
    "speech": "speech", "tts": "speech", "audio.speech": "speech", "speech.synthesis": "speech",
    "transcriptions": "transcribe", "transcription": "transcribe", "stt": "transcribe",
    "audio.transcriptions": "transcribe", "speech.transcription": "transcribe",
    "speech.translation": "transcribe", "images": "image", "image": "image", "generations": "image",
    "image.generate": "image", "image.edit": "image", "videos": "video", "video": "video",
    "video.generate": "video", "embeddings": "embed", "text.embeddings": "embed",
    "rerank": "rerank", "text.rerank": "rerank", "decisions": "decide",
    "decision.systemone": "decide", "music": "music", "sfx": "sfx", "audio": "audio",
    "audio.generate": "audio", "mesh": "mesh",
}

# The one mapping from a route intent to a capability (design §2.2, RT M4).
# The catalog's service capability names, plus the intents routes emit that
# the catalog does not name. tests/test_access_plans.py fails if a route emits
# an intent missing here.
INTENT_CAPABILITIES = {
    **PROTOCOL_CAPABILITIES,
    "text.completions": "chat",
    "chat.responses": "chat",
    "chat.generate": "chat",
    "chat.messages": "chat",
    "chat.messages.count_tokens": "chat",
    "music.generate": "music",
    "sfx.generate": "sfx",
    "audio.transcriptions": "transcribe",
    "image.variation": "image",
    "video.remix": "video",
    "video.edit": "video",
    "video.extend": "video",
    "image.analyze": "image",
    "mesh.generate": "mesh",
    # Route spellings protocol frontends use for the same work.
    "embedding": "embed",
    "images.generate": "image",
    "images.generations": "image",
    "images.edit": "image",
    "images.edits": "image",
    "audio.transcribe": "transcribe",
    "audio.translations": "transcribe",
    "videos.generate": "video",
}


# The OpenAI-compatible endpoints the gateway's lifecycle hop serves (``/internal/litellm/local/{backend}/v1<path>``),
# by the capability that needs them. This is the one table: the hop routes by it, and the service registry refuses a
# transition-enabled member that declares a capability with no row here (that member would be reached only by an
# endpoint the hop answers with 404).
INNER_HOP_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "chat": ("/chat/completions", "/completions"),
    "embed": ("/embeddings",),
}
# The admission intent and resource class of each inner-hop endpoint, as the outer routes name them.
INNER_HOP_ADMISSION: dict[str, tuple[str, str]] = {
    "/chat/completions": ("chat.generate", "chat"),
    "/completions": ("chat.generate", "chat"),
    "/embeddings": ("text.embeddings", "embeddings"),
}


def capability_of_word(word: str) -> str:
    """The capability a record's capability word stands for (an unknown word stands for itself)."""
    text = str(word or "").strip()
    return INTENT_CAPABILITIES.get(text, text)


def inner_hop_endpoint_served(endpoint: str) -> bool:
    """Whether any capability maps to the inner-hop endpoint ``endpoint`` (a path such as ``/embeddings``)."""
    return any(endpoint in paths for paths in INNER_HOP_ENDPOINTS.values())


def inner_hop_endpoints_for(words: Iterable[str]) -> frozenset[str]:
    """The inner-hop endpoints that a record with these capability words is reached by."""
    served: set[str] = set()
    for word in words:
        served.update(INNER_HOP_ENDPOINTS.get(capability_of_word(word), ()))
    return frozenset(served)


def capabilities_without_inner_endpoint(words: Iterable[str]) -> list[str]:
    """The capability words (as declared) that stand for a real capability no inner-hop endpoint serves.

    A word that maps to no capability (the ``openai`` compatibility marker, for one) asks for no endpoint, so it is
    not refused: only a word that resolves to a ``CAPABILITY_LABELS`` key without a row in ``INNER_HOP_ENDPOINTS`` is.
    """
    refused: list[str] = []
    for word in words:
        capability = capability_of_word(word)
        if capability in CAPABILITY_LABELS and capability not in INNER_HOP_ENDPOINTS:
            refused.append(word)
    return refused


def offered_model_kinds(words: Iterable[str]) -> list[str]:
    """The kinds that an engine's record capability words serve, in MODEL_KINDS order.

    One mapping from an engine's own words to a kind, so the definitions answer,
    the save check and the probe's suggestion read the engine the same way.
    """
    served = {PROTOCOL_CAPABILITIES.get(word) for word in words}
    return [kind for kind in MODEL_KINDS if kind in served]
