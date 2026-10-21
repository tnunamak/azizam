from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
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
                        "base_url": "https://openrouter.ai/api/v1",
                        "credential_origin": "https://openrouter.ai:443",
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
                        "base_url": "https://api.typesafe.ai/v1",
                        "credential_origin": "https://api.typesafe.ai:443",
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
        self.access_plans = [{"id": "plan_local", "name": "Local only", "plan": {"paid_providers": []}}]

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_access_plans(self) -> list[dict[str, Any]]:
        return self.access_plans

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


@pytest.mark.parametrize(
    ("provider_id", "env_name"),
    (("openrouter", "OPENROUTER_API_KEY"), ("typesafe", "TYPESAFE_API_KEY")),
)
def test_provider_admin_reports_missing_env_keys_in_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            ledger_path=tmp_path / "spend.db",
            receipts_path=tmp_path / "receipts.db",
        )
    )

    async def fetch() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(
                f"/admin/api/providers/{provider_id}",
                headers={"authorization": "Bearer test-admin"},
            )

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert response.json()["provider"]["key_status"] == {
        "state": "missing",
        "source": "env",
        "api_key_env": env_name,
    }
    assert env_name in response.text
    assert "sk-" not in response.text
