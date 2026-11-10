"""Compatibility contracts for operator descriptions written before protocol traits."""
import yaml

from gateway.engine_protocol import description


def raw_document(name):
    return yaml.safe_load((description.REGISTRY_DIR / f"{name}.yml").read_text())


def traits(doc):
    return (doc.implementation, doc.catalog_intent, doc.catalog_aliases, dict(doc.multipart))


def test_shipped_traits_golden():
    assert {name: traits(doc) for name, doc in description.load_registry(description.REGISTRY_DIR).items()} == {
        "chunked-stt": ("chunked_transcription", "", (), {}),
        "gateway-compose": ("", "", (), {}),
        "http-multipart": ("multipart_post", "", (), {"service": "service", "path": "path", "map": "map"}),
        "inline-tts": ("inline_speech", "", (), {}),
        "node-graph": ("graph_submission", "", (), {}),
        "ollama-native": ("", "", (), {}),
        "openai-compatible": ("", "chat.completions", ("openai-chat", "anthropic-messages"), {}),
    }
