from __future__ import annotations

from gateway.provider_catalog import load_provider_catalog


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
