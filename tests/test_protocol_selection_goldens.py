"""Pinned shipped selection and catalog contracts before moving selectors to data."""
from dataclasses import asdict

from gateway.engine_protocol import description, reviewed
from gateway.frontend_shapes.mesh import mesh_profile
from gateway.provider_model_catalog import remote_provider_model_capabilities, remote_provider_model_metadata
from tests.mesh_workflows import bundled_document, second_document


def test_shipped_protocol_selection_golden():
    documents = description.load_registry(description.REGISTRY_DIR)
    assert {key: doc.executor for key, doc in documents.items()} == {
        "chunked-stt": "adapter:chunked-stt", "gateway-compose": "adapter:gateway-compose",
        "http-multipart": "adapter:http-multipart", "inline-tts": "adapter:inline-tts",
        "node-graph": "adapter:node-graph", "ollama-native": "http-v1", "openai-compatible": "http-v1",
    }
    for protocol, implementation in [("chunked-stt", "ChunkedSttAdapter"),
                                     ("inline-tts", "InlineTtsAdapter"), ("node-graph", "NodeGraphAdapter")]:
        assert type(reviewed._build(documents[protocol])).__name__ == implementation
    assert mesh_profile(bundled_document()) == []
    assert mesh_profile(second_document()) == []


def test_provider_catalog_golden():
    for protocol in ("openai-compatible", "openai-chat", "anthropic-messages"):
        service = {"config": {"remote_provider": True, "protocol": protocol, "provider_id": "example",
                              "models": [{"id": "example/model"}]}}
        rows = remote_provider_model_capabilities("example", service)
        assert len(rows) == 1
        assert asdict(rows[0]) == asdict(type(rows[0])(
            intent="chat.completions", backend="example", model="example/model", input_modalities=("text",),
            output_modalities=("text",), streaming=True, resource_tier="expensive", tags=("remote",)))
        assert remote_provider_model_metadata("example", service) == {"example/model": {
            "id": "example/model", "model_group": "example/model", "service_id": "example",
            "provider_id": "example", "locality": "remote"}}
