from gateway.capabilities import Capability
from gateway.service_usage import relied_on_service_ids


def test_unused_discovery_and_disabled_routes_do_not_trigger_attention():
    registry = {"services": {"legacy": {"enabled": True}, "primary": {"enabled": True},
                              "disabled": {"enabled": False}}}
    caps = [Capability(intent="chat.completions", backend="legacy"),
            Capability(intent="chat.completions", backend="primary", model="fast", tags=("alias",)),
            Capability(intent="chat.completions", backend="disabled", model="image", tags=("alias",))]
    assert relied_on_service_ids(registry=registry, runtime_config={"routing": {"default_backend": "disabled"}},
                                 capabilities=caps) == ["primary"]


def test_explicit_routes_and_model_mappings_are_reliance_evidence():
    registry = {"services": {"default": {}, "role": {}, "mapped": {"config": {"models": [{"id": "chat"}]}},
                              "off-model": {"config": {"models": [{"id": "unused", "enabled": False}]}}}}
    config = {"routing": {"default_backend": "default", "check_interval": 30},
              "roles": {"coder": {"backend": "role"}, "disabled": {"backend": "off-model", "enabled": False}}}
    assert relied_on_service_ids(registry=registry, runtime_config=config, capabilities=[]) == ["default", "mapped", "role"]


def test_unresolved_alias_and_metadata_do_not_invent_reliance():
    assert relied_on_service_ids(registry={"services": {"unused": {"name": "Default engine"}}},
                                 runtime_config={}, capabilities=[Capability(intent="image.generate", backend="gateway", model="image", tags=("alias",))]) == []


def test_selected_protocol_default_is_used_without_alias_or_model_mapping():
    registry = {"services": {"transcription": {}, "fallback": {}, "disabled": {"enabled": False}}}
    assert relied_on_service_ids(registry=registry, runtime_config={}, capabilities=[],
                                 selected_defaults=["transcription", "disabled"]) == ["transcription"]


def test_selected_execution_substrate_is_used_without_its_own_model_mapping():
    from gateway.backends.llm_execution import litellm_runtime_from_registry

    registry = {"services": {
        "router-main": {"kind": "llm-substrate", "priority": 1,
                        "config": {"base_url": "http://router-main.test"}},
        "router-idle": {"kind": "llm-substrate", "priority": 2,
                        "config": {"base_url": "http://router-idle.test"}},
        "chat-engine": {},
        "unused-engine": {},
    }}
    execution = litellm_runtime_from_registry(registry, env={})
    assert execution.enabled and execution.service_id == "router-main"
    used = relied_on_service_ids(
        registry=registry, runtime_config={},
        capabilities=[Capability(intent="chat.completions", backend="chat-engine",
                                 model="fast", tags=("alias",)),
                      Capability(intent="chat.completions", backend="unused-engine")],
        selected_execution_dependencies=[execution.service_id],
    )
    assert used == ["chat-engine", "router-main"]


def test_execution_dependencies_require_enabled_registry_records():
    registry = {"services": {"selected": {}, "disabled": {"enabled": False}, "unused": {}}}
    assert relied_on_service_ids(
        registry=registry, runtime_config={}, capabilities=[],
        selected_execution_dependencies=["selected", "disabled", "missing"],
    ) == ["selected"]


def test_in_use_substrate_failure_remains_actionable_with_unused_engine_failure():
    from gateway.admin_api import derive_admin_health_projection
    registry = {"services": {"router": {}, "unused": {}}}
    used = relied_on_service_ids(registry=registry, runtime_config={}, capabilities=[],
                                 selected_execution_dependencies=["router"])
    health = derive_admin_health_projection(registry, {
        "_relied_on_service_ids": used,
        "backends": [{"service_id": "router", "live_status": "down"},
                     {"service_id": "unused", "live_status": "down"}],
    }, observation_available=True)
    assert health["state"] == "bad"
    rows = {row["service_id"]: row for row in health["services"]}
    assert rows["router"]["in_use"] is True
    assert rows["router"]["tone"] == "bad"
    assert rows["unused"]["in_use"] is False
    assert rows["unused"]["tone"] == "neutral"


def test_status_producer_includes_selected_execution_dependency(monkeypatch):
    import asyncio
    import proxy

    registry = {"services": {
        "router-selected": {"kind": "llm-substrate", "config": {"base_url": "http://router.test"}},
        "unused": {},
    }}
    async def status_payload(config):
        return {"backends": []}
    async def evidence(*args, **kwargs):
        return {}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", status_payload)
    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", evidence)
    monkeypatch.setattr(proxy, "_runtime_availability_evidence", lambda *args, **kwargs: {})
    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", lambda context: [])
    monkeypatch.setattr(proxy, "service_for_capability", lambda capability: None)
    monkeypatch.setattr(proxy, "_highest_priority_chat_backend", lambda: None)
    monkeypatch.setattr(proxy, "config", {})
    payload = asyncio.run(proxy._status_payload_with_availability())
    assert payload["_relied_on_service_ids"] == ["router-selected"]
