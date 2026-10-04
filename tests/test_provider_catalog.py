from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from gateway.provider_catalog import load_provider_catalog, normalize_provider_preset


def test_builtin_provider_catalog_is_data_and_has_required_presets() -> None:
    catalog = load_provider_catalog()

    assert {
        "openrouter",
        "openai",
        "anthropic",
        "typesafe",
        "groq",
        "together",
        "deepseek",
        "mistral",
        "google-openai",
        "generic-openai",
        "generic-anthropic",
    } <= set(catalog)
    openrouter = catalog["openrouter"].to_dict()
    assert openrouter["model_listing"]["pricing_path"] == "pricing"
    assert openrouter["auth_test"] == {"method": "GET", "path": "/key"}
    assert catalog["anthropic"].to_dict()["model_listing"]["headers"] == {
        "anthropic-version": "2023-06-01"
    }


@pytest.mark.parametrize(("field", "invalid"), [
    ("protocol", "vendor-specific-code"), ("display_name", ""),
    ("logo_key", None), ("base_url", "https://key:secret@example.test"),
    ("billing_dimensions", "tokens"), ("auth", {"header": "invalid\r\nheader"}),
    ("auth", {"header": "X-Custom-Key", "prefix": ""}),
    ("auth", {"header": "Authorization", "prefix": "Custom"}),
    ("model_listing", {"path": "//other.test/models"}),
    ("model_listing", {"path": "/models", "method": "POST"}),
    ("model_listing", {"path": "/models", "prices": {"input": {"path": "price", "scale": "NaN"}}}),
])
def test_catalog_rejects_invalid_declarations(field: str, invalid: object) -> None:
    raw = copy.deepcopy(load_provider_catalog()["openrouter"].to_dict())
    raw[field] = invalid
    with pytest.raises(ValueError):
        normalize_provider_preset(raw)


def test_operator_catalog_extension_and_duplicate_id(tmp_path: Path) -> None:
    raw = load_provider_catalog()["generic-openai"].to_dict()
    raw.update(id="operator-cloud", display_name="Operator cloud", base_url="http://127.0.0.1:18399/v1")
    (tmp_path / "custom.yml").write_text(yaml.safe_dump(raw))
    assert load_provider_catalog(tmp_path)["operator-cloud"].base_url == raw["base_url"]
    (tmp_path / "duplicate.yml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="duplicate"):
        load_provider_catalog(tmp_path)
