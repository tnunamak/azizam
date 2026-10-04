"""Keep generated browser contracts in sync with observable Python payloads."""
import importlib.util
from pathlib import Path

import pytest

from pydantic import TypeAdapter

from gateway.availability import project_service
from gateway.capabilities import Capability, summarize_capabilities
from gateway.models_catalog import build_models_catalog
from gateway.product_contracts import Availability, CapabilitiesResponse, ModelsCatalog


def test_generated_browser_types_are_current():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "generate_product_types", root / "scripts/generate-product-api-types.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.OUTPUT.read_text() == generator.render()


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
