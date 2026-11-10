"""Compatibility contracts for operator descriptions written before protocol traits."""
import pytest
import yaml

from gateway.engine_protocol import description
from gateway.engine_protocol.errors import DescriptionError
from gateway.frontend_shapes.mesh import mesh_profile
from gateway.multipart_adapter import MultipartRunner, runners
from gateway.provider_model_catalog import remote_provider_model_capabilities
from tests.mesh_workflows import bundled_document


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


def test_legacy_multipart_keeps_runner_and_mesh_profile(tmp_path, monkeypatch):
    raw = raw_document("http-multipart")
    raw.pop("implementation")
    raw.pop("multipart")
    # The executor identity still selects the default after the file is renamed.
    raw["id"] = "fixture-form"
    (tmp_path / "fixture-form.yml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(description, "user_directory", lambda: tmp_path)
    doc = description.load_registry()[raw["id"]]
    assert traits(doc) == traits(description.load_shipped_description("http-multipart"))
    assert MultipartRunner(None, doc).adapter_id == doc.adapter_identity
    # Replace the seed to leave exactly one description for this binding.
    monkeypatch.setattr(description, "load_registry", lambda: {doc.id: doc})
    assert len(runners(None)) == 1
    assert mesh_profile(bundled_document()) == []


def test_legacy_catalog_keeps_intent_and_aliases(tmp_path, monkeypatch):
    raw = raw_document("openai-compatible")
    raw.pop("catalog")
    (tmp_path / "openai-compatible.yml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(description, "user_directory", lambda: tmp_path)
    original = description.load_shipped_description(raw["id"])
    doc = description.load_registry()[raw["id"]]
    assert traits(doc) == traits(original)
    for protocol in (doc.id, *original.catalog_aliases):
        rows = remote_provider_model_capabilities("example", {
            "remote_provider": True, "protocol": protocol, "models": [{"id": "example/model"}],
        })
        assert [row.intent for row in rows] == [original.catalog_intent]


def test_unknown_implementation_is_skipped_at_load(tmp_path, monkeypatch):
    raw = raw_document("node-graph")
    raw.update(id="fixture-unknown", implementation="not_registered")
    (tmp_path / "fixture-unknown.yml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(description, "user_directory", lambda: tmp_path)
    assert raw["id"] not in description.load_registry()
    assert any(row["file"] == "fixture-unknown.yml" for row in description.registry_problems())
    with pytest.raises(DescriptionError, match="implementation"):
        description.parse_description(raw, source="fixture-unknown.yml")
    assert set(description.load_registry(description.REGISTRY_DIR)) == {
        path.stem for path in description.REGISTRY_DIR.glob("*.yml")
    }
