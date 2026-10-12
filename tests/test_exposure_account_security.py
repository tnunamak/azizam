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
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "store", proxy.ADMIN_STORE)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", SIGNING_KEY)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", CookieConfig(secure=False))
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
    monkeypatch.setattr(proxy, "SETUP_ALLOWED_CIDRS", None)
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


@pytest.mark.parametrize("peer,configured,status", [
    ("172.30.251.1", False, 403),
    ("172.30.251.1", True, 303),
    ("172.30.251.2", True, 403),
    ("203.0.113.10", True, 403),
])
def test_setup_exact_container_host_allowance(isolated_admin, monkeypatch, peer, configured, status):
    monkeypatch.setattr(proxy, "LAN_BYPASS_CIDRS", [])
    monkeypatch.setattr(proxy, "SETUP_ALLOWED_CIDRS", [ipaddress.ip_network("172.30.251.1/32")] if configured else None)
    client = TestClient(proxy.app, base_url="http://localhost", client=(peer, 50000))
    response = client.post("/setup/local-owner", data={
        "email": "owner@example.test", "password": "Container-owner-test", "password_confirm": "Container-owner-test",
    }, follow_redirects=False)
    assert response.status_code == status
    assert len(admin_store.list_users(path=isolated_admin)) == (1 if status == 303 else 0)


@pytest.mark.parametrize("forwarded", [None, "", "garbage", "203.0.113.10"])
def test_setup_container_host_used_as_proxy_cannot_hide_public_client(isolated_admin, monkeypatch, forwarded):
    bridge = ipaddress.ip_network("172.30.251.1/32")
    monkeypatch.setattr(proxy, "SETUP_ALLOWED_CIDRS", [bridge])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [bridge])
    client = TestClient(proxy.app, base_url="http://localhost", client=("172.30.251.1", 50000))
    response = client.post("/setup/local-owner", headers={} if forwarded is None else {"x-forwarded-for": forwarded}, data={
        "email": "attacker@example.test", "password": "Container-owner-test", "password_confirm": "Container-owner-test",
    }, follow_redirects=False)
    assert response.status_code == 403
    assert admin_store.list_users(path=isolated_admin) == []


def test_compose_setup_allowance_matches_defined_gateway():
    import yaml
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())
    gateway = compose["networks"]["default"]["ipam"]["config"][0]["gateway"]
    assert compose["services"]["gateway"]["environment"]["AZIZAM_SETUP_ALLOWED_CIDRS"] == gateway + "/32"
    assert compose["services"]["gateway"]["ports"][0].startswith("127.0.0.1:")
    assert "--no-proxy-headers" in compose["services"]["gateway"]["command"]


def test_compose_keeps_data_and_creations_in_named_volumes():
    import yaml
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())
    mounts = {m.split(":")[1]: m.split(":")[0] for m in compose["services"]["gateway"]["volumes"]}
    assert mounts["/app/data"] == "gateway-data"
    assert mounts["/app/cache"] == "gateway-cache"
    assert {"gateway-data", "gateway-cache"} <= set(compose["volumes"])


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


def _first_owner_client(peer: str) -> TestClient:
    return TestClient(proxy.app, base_url="http://localhost", client=(peer, 50000))


def test_setup_local_owner_refuses_public_first_claim_without_side_effects(
    isolated_admin: Path,
) -> None:
    client = _first_owner_client("203.0.113.10")

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
    client = _first_owner_client("127.0.0.1")

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


def test_setup_local_owner_accepts_direct_lan_first_claim(
    isolated_admin: Path,
) -> None:
    client = _first_owner_client("192.168.1.25")

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
    assert len(admin_store.list_users(path=isolated_admin)) == 1


