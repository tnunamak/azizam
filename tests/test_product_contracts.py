"""Keep generated browser contracts in sync with observable Python payloads."""
import importlib.util
import json
from pathlib import Path
import sys
from typing import get_args

import pytest

from pydantic import TypeAdapter

from gateway import product_contracts
from gateway.availability import project_service
from gateway.capabilities import Capability, summarize_capabilities
from gateway.models_catalog import build_models_catalog
from gateway.product_contracts import Availability, CapabilitiesResponse, ModelsCatalog


_ROOT = Path(__file__).resolve().parents[1]
_MODEL_SCHEMA_PATH = _ROOT / "registry/defaults/models.schema.json"
_MODEL_ENUMS = [
    ("ModelType", ("type",)),
    ("InputModality", ("input_modalities", "items")),
    ("OutputModality", ("output_modalities", "items")),
    ("ModelTag", ("tags", "items")),
]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_product_types", _ROOT / "scripts/generate-product-api-types.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return generator


def _enum_definition(schema, path):
    definition = schema["additionalProperties"]["properties"]["model_metadata"]["properties"]
    for key in path:
        definition = definition[key]
    return definition


def _load_contracts_from_schema(schema, tmp_path, monkeypatch):
    schema_path = tmp_path / "models.schema.json"
    schema_path.write_text(schema if isinstance(schema, str) else json.dumps(schema), encoding="utf-8")
    original_open = Path.open

    def open_schema(path, *args, **kwargs):
        return original_open(schema_path if path == _MODEL_SCHEMA_PATH else path, *args, **kwargs)

    # Import a separate module so existing contracts and their consumers are not mutated.
    spec = importlib.util.spec_from_file_location(
        "product_contracts_schema_test", _ROOT / "gateway/product_contracts.py")
    contracts = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, contracts)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_schema)
        spec.loader.exec_module(contracts)
    return contracts


def test_malformed_model_schema_names_the_schema_file(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as failure:
        _load_contracts_from_schema("{", tmp_path, monkeypatch)
    assert str(_MODEL_SCHEMA_PATH) in str(failure.value) and "invalid JSON" in str(failure.value)
    assert isinstance(failure.value.__cause__, json.JSONDecodeError)


def test_missing_model_schema_names_the_schema_file(tmp_path):
    path = tmp_path / "missing-models.schema.json"
    with pytest.raises(ValueError) as failure:
        product_contracts.load_model_metadata_vocabularies(path)
    assert str(failure.value).startswith(f"{path}: cannot read model metadata schema:")
    assert isinstance(failure.value.__cause__, FileNotFoundError)


def test_invalid_utf8_model_schema_names_the_schema_file(tmp_path):
    path = tmp_path / "invalid-utf8-models.schema.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError) as failure:
        product_contracts.load_model_metadata_vocabularies(path)
    assert str(failure.value).startswith(f"{path}: cannot read model metadata schema:")
    assert isinstance(failure.value.__cause__, UnicodeDecodeError)


def test_generated_browser_types_are_current():
    generator = _load_generator()
    assert generator.OUTPUT.read_bytes() == generator.render().encode("utf-8")


@pytest.mark.parametrize("alias,path", _MODEL_ENUMS)
def test_model_contract_enum_matches_schema_in_order(alias, path):
    schema = json.loads(_MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert get_args(getattr(product_contracts, alias)) == tuple(_enum_definition(schema, path)["enum"])


def test_schema_extensions_reach_generated_browser_types(tmp_path, monkeypatch):
    schema = json.loads(_MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))
    additions = ("unfamiliar-model-kind", "unfamiliar-input", "unfamiliar-output", "unfamiliar-tag")
    for (_alias, path), value in zip(_MODEL_ENUMS, additions):
        _enum_definition(schema, path)["enum"].append(value)

    contracts = _load_contracts_from_schema(schema, tmp_path, monkeypatch)
    generator = _load_generator()
    generator.contracts = contracts
    rendered = generator.render()
    for alias, path in _MODEL_ENUMS:
        values = _enum_definition(schema, path)["enum"]
        assert get_args(getattr(contracts, alias)) == tuple(values)
        union = " | ".join(json.dumps(value) for value in values)
        assert f"export type {alias} = {union};" in rendered


@pytest.mark.parametrize("alias,path", _MODEL_ENUMS)
@pytest.mark.parametrize("invalid", ["missing", "empty"])
def test_invalid_schema_enum_fails_at_import(alias, path, invalid, tmp_path, monkeypatch):
    schema = json.loads(_MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))
    definition = _enum_definition(schema, path)
    if invalid == "missing":
        del definition["enum"]
    else:
        definition["enum"] = []
    with pytest.raises(ValueError) as exc:
        _load_contracts_from_schema(schema, tmp_path, monkeypatch)
    assert ".".join((*path, "enum")) in str(exc.value), alias
    expected_message = "Missing" if invalid == "missing" else "non-empty"
    assert expected_message in str(exc.value)


