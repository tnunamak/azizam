from __future__ import annotations

from gateway import route_registry, service_registry


def _registry_with_frontend(enabled: bool) -> dict:
    """Build a minimal registry with one protocol frontend."""
    return service_registry.normalize_service_registry(
        {
            "services": {},
            "protocol_frontends": {
                "custom": {
                    "id": "custom",
                    "name": "Custom API",
                    "protocol": "custom",
                    "enabled": enabled,
                    "routes": ["/custom/v1"],
                    "registered_routes": [
                        {
                            "methods": ["POST"],
                            "canonical": "/v1/custom/run",
                            "alias": "/custom/v1/run",
                        }
                    ],
                }
            },
        }
    )


def test_disabled_frontend_routes_excluded_from_registered_routes() -> None:
    registry = _registry_with_frontend(enabled=False)
    routes = route_registry.registered_route_records(registry=registry)
    canonicals = [r.get("canonical") for r in routes]
    assert "/v1/custom/run" not in canonicals, (
        "disabled frontend routes must not appear in registered_route_records"
    )


def test_disabled_frontend_routes_excluded_from_aliases() -> None:
    registry = _registry_with_frontend(enabled=False)
    aliases = route_registry.canonical_protocol_route_aliases(registry=registry)
    alias_canonicals = [entry["canonical"] for entry in aliases]
    assert "/v1/custom/run" not in alias_canonicals, (
        "disabled frontend routes must not appear in canonical_protocol_route_aliases"
    )


def test_enabled_frontend_routes_present_in_registered_routes() -> None:
    registry = _registry_with_frontend(enabled=True)
    routes = route_registry.registered_route_records(registry=registry)
    canonicals = [r.get("canonical") for r in routes]
    assert "/v1/custom/run" in canonicals, (
        "enabled frontend routes must appear in registered_route_records"
    )


def test_route_aliases_and_policies_are_projected_from_catalog() -> None:
    registry = service_registry.build_service_registry_seed({"backends": {}})

    aliases = route_registry.canonical_protocol_route_aliases(registry=registry)
    runtime_policy = route_registry.runtime_route_policy(registry=registry)
    openai_policy = route_registry.azizam_policy(registry=registry)
    asset_policy = route_registry.asset_content_route_policy_by_kind(registry=registry)

    assert {
        (method, entry["canonical"], entry["alias"])
        for entry in aliases
        for method in entry["methods"]
    } >= {
        ("POST", "/v1/chat/completions", "/openai/v1/chat/completions"),
        ("POST", "/v1/messages", "/anthropic/v1/messages"),
        ("POST", "/api/chat", "/ollama/api/chat"),
    }
    assert runtime_policy["/v1/audio/generate"]["intent"] == "audio.generate"
    assert runtime_policy["/v1/videos"]["capability_resource_tier"] == "expensive"
    assert openai_policy["/v1/embeddings"] == (
        "openai.embeddings",
        "text.embeddings",
    )
    assert asset_policy["aud"]["protocol"] == "openai.audio"


def test_route_registry_backfills_builtin_route_facts_for_older_db_records() -> None:
    older_registry = service_registry.normalize_service_registry(
        {
            "services": {},
            "protocol_frontends": {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI-compatible",
                    "protocol": "openai",
                    "routes": ["/openai/v1", "/v1"],
                    "intents": ["chat.completions"],
                }
            },
        }
    )

    aliases = route_registry.canonical_protocol_route_aliases(registry=older_registry)
    openai_policy = route_registry.azizam_policy(registry=older_registry)

    assert any(
        entry["canonical"] == "/v1/chat/completions"
        and entry["alias"] == "/openai/v1/chat/completions"
        for entry in aliases
    )
    assert openai_policy["/v1/chat/completions"] == (
        "openai.chat",
        "chat.completions",
    )
