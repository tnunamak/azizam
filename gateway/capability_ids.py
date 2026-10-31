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


def offered_model_kinds(words: Iterable[str]) -> list[str]:
    """The kinds that an engine's record capability words serve, in MODEL_KINDS order.

    One mapping from an engine's own words to a kind, so the definitions answer,
    the save check and the probe's suggestion read the engine the same way.
    """
    served = {PROTOCOL_CAPABILITIES.get(word) for word in words}
    return [kind for kind in MODEL_KINDS if kind in served]