def test_availability_contract_preserves_public_receipt_evidence():
    payload = project_service(
        "service",
        service_records={"services": {"service": {"enabled": True}}},
        evidence={"service": {
            "source": "request", "status": "ready", "available": True,
            "receipt_status": "succeeded", "observed_at_s": 123,
            "request_id": "private-request", "status_code": 200,
        }},
    ).to_public_dict()
    adapter = TypeAdapter(Availability)
    round_trip = adapter.dump_python(adapter.validate_python(payload))
    assert round_trip == payload
    assert round_trip["evidence"]["receipt_status"] == "succeeded"
    assert "request_id" not in round_trip["evidence"]
    assert "status_code" not in round_trip["evidence"]


def test_shared_types_describe_real_catalog_and_capability_payloads():
    capabilities = [Capability(intent="chat.completions", backend="chat-service", model="demo-chat")]
    TypeAdapter(CapabilitiesResponse).validate_python(summarize_capabilities(capabilities, "client"))
    TypeAdapter(ModelsCatalog).validate_python(build_models_catalog(capabilities=capabilities))


def test_catalog_contract_preserves_structured_operator_price():
    # The operator price table documented in docs/REMOTE_PROVIDERS.md.
    price = {"input_usd_per_million_tokens": "3.00", "output_usd_per_million_tokens": "15.00"}
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="provider", model="demo-chat")],
        service_records={"services": {"provider": {"config": {
            "remote_provider": True, "price": price,
        }}}},
    )
    adapter = TypeAdapter(ModelsCatalog)
    round_trip = adapter.dump_python(adapter.validate_python(catalog))
    assert round_trip == catalog
    assert round_trip["models"][0]["cost"] == {"kind": "paid", "price": price}


def test_catalog_contract_preserves_technical_description_detail():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="service", model="model")],
        model_entries=[{"id": "model", "description": "Served via local llama.cpp on :5051."}],
    )
    adapter = TypeAdapter(ModelsCatalog)
    round_trip = adapter.dump_python(adapter.validate_python(catalog))
    assert round_trip["models"][0]["description"] == "A chat model."
    assert round_trip["models"][0]["technical_description"] == "Served via local llama.cpp on :5051."


@pytest.mark.parametrize("intent", ["image.edit", "speech.transcription", "decision.systemone"])
def test_catalog_contract_covers_multipart_and_unsupported_snippets(intent):
    catalog = build_models_catalog(capabilities=[Capability(intent=intent, backend="service", model="model")])
    TypeAdapter(ModelsCatalog).validate_python(catalog)


def test_catalog_contract_covers_alias_configuration_error():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="service", model="model")],
        alias_views={"chat": {"stable": [{"alias": "fast", "resolves_to": "model", "error": "No route"}]}},
    )
    assert catalog["aliases"][0]["availability"]["status"] == "unavailable"
    TypeAdapter(ModelsCatalog).validate_python(catalog)


