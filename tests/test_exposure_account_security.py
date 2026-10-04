from __future__ import annotations

import ipaddress
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

import proxy
from gateway import admin_store
from gateway.admin_oidc import (
    CookieConfig,
    csrf_token_for_session,
    encode_session_cookie,
    session_from_claims,
)
from gateway.admin_runtime import create_admin_service_account_key
from gateway.local_owner import verify_password as verify_local_password


SIGNING_KEY = b"exposure-account-test-key-32-bytes!"


async def _empty_status_projection(_config):
    return {}


@pytest.fixture
def isolated_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "store", proxy.ADMIN_STORE)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", SIGNING_KEY)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", CookieConfig(secure=False))
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
    monkeypatch.setattr(
        proxy,
        "LAN_BYPASS_CIDRS",
        [ipaddress.ip_network("192.168.0.0/16")],
    )
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    monkeypatch.setattr(proxy, "_CF_IP_NETWORKS", None)
    monkeypatch.setattr(
        proxy.status_endpoint,
        "get_cached_status",
        _empty_status_projection,
    )
    return path


def _owner_session_cookie(path: Path) -> tuple[str, str, str]:
    user = admin_store.upsert_oidc_user(
        "local",
        "owner@example.test",
        email="owner@example.test",
        path=path,
    )
    admin_store.assign_admin_role(
        role="owner",
        subject_kind="user",
        subject_id=str(user["id"]),
        granted_by="test",
        path=path,
    )
    session = session_from_claims(
        {"sub": str(user["id"]), "email": "owner@example.test"},
        role="owner",
        cookie=proxy.ADMIN_AUTH.cookie,
    )
    cookie = encode_session_cookie(session, signing_key=SIGNING_KEY)
    csrf = csrf_token_for_session(session, signing_key=SIGNING_KEY)
    return proxy.ADMIN_AUTH.cookie.name, cookie, csrf


def test_setup_local_owner_refuses_public_first_claim_without_side_effects(
    isolated_admin: Path,
) -> None:
    client = TestClient(proxy.app, client=("203.0.113.10", 50000))

    response = client.post(
        "/setup/local-owner",
        data={
            "email": "attacker@example.test",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert admin_store.list_users(path=isolated_admin) == []
    assert admin_store.list_admin_roles(path=isolated_admin) == []


def test_setup_local_owner_accepts_explicit_loopback_first_claim(
    isolated_admin: Path,
) -> None:
    client = TestClient(proxy.app, client=("127.0.0.1", 50000))

    response = client.post(
        "/setup/local-owner",
        data={
            "email": "owner@example.test",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"
    assert len(admin_store.list_users(path=isolated_admin)) == 1


def test_setup_local_owner_first_claim_is_atomic_under_race(
    isolated_admin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    original_hash = proxy.local_hash_password

    def synchronized_hash(password: str) -> str:
        verifier = original_hash(password)
        barrier.wait(timeout=10)
        return verifier

    monkeypatch.setattr(proxy, "local_hash_password", synchronized_hash)

    def claim(email: str, password: str):
        client = TestClient(proxy.app, client=("127.0.0.1", 50000))
        return client.post(
            "/setup/local-owner",
            data={
                "email": email,
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            claim,
            "owner-one@example.test",
            "correct-horse-battery-one",
        )
        second = executor.submit(
            claim,
            "owner-two@example.test",
            "correct-horse-battery-two",
        )
        responses = [first.result(timeout=20), second.result(timeout=20)]

    assert sorted(response.status_code for response in responses) == [303, 303]
    users = admin_store.list_users(path=isolated_admin)
    roles = admin_store.list_admin_roles(path=isolated_admin)
    assert len(users) == 1
    assert len(roles) == 1
    assert roles[0]["role"] == "owner"
    assert roles[0]["subject_id"] == users[0]["id"]
    assert users[0]["email"] in {"owner-one@example.test", "owner-two@example.test"}
    verifier = admin_store.get_local_password_verifier(
        users[0]["id"],
        path=isolated_admin,
    )
    assert verifier is not None
    password_by_email = {
        "owner-one@example.test": "correct-horse-battery-one",
        "owner-two@example.test": "correct-horse-battery-two",
    }
    assert verify_local_password(
        password_by_email[users[0]["email"]],
        verifier["verifier"],
    )


def test_admin_config_reload_cookie_session_requires_valid_csrf(
    isolated_admin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"reload": 0}

    def fake_reload_runtime_config():
        calls["reload"] += 1
        return {
            "LLM_BACKEND_HOSTS": {},
            "TABBY_HOST": "",
            "KOBOLD_HOST": "",
            "TTS_HOST": "",
            "STT_HOST": "",
            "EMBEDDINGS_HOST": "",
            "COMFY_HOST": "",
        }

    monkeypatch.setattr(proxy, "reload_runtime_config", fake_reload_runtime_config)
    cookie_name, cookie_value, csrf = _owner_session_cookie(isolated_admin)
    client = TestClient(proxy.app, client=("127.0.0.1", 50000))
    client.cookies.set(cookie_name, cookie_value)

    missing = client.post("/admin/api/config/reload")
    assert missing.status_code == 403, missing.text
    assert missing.json()["error"]["code"] == "admin_csrf_required"

    wrong = client.post(
        "/admin/api/config/reload",
        headers={"x-ai-gateway-csrf": "wrong"},
    )
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["error"]["code"] == "admin_csrf_required"
    assert calls["reload"] == 0

    accepted = client.post(
        "/admin/api/config/reload",
        headers={"x-ai-gateway-csrf": csrf},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reloaded"] is True
    assert calls["reload"] == 1


def test_admin_config_reload_bearer_admin_does_not_require_csrf(
    isolated_admin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"reload": 0}

    def fake_reload_runtime_config():
        calls["reload"] += 1
        return {
            "LLM_BACKEND_HOSTS": {},
            "TABBY_HOST": "",
            "KOBOLD_HOST": "",
            "TTS_HOST": "",
            "STT_HOST": "",
            "EMBEDDINGS_HOST": "",
            "COMFY_HOST": "",
        }

    monkeypatch.setattr(proxy, "reload_runtime_config", fake_reload_runtime_config)
    admin_key, _, _ = create_admin_service_account_key("owner", path=isolated_admin)
    client = TestClient(proxy.app, client=("127.0.0.1", 50000))

    accepted = client.post(
        "/admin/api/config/reload",
        headers={"authorization": f"Bearer {admin_key}"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reloaded"] is True
    assert calls["reload"] == 1
