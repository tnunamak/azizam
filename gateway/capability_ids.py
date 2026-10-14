"""Capability ids and the one mapping from a route intent to a capability.

A leaf module: the catalog (``model_presentation``) and the access-plan
evaluator (``access_plans``) both read it, so neither imports the other.
"""

CAPABILITY_LABELS = {
    "chat": "Chat", "image": "Images", "video": "Video", "speech": "Speech",
    "transcribe": "Transcription", "music": "Music", "sfx": "Sound effects",
    "audio": "Audio", "decide": "Decisions", "embed": "Document search", "rerank": "Rerank",
}
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
    "audio.generate": "audio",
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
    "image.analyze": "image",
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
