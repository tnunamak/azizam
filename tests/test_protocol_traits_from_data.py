"""Protocol aliases select validated data traits, never Python import paths."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from gateway.engine_protocol import description, reviewed
from gateway.engine_protocol.adapter import AdapterOrigin
from gateway.engine_protocol.errors import DescriptionError, ProtocolError, UnknownOperation
from gateway.engine_protocol.types import CanonicalRequest, Connection
from gateway.frontend_shapes.mesh import mesh_profile
from gateway import workflow_adapters
from gateway.multipart_adapter import MultipartRunner, runner, runners
from gateway.mesh_manifest import ImageAsset
from gateway.provider_model_catalog import remote_provider_model_capabilities
from tests.mesh_workflows import bundled_document


def document(name):
    return yaml.safe_load((description.REGISTRY_DIR / f"{name}.yml").read_text())


def parsed(raw):
    return description.parse_description(raw, source="fixture.yml")


def test_adapter_identity_comes_from_validated_registration(monkeypatch):
    for name in ("node-graph", "chunked-stt"):
        raw = document(name)
        raw.update(id="fixture-loom", executor="adapter:fixture-worker", label="Fixture Loom")
        doc = parsed(raw)
        adapter = reviewed._build(doc)
        assert adapter.adapter_id == "fixture-worker"
        assert adapter.built_from == AdapterOrigin.of(doc)
        assert adapter.label == "Fixture Loom"
        with pytest.raises(UnknownOperation) as caught:
            asyncio.run(adapter.invoke(Connection(address="https://example.com"), "fixture.nope", CanonicalRequest("fixture.nope")))
        assert "fixture.nope" in str(caught.value)
        raw["implementation"] = "not_registered"
        with pytest.raises(DescriptionError, match="implementation"):
            parsed(raw)
        # Keep the execution-boundary refusal covered even for a forged compiled description.
        unknown = replace(doc, implementation=raw["implementation"])
        with pytest.raises(ProtocolError, match="Unknown reviewed implementation"):
            reviewed._build(unknown)
        monkeypatch.setattr(reviewed, "_candidates", lambda *_: [unknown, parsed(document(name))])
        with pytest.raises(ProtocolError, match="Unknown reviewed implementation"):
            reviewed.adapter_for_backend({"protocol": "fixture-loom"}, "fixture")


def test_protocol_aliases_select_declared_traits(monkeypatch):
    docs = {}
    for name, intent in (("fixture-one", "fixture.first"), ("fixture-two", "fixture.second")):
        raw = document("http-multipart")
        raw.update(id=name, executor=f"adapter:{name}", binding=name, catalog={"intent": intent, "aliases": [name + "-alias"]})
        # Even the binding field spellings can change, provided their validated roles stay the same.
        raw["binding_fields"] = {"endpoint": raw["binding_fields"]["service"], "route": raw["binding_fields"]["path"],
                                 "fields": raw["binding_fields"]["map"]}
        raw["multipart"] = {"service": "endpoint", "path": "route", "map": "fields"}
        doc = parsed(raw)
        docs[name] = doc
    load_registry = description.load_registry
    monkeypatch.setattr(description, "load_registry", lambda directory=None: docs if directory is None else load_registry(directory))
    monkeypatch.setattr(workflow_adapters, "_RUNNERS", {})
    for selected in runners(None):
        workflow_adapters.register(selected)
    assert {row.binding_key for row in runners(None)} == set(docs)
    for name, doc in docs.items():
        contract = bundled_document()
        binding = contract["bindings"].pop("http-multipart")
        contract["bindings"][name] = {"endpoint": binding["service"], "route": binding["path"], "fields": binding["map"]}
        assert mesh_profile(contract) == []
        selected = workflow_adapters.for_binding(name)
        assert selected is workflow_adapters.for_adapter(name)
        assert selected.binding_key == name and selected.adapter_id == name
        file = ImageAsset(b"picture", "fixture.png", "image/png")
        selected._read_file = AsyncMock(return_value=file)
        context = SimpleNamespace(pin=SimpleNamespace(contract=contract), generated={}, inputs={},
                                  media={key: [object()] for key, spec in contract["inputs"].items() if spec["type"] == "asset"})
        file_field, actual_file, scalar = asyncio.run(selected._form(context, contract["bindings"][name]))
        picture = next(key for key, spec in contract["inputs"].items() if spec["type"] == "asset")
        assert file_field == binding["map"][picture] and actual_file == file
        assert isinstance(scalar, dict)
        for protocol in (name, name + "-alias"):
            service = {"remote_provider": True, "protocol": protocol, "models": [{"id": "fixture/model"}]}
            rows = remote_provider_model_capabilities("example", service)
            assert [row.intent for row in rows] == [doc.catalog_intent]
    incomplete = document("http-multipart")
    # No shipped executor default exists for this identity, so absent traits stay absent.
    incomplete.update(id="http-multipart-like", binding="http-multipart-like", executor="adapter:fixture-unregistered")
    del incomplete["multipart"]
    del incomplete["implementation"]
    doc = parsed(incomplete)
    docs[doc.id] = doc
    contract["bindings"] = {doc.binding: binding}
    assert mesh_profile(contract)
    with pytest.raises(ValueError, match="multipart traits"):
        MultipartRunner(None, doc)


@pytest.mark.parametrize("change", ["unknown", "import", "missing_role", "wrong_kind", "optional", "no_binding", "no_form", "no_map", "missing_multipart"])
def test_protocol_traits_refuse_invalid_data(change):
    raw = document("http-multipart")
    if change == "unknown":
        raw["multipart"]["extra"] = "value"
    elif change == "import":
        raw["implementation"] = "package.module:factory"
    elif change == "missing_role":
        del raw["multipart"]["map"]
    elif change == "wrong_kind":
        raw["binding_fields"]["path"]["kind"] = "text"
    elif change == "optional":
        raw["binding_fields"]["service"]["required"] = False
    elif change == "no_binding":
        del raw["binding_fields"]
    elif change == "no_form":
        raw["check_targets"] = "graph"
    elif change == "missing_multipart":
        del raw["multipart"]
    else:
        raw["inputs_via"] = "role"
    with pytest.raises(DescriptionError):
        parsed(raw)


def test_protocol_traits_refuse_duplicate_keys_and_unknown_catalog_fields():
    raw = document("openai-compatible")
    raw["catalog"]["extra"] = "no"
    with pytest.raises(DescriptionError):
        parsed(raw)
    text = yaml.safe_dump(document("http-multipart")) + "implementation: other\n"
    with pytest.raises(DescriptionError):
        description.parse_description_text(text, source="fixture.yml")


def test_legacy_descriptions_inherit_shipped_registration():
    for name, expected in (("node-graph", "NodeGraphAdapter"), ("chunked-stt", "ChunkedSttAdapter"),
                           ("inline-tts", "InlineTtsAdapter")):
        raw = document(name)
        del raw["implementation"]
        assert type(reviewed._build(parsed(raw))).__name__ == expected


def test_explicit_reviewed_registration_keeps_precedence(monkeypatch):
    doc = parsed(document("node-graph"))
    sentinel = SimpleNamespace(interface_version=2)
    monkeypatch.setattr(reviewed, "_FACTORIES", {doc.adapter_identity: lambda _: sentinel})
    monkeypatch.setattr(reviewed, "_BUILT", {})
    assert reviewed._build(doc) is sentinel


def test_multipart_unknown_implementation_is_refused():
    raw = document("http-multipart")
    raw["implementation"] = "unregistered"
    with pytest.raises(DescriptionError, match="implementation"):
        runner(None, parsed(raw))
