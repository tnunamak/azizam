"""Configured decision protocols reach the public dispatch and model catalog."""
import asyncio
from types import SimpleNamespace

from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy


def test_custom_decision_provider_reaches_dispatch_and_discovery(monkeypatch):
    import proxy

    registry = {"services": {"custom-decider": {
        "id": "custom-decider", "kind": "provider", "enabled": True,
        "capabilities": ["typesafe-systemone"],
        "config": {
            "remote_provider": True, "provider_id": "custom-decider",
            "protocol": "typesafe-systemone", "base_url": "https://decider.test/v1",
            "api_key_env": "CUSTOM_DECIDER_TEST_KEY",
            "models": [{"id": "custom-decider/selected"},
                       {"id": "custom-decider/disabled", "enabled": False}],
        },
    }}}
    profile = DEFAULT_PROFILE.with_overrides(
        id="decision-plan", remote=RemoteProviderPolicy(providers=frozenset({"custom-decider"})),
    )
    auth = SimpleNamespace(identity=SimpleNamespace(app_id="synthetic-app"))
    monkeypatch.setenv("CUSTOM_DECIDER_TEST_KEY", "synthetic-decision-key")
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(
        get_service_registry=lambda: registry,
        get_provider_admin_state=lambda: {"providers": {"custom-decider": {"allowed_plans": [profile.id]}}},
    ))
    monkeypatch.setattr(proxy, "_profile_for_auth_context", lambda _auth: profile)
    monkeypatch.setattr(proxy, "_profile_target_visibility", lambda _auth: "available")
    seen = []

    async def dispatch(_auth, _profile, body, request_id, **context):
        seen.append((body["model"], request_id, context["registry"]))
        return "remote-dispatch"

    monkeypatch.setattr(proxy.decision_provider_execution, "systemone", dispatch)
    assert asyncio.run(proxy._dispatch_decision(
        auth, profile, {"model": "custom-decider/selected"}, "req-custom",
    )) == "remote-dispatch"
    assert seen == [("custom-decider/selected", "req-custom", registry)]
    models = proxy._remote_decision_models(auth, profile)
    assert [(model.id, model.backend, model.remote) for model in models] == [
        ("custom-decider/selected", "custom-decider", True),
    ]
