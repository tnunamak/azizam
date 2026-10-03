"""Member discovery applies the same provider identity and plan restriction."""
from types import SimpleNamespace

import pytest

from gateway.capabilities import Capability
from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy


@pytest.mark.parametrize(("granted", "allowed_plans", "visible"), [
    (True, ["member-plan"], True),
    (False, ["member-plan"], False),
    (True, [], False),
    (True, ["another-plan"], False),
])
def test_catalog_uses_canonical_provider_and_both_plan_gates(
    monkeypatch: pytest.MonkeyPatch, granted: bool, allowed_plans: list[str], visible: bool,
) -> None:
    import proxy
    registry = {"services": {"legacy-row-id": {
        "id": "legacy-row-id", "kind": "provider", "enabled": True,
        "config": {
            "remote_provider": True, "provider_id": "cloud-canonical",
            "protocol": "openai-compatible", "api_key_env": "TEST_PROVIDER_KEY",
            "base_url": "http://127.0.0.1:18395/v1",
        },
    }}}
    profile = DEFAULT_PROFILE.with_overrides(
        id="member-plan",
        remote=RemoteProviderPolicy(providers=frozenset({"cloud-canonical"} if granted else set())),
    )
    monkeypatch.setattr(proxy, "_profile_for_auth_context", lambda _auth: profile)
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(
        get_service_registry=lambda: registry,
        get_provider_admin_state=lambda: {"providers": {"cloud-canonical": {"allowed_plans": allowed_plans}}},
    ))
    cap = Capability(intent="chat.completions", backend="legacy-row-id", model="cloud-canonical/test", tags=("remote",))
    projected = proxy._capability_for_profile(cap, object())
    assert projected.policy_available is visible
    assert projected.backend == "legacy-row-id"