def test_setup_local_owner_refuses_forwarded_public_client_from_trusted_lan_proxy(
    isolated_admin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "TRUSTED_PROXIES",
        [ipaddress.ip_network("192.168.1.4/32")],
    )
    client = _first_owner_client("192.168.1.4")

    response = client.post(
        "/setup/local-owner",
        headers={"x-forwarded-for": "203.0.113.10"},
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


def test_setup_local_owner_refuses_cross_origin_lan_post_without_side_effects(
    isolated_admin: Path,
) -> None:
    admin_store.put_operator_setting(
        "instance_presentation",
        {"base_url": "https://openai.vivid.fish"},
        path=isolated_admin,
    )
    client = TestClient(
        proxy.app,
        base_url="https://openai.vivid.fish",
        client=("192.168.1.25", 50000),
    )

    response = client.post(
        "/setup/local-owner",
        headers={"origin": "https://evil.example"},
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


def test_setup_local_owner_refuses_lan_dns_rebinding_host_before_writes(
    isolated_admin: Path,
) -> None:
    admin_store.put_operator_setting(
        "instance_presentation",
        {"base_url": "https://openai.vivid.fish"},
        path=isolated_admin,
    )
    client = TestClient(
        proxy.app,
        base_url="https://evil.example",
        client=("192.168.1.25", 50000),
    )

    response = client.post(
        "/setup/local-owner",
        headers={"origin": "https://evil.example"},
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
        client = _first_owner_client("127.0.0.1")
        response = client.post(
            "/setup/local-owner",
            data={
                "email": email,
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )
        return email, password, response

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
        results = [first.result(timeout=20), second.result(timeout=20)]

    responses = [response for _, _, response in results]
    assert sorted(response.status_code for response in responses) == [303, 303]
    winners = [
        (email, password, response)
        for email, password, response in results
        if proxy.ADMIN_AUTH.cookie.name in response.headers.get("set-cookie", "")
    ]
    assert len(winners) == 1
    winner_email, winner_password, winner_response = winners[0]
    assert winner_response.headers["location"] == "/"
    losers = [response for _, _, response in results if response is not winner_response]
    assert len(losers) == 1
    assert losers[0].headers["location"] == "/setup/"
    assert "set-cookie" not in losers[0].headers

    users = admin_store.list_users(path=isolated_admin)
    roles = admin_store.list_admin_roles(path=isolated_admin)
    assert len(users) == 1
    assert len(roles) == 1
    assert roles[0]["role"] == "owner"
    assert roles[0]["subject_id"] == users[0]["id"]
    assert users[0]["email"] == winner_email
    verifier = admin_store.get_local_password_verifier(
        users[0]["id"],
        path=isolated_admin,
    )
    assert verifier is not None
    assert verify_local_password(winner_password, verifier["verifier"])


def test_setup_local_owner_same_email_race_preserves_winner_verifier(
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

    def claim(password: str):
        client = _first_owner_client("127.0.0.1")
        response = client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.test",
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )
        return password, response

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "correct-horse-battery-one")
        second = executor.submit(claim, "correct-horse-battery-two")
        results = [first.result(timeout=20), second.result(timeout=20)]

    responses = [response for _, response in results]
    assert sorted(response.status_code for response in responses) == [303, 303]
    winners = [
        (password, response)
        for password, response in results
        if proxy.ADMIN_AUTH.cookie.name in response.headers.get("set-cookie", "")
    ]
    assert len(winners) == 1
    winner_password, winner_response = winners[0]
    assert winner_response.headers["location"] == "/"
    losers = [response for _, response in results if response is not winner_response]
    assert len(losers) == 1
    assert losers[0].headers["location"] == "/setup/"
    assert "set-cookie" not in losers[0].headers

    users = admin_store.list_users(path=isolated_admin)
    roles = admin_store.list_admin_roles(path=isolated_admin)
    assert len(users) == 1
    assert len(roles) == 1
    verifier = admin_store.get_local_password_verifier(
        users[0]["id"],
        path=isolated_admin,
    )
    assert verifier is not None
    assert verify_local_password(winner_password, verifier["verifier"])
    loser_passwords = [
        password for password, response in results if response is not winner_response
    ]
    assert len(loser_passwords) == 1
    assert not verify_local_password(loser_passwords[0], verifier["verifier"])


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
    client = _first_owner_client("127.0.0.1")
    client.cookies.set(cookie_name, cookie_value)

    missing = client.post("/admin/api/config/reload")
    assert missing.status_code == 403, missing.text
    assert missing.json()["error"]["code"] == "admin_csrf_required"

    wrong = client.post(
        "/admin/api/config/reload",
        headers={"x-azizam-csrf": "wrong"},
    )
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["error"]["code"] == "admin_csrf_required"
    assert calls["reload"] == 0

    accepted = client.post(
        "/admin/api/config/reload",
        headers={"x-azizam-csrf": csrf},
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
    client = _first_owner_client("127.0.0.1")

    accepted = client.post(
        "/admin/api/config/reload",
        headers={"authorization": f"Bearer {admin_key}"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reloaded"] is True
    assert calls["reload"] == 1
