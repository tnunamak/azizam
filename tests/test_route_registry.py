from __future__ import annotations

from gateway import route_registry, service_registry


def test_route_aliases_and_policies_are_projected_from_catalog() -> None:
    registry = service_registry.build_service_registry_seed({"backends": {}})

    aliases = route_registry.canonical_protocol_route_aliases(registry=registry)
    runtime_policy = route_registry.runtime_route_policy(registry=registry)
    openai_policy = route_registry.openai_proxy_policy(registry=registry)
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
    openai_policy = route_registry.openai_proxy_policy(registry=older_registry)

    assert any(
        entry["canonical"] == "/v1/chat/completions"
        and entry["alias"] == "/openai/v1/chat/completions"
        for entry in aliases
    )
    assert openai_policy["/v1/chat/completions"] == (
        "openai.chat",
        "chat.completions",
    )