@pytest.mark.parametrize("ready,expected", [(True, "ready"), (None, "unknown")])
def test_remote_model_price_and_alias_share_observed_availability(ready, expected):
    import asyncio
    from gateway.availability import project_capabilities
    from gateway.local_capabilities import build_alias_capabilities
    from gateway.models_catalog import probe_service_health, registered_model_capabilities
    registry = {"services": {"cloud-service": {"enabled": True, "config": {
        "remote_provider": True, "provider_type": "paid-remote", "provider_id": "openrouter",
        "protocol": "openai-compatible", "base_url": "https://provider.example/v1", "health_endpoint": "/health",
        "models": [{"model_group": "openrouter/visible", "upstream_model": "visible-upstream"}],
        "price": {"models": {"openrouter/visible": {
            "input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15",
        }}},
    }}}}
    views = {"chat": {"stable": [{"alias": "fast", "resolves_to": "openrouter/visible"}]}}
    async def get_json(url):
        assert url == "https://provider.example/health"
        return {"ready": ready} if ready is not None else {"object": "unmeasured"}
    evidence = asyncio.run(probe_service_health(registry, get_json=get_json))
    capabilities = project_capabilities([
        *registered_model_capabilities(registry), *build_alias_capabilities(views),
    ], service_records=registry, evidence=evidence)
    catalog = build_models_catalog(capabilities=capabilities, alias_views=views,
                                  service_records=registry, health_evidence=evidence)
    adapter = TypeAdapter(ModelsCatalog)
    round_trip = adapter.dump_python(adapter.validate_python(catalog))
    assert round_trip == catalog
    for row in [*round_trip["aliases"], *round_trip["models"]]:
        assert row["locality"] == "remote"
        assert row["cost"] == {"kind": "paid", "price": "$3 input / $15 output per 1M tokens"}
        assert row["availability"]["status"] == expected
        assert row["availability"]["evidence"]["observed_at_s"] == evidence["cloud-service"]["observed_at_s"]
    assert [row["id"] for row in round_trip["aliases"]] == ["fast"]
    assert [row["id"] for row in round_trip["models"]] == ["openrouter/visible"]
    alias = round_trip["aliases"][0]
    assert alias["resolves_to"] == {"chat": "openrouter/visible"}
    assert alias["same_model_as"] == ["openrouter/visible"]
    # The alias and the model it points at are two rows that carry the same observed
    # price and availability (checked in the loop above). Compare the unaliased catalog too.
    concrete = build_models_catalog(capabilities=capabilities, service_records=registry,
                                   health_evidence=evidence)["models"][0]
    assert concrete["id"] == "openrouter/visible"
    assert alias["cost"] == concrete["cost"]
    assert alias["availability"] == concrete["availability"]


def test_chat_alias_does_not_hide_same_named_concrete_embedding_model():
    from gateway.availability import project_capabilities
    from gateway.local_capabilities import build_alias_capabilities
    views = {"chat": {"stable": [{"alias": "fast", "resolves_to": "chat-model"}]}}
    capabilities = project_capabilities([
        Capability(intent="chat.completions", backend="chat", model="chat-model"),
        Capability(intent="text.embeddings", backend="embedding", model="fast"),
        *build_alias_capabilities(views),
    ])
    catalog = build_models_catalog(capabilities=capabilities, alias_views=views)
    assert [row["id"] for row in catalog["aliases"]] == ["fast"]
    # The chat model `fast` points at is a row too; the embedding model that shares the alias's name stays distinct.
    assert {row["id"] for row in catalog["models"]} == {"chat-model", "fast"}
    assert catalog["aliases"][0]["resolves_to"] == {"chat": "chat-model"}
    embedding = next(row for row in catalog["models"] if row["id"] == "fast")
    assert [cap["intent"] for cap in embedding["capabilities"]] == ["text.embeddings"]

def test_home_contract_round_trips_optional_required_media_inputs():
    from gateway.home_api import _capability_rows
    from gateway.product_contracts import HomeCapability

    row, = _capability_rows([Capability(intent="chat.completions", backend="service", model="demo")], {})
    adapter = TypeAdapter(HomeCapability)
    assert "required_media_inputs" not in adapter.dump_python(adapter.validate_python(row))
    media = [
        {"name": "image", "label": "a starting picture", "modality": "image", "accepts": ["image/*"]},
        {"name": "last_frame", "label": "an ending picture", "modality": "image", "accepts": ["image/*"]},
    ]
    with_media = {**row, "required_media_inputs": media}
    assert adapter.dump_python(adapter.validate_python(with_media)) == with_media
    schema = adapter.json_schema()
    assert "required_media_inputs" in schema["properties"]
    assert "required_media_inputs" not in schema.get("required", [])


def test_browser_types_expose_required_media_input_fields():
    types = (Path(__file__).resolve().parents[1] / "web/src/lib/product-api-types.ts").read_text()
    assert "export interface RequiredMediaInput {" in types
    assert "  required_media_inputs?: Array<RequiredMediaInput>;" in types
