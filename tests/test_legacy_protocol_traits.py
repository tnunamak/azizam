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
        "sdapi": ("", "", (), {}),
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


def test_renamed_chunked_implementation_requires_filenames():
    raw = raw_document("chunked-stt")
    raw.update(id="fixture-transcriber", executor="adapter:fixture-transcriber")
    raw.pop("multipart_filenames")
    with pytest.raises(DescriptionError, match="multipart_filenames"):
        description.parse_description(raw, source="fixture-transcriber.yml")


def test_every_legacy_shipped_copy_selects_original_traits(tmp_path, monkeypatch):
    originals = description.load_registry(description.REGISTRY_DIR)
    for name in originals:
        raw = raw_document(name)
        for key in ("implementation", "multipart", "catalog"):
            raw.pop(key, None)
        (tmp_path / f"{name}.yml").write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(description, "user_directory", lambda: tmp_path)
    # registry_problems() reports the roots of the last merged load.
    assert set(description.load_registry()) == set(originals)
    assert description.registry_problems() == []
    # Load only the copies, so a copy that was skipped cannot be stood in for by its shipped original.
    loaded = description.load_registry(tmp_path)
    assert set(loaded) == set(originals)
    for name, original in originals.items():
        assert traits(loaded[name]) == traits(original), name
        assert loaded[name].multipart_filenames == original.multipart_filenames


def test_explicit_catalog_and_multipart_traits_keep_precedence():
    raw = raw_document("openai-compatible")
    raw["catalog"] = {"intent": "fixture.intent", "aliases": ["fixture-alias"]}
    doc = description.parse_description(raw, source="fixture.yml")
    assert (doc.catalog_intent, doc.catalog_aliases) == ("fixture.intent", ("fixture-alias",))
    raw = raw_document("http-multipart")
    raw.pop("implementation")
    raw["binding_fields"]["endpoint"] = raw["binding_fields"].pop("service")
    raw["multipart"]["service"] = "endpoint"
    doc = description.parse_description(raw, source="fixture.yml")
    assert doc.implementation == "multipart_post"
    assert doc.multipart["service"] == "endpoint"


def test_legacy_inherited_traits_still_require_valid_binding_fields():
    raw = raw_document("http-multipart")
    raw.pop("implementation")
    raw.pop("multipart")
    raw["binding_fields"]["service"]["required"] = False
    with pytest.raises(DescriptionError, match="required service"):
        description.parse_description(raw, source="fixture.yml")


def test_shared_http_executor_does_not_inherit_unrelated_catalog():
    raw = raw_document("openai-compatible")
    raw.update(id="fixture-http")
    raw.pop("catalog")
    doc = description.parse_description(raw, source="fixture.yml")
    assert (doc.catalog_intent, doc.catalog_aliases) == ("", ())


def test_ambiguous_shipped_executor_does_not_supply_traits(tmp_path, monkeypatch):
    raw = raw_document("http-multipart")
    for name in ("fixture-first", "fixture-second"):
        (tmp_path / f"{name}.yml").write_text(yaml.safe_dump({**raw, "id": name}))
    raw.pop("implementation")
    raw.pop("multipart")
    monkeypatch.setattr(description, "REGISTRY_DIR", tmp_path)
    doc = description.parse_description(raw, source="fixture.yml")
    assert traits(doc) == ("", "", (), {})


@pytest.mark.parametrize("unrelated", ["- a list, not a mapping\n", "id: [unclosed\n", "id: one\nid: two\n"])
def test_unreadable_shipped_file_does_not_break_inheritance_for_others(tmp_path, monkeypatch, unrelated):
    """A shipped file that is not a readable mapping supplies no defaults; it reports its own problem when it loads."""
    raw = raw_document("http-multipart")
    (tmp_path / "http-multipart.yml").write_text(yaml.safe_dump(raw))
    (tmp_path / "fixture-broken.yml").write_text(unrelated)
    raw.pop("implementation")
    raw.pop("multipart")
    monkeypatch.setattr(description, "REGISTRY_DIR", tmp_path)
    doc = description.parse_description(raw, source="fixture.yml")
    assert traits(doc) == traits(description.parse_description(raw_document("http-multipart"), source="seed.yml"))


def _synthetic_remote_models(monkeypatch, models, *, nested=False):
    from types import SimpleNamespace

    doc = SimpleNamespace(id="fixture-protocol", catalog_aliases=(), catalog_intent="fixture.intent")
    monkeypatch.setattr(description, "load_registry", lambda: {doc.id: doc})
    config = {"remote_provider": True, "protocol": doc.id, "models": models}
    return remote_provider_model_capabilities(
        "fixture-service", {"config": config} if nested else config,
    )


@pytest.mark.parametrize("nested", [False, True])
def test_remote_model_explicit_traits_are_data(monkeypatch, nested):
    row = {"id": "fixture-model", "input_modalities": ["text", "audio"],
           "output_modalities": ["image"], "streaming": False, "resource_tier": "batch"}
    rows = _synthetic_remote_models(monkeypatch, [row], nested=nested)
    assert len(rows) == 1
    cap = rows[0]
    assert (cap.input_modalities, cap.output_modalities, cap.streaming, cap.resource_tier) == (
        ("text", "audio"), ("image",), False, "batch",
    )
    assert (cap.intent, cap.backend, cap.model, cap.tags) == (
        "fixture.intent", "fixture-service", "fixture-model", ("remote",),
    )
    assert row["input_modalities"] == ["text", "audio"]


def test_remote_model_absent_traits_keep_defaults(monkeypatch):
    cap = _synthetic_remote_models(monkeypatch, {"fixture-model": {}})[0]
    assert (cap.input_modalities, cap.output_modalities, cap.streaming, cap.resource_tier) == (
        ("text",), ("text",), True, "expensive",
    )


@pytest.mark.parametrize("field,value", [
    ("input_modalities", "text"), ("input_modalities", None),
    ("input_modalities", ["text", 1]), ("input_modalities", [""]),
    ("output_modalities", "image"), ("output_modalities", ("image",)),
    ("output_modalities", False), ("output_modalities", [" "]),
    ("streaming", "false"), ("streaming", 0), ("streaming", 1), ("streaming", None),
    ("resource_tier", None), ("resource_tier", 1), ("resource_tier", " "),
    ("resource_tier", "standard"), ("resource_tier", []), ("resource_tier", True),
])
def test_remote_model_malformed_trait_names_row(monkeypatch, field, value):
    with pytest.raises(ValueError) as error:
        _synthetic_remote_models(monkeypatch, [{"id": "bad-fixture", field: value}])
    message = str(error.value)
    assert "fixture-service" in message
    assert "bad-fixture" in message
    assert field in message


def test_remote_model_empty_modality_lists_are_not_defaulted(monkeypatch):
    cap = _synthetic_remote_models(monkeypatch, [{
        "id": "fixture-empty", "input_modalities": [], "output_modalities": [],
    }])[0]
    assert cap.input_modalities == ()
    assert cap.output_modalities == ()


@pytest.mark.parametrize("tier", ["interactive", "batch", "expensive"])
def test_remote_model_resource_tier_uses_capability_vocabulary(monkeypatch, tier):
    cap = _synthetic_remote_models(monkeypatch, [{
        "id": "fixture-tier", "resource_tier": tier,
    }])[0]
    assert cap.resource_tier == tier
