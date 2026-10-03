from __future__ import annotations

import socket
import threading
import time
from typing import Any, Mapping

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from gateway.admin_api import AdminAuthError, AdminContext
from gateway.provider_admin import create_provider_admin_router


class TemporaryProviderAdminStore:
    def __init__(self) -> None:
        self.registry = {
            "schema_version": 1,
            "services": {
                "openrouter": {
                    "id": "openrouter",
                    "name": "OpenRouter",
                    "enabled": True,
                    "kind": "provider",
                    "config": {
                        "remote_provider": True,
                        "provider_type": "paid-remote",
                        "api_key_env": "OPENROUTER_API_KEY",
                        "models": [{"model_group": "openrouter/test-model"}],
                    },
                    "secret_refs": {"api_key_env": "OPENROUTER_API_KEY"},
                },
                "typesafe": {
                    "id": "typesafe",
                    "name": "TypeSafe",
                    "enabled": True,
                    "kind": "provider",
                    "config": {
                        "remote_provider": True,
                        "provider_type": "paid-remote",
                        "api_key_env": "TYPESAFE_API_KEY",
                        "protocol": "typesafe-systemone",
                        "models": [{"model_group": "typesafe/jev-latest"}],
                    },
                    "secret_refs": {"api_key_env": "TYPESAFE_API_KEY"},
                },
            },
        }
        self.provider_admin_state = {
            "schema_version": 1,
            "providers": {},
            "global_caps": {},
        }
        self.profiles = [{"id": "default-local", "name": "Default local"}]

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_profiles(self) -> list[dict[str, Any]]:
        return self.profiles

    def get_provider_admin_state(self) -> dict[str, Any]:
        return self.provider_admin_state

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.provider_admin_state = dict(data)
        return self.provider_admin_state


def _admin_auth(request: Request) -> AdminContext:
    if request.headers.get("authorization") != "Bearer test-admin":
        raise AdminAuthError()
    return AdminContext(subject="usr_test", roles=frozenset({"admin"}))


def _find_18xxx_port() -> int:
    for port in range(18000, 19000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free test port in 18000-18999")


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.2) as client:
                response = client.get(f"http://127.0.0.1:{port}/admin/api/providers")
                if response.status_code in {200, 401}:
                    return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise AssertionError("uvicorn test server did not start")


@pytest.mark.parametrize(
    ("provider_id", "env_name"),
    (("openrouter", "OPENROUTER_API_KEY"), ("typesafe", "TYPESAFE_API_KEY")),
)
def test_provider_admin_reports_missing_env_keys_over_real_tcp(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    env_name: str,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    app = FastAPI()
    app.include_router(
        create_provider_admin_router(
            TemporaryProviderAdminStore(),
            auth=_admin_auth,
            spending_provider=lambda: {},
        )
    )

    port = _find_18xxx_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_server(port)
        with httpx.Client(timeout=2) as client:
            response = client.get(
                f"http://127.0.0.1:{port}/admin/api/providers/{provider_id}",
                headers={"authorization": "Bearer test-admin"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"]["key_status"] == {
            "state": "missing",
            "api_key_env": env_name,
        }
        assert env_name in response.text
        assert "sk-" not in response.text
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
