"""Keep generated browser contracts in sync with observable Python payloads."""
import importlib.util
from pathlib import Path

import pytest

from pydantic import TypeAdapter

from gateway.capabilities import Capability, summarize_capabilities
from gateway.models_catalog import build_models_catalog
from gateway.product_contracts import CapabilitiesResponse, ModelsCatalog


def test_generated_browser_types_are_current():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "generate_product_types", root / "scripts/generate-product-api-types.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.OUTPUT.read_text() == generator.render()


def test_shared_types_describe_real_catalog_and_capability_payloads():
    capabilities = [Capability(intent="chat.completions", backend="chat-service", model="demo-chat")]
    TypeAdapter(CapabilitiesResponse).validate_python(summarize_capabilities(capabilities, "client"))
    TypeAdapter(ModelsCatalog).validate_python(build_models_catalog(capabilities=capabilities))


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
