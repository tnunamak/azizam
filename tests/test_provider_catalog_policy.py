"""Member discovery applies the same provider identity and plan restriction."""
from types import SimpleNamespace

import pytest

from gateway.capabilities import Capability
from tests.support.access import access, access_context, plan_for  # noqa: E402


@pytest.mark.parametrize(("person_lists", "app_lists", "visible"), [
    (True, True, True),
    (False, True, False),
    (True, False, False),
    (False, False, False),
])
def test_catalog_uses_canonical_provider_and_both_plans(
    monkeypatch: pytest.MonkeyPatch, person_lists: bool, app_lists: bool, visible: bool,
) -> None:
    import proxy
    registry = {"services": {"legacy-row-id": {
        "id": "legacy-row-id", "kind": "provider", "enabled": True,
        "config": {
            "remote_provider": True, "provider_id": "cloud-canonical",
            "protocol": "openai-compatible", "api_key_env": "TEST_PROVIDER_KEY",
            "base_url": "http://127.0.0.1:18395/v1",
            "credential_origin": "http://127.0.0.1:18395",
        },
    }}}
    person = plan_for("person-plan", paid=["cloud-canonical"] if person_lists else [])
    app = plan_for("app-plan", paid=["cloud-canonical"] if app_lists else [])
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(get_service_registry=lambda: registry))
    cap = Capability(intent="chat.completions", backend="legacy-row-id", model="cloud-canonical/test", tags=("remote",))
    projected = proxy._capability_for_access(cap, access_context(access(person, app)))
    assert projected.policy_available is visible
    assert projected.backend == "legacy-row-id"


def test_no_identity_sees_no_remote_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import proxy
    registry = {"services": {"cloud": {
        "id": "cloud", "kind": "provider", "enabled": True,
        "config": {"remote_provider": True, "protocol": "openai-compatible", "api_key_env": "K",
                   "base_url": "http://127.0.0.1:18395/v1", "credential_origin": "http://127.0.0.1:18395"},
    }}}
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(get_service_registry=lambda: registry))
    cap = Capability(intent="chat.completions", backend="cloud", model="cloud/test", tags=("remote",))
    assert proxy._capability_for_access(cap, proxy.AuthContext()).policy_available is False
