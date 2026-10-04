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
