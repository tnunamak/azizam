"""Trusted addresses: a keyless request from an owner-trusted address runs as one App.

Real key lookup and plan evaluation in the test-owned admin.db, no stubbed identity.
"""

from __future__ import annotations

import ipaddress
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import accounts, admin_store, receipts, trusted_addresses  # noqa: E402
from gateway.access_plans import ALL, CAPABILITIES  # noqa: E402
from gateway.runtime_auth import hash_api_key  # noqa: E402

PLAN = {
    "name": "Local only",
    "can_use": {capability: {"items": ALL} for capability in CAPABILITIES},
    "direct_engine_access": {"engines": [], "operations": []},
    "paid_providers": [],
    "provider_fallback": False,
    "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
    "can_load_models": True,
    "receipt_detail": "basic",
    "media_metadata": "none",
}
CHAT = "/v1/chat/completions"
BODY = {"model": "default", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}


@pytest.fixture
def store():
    path = proxy.ADMIN_STORE.path
    admin_store.init_db(path)
    accounts.initialize(path)
    conn = admin_store.sqlite3.connect(path)
    conn.execute("DELETE FROM app_trusted_addresses")  # the test database outlives each test
    conn.commit()
    conn.close()
    return path


def _plan(store, **overrides):
    return admin_store.create_access_plan({**PLAN, "name": f"plan {uuid.uuid4().hex[:6]}", **overrides}, path=store)["id"]


def _person(store):
    user = admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email="p@example.test",
                                        display_name="Person", path=store)
    accounts.change_person(user["id"], "test", plan_id=_plan(store), path=store)
    return user["id"]


def _trusted_app(store, address, *, access_plan=None, name="Living room display"):
    person = _person(store)
    app = admin_store.create_app(name, kind="service", owner_user_id=person,
                                 access_plan=access_plan or "person", path=store)
    entry = None
    if address:
        entry = admin_store.add_trusted_address(app["id"], trusted_addresses.normalize_entry(address),
                                                max_entries=32, path=store)
    return app["id"], entry


def _post(peer, headers=None, trusted_proxies=()):
    client = TestClient(proxy.app, client=(peer, 4242))
    return client.post(CHAT, json=BODY, headers=headers or {})


def _code(response):
    return response.json().get("error", {}).get("code")


def _reaches_plan(response):
    """Past authentication: whatever the plan then says, it is not a key error."""
    return _code(response) not in {"key_required", "key_invalid", "app_disabled", "person_disabled"}


# --- entry syntax and matching rules -------------------------------------------------


@pytest.mark.parametrize("text, stored", [
    ("192.168.1.5", "192.168.1.5"),
    ("192.168.1.0/24", "192.168.1.0/24"),
    ("192.168.1.5/32", "192.168.1.5"),
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("fd00::/8", "fd00::/8"),
    ("  10.0.0.0/8 ", "10.0.0.0/8"),
])
def test_entry_spelling(text, stored):
    assert trusted_addresses.normalize_entry(text) == stored


@pytest.mark.parametrize("text", ["", "   ", "0.0.0.0/0", "::/0", "192.168.1.7/24", "not-an-ip", "10.0.0.1, 10.0.0.2",
                                  "10.0.0.1 10.0.0.2", "::ffff:10.0.0.1", "256.1.1.1", "10.0.0.0/33"])
def test_entry_refusals(text):
    with pytest.raises(trusted_addresses.AddressError):
        trusted_addresses.normalize_entry(text)


def _entry(address, created_at=1):
    return {"address": address, "created_at": created_at, "id": address}


@pytest.mark.parametrize("client, entries, expected", [
    ("192.168.1.5", ["192.168.1.5"], "192.168.1.5"),
    ("192.168.1.6", ["192.168.1.5"], None),
    ("192.168.1.200", ["192.168.1.0/24"], "192.168.1.0/24"),
    ("192.168.2.1", ["192.168.1.0/24"], None),
    ("127.0.0.1", ["127.0.0.1"], "127.0.0.1"),
    ("::1", ["::1"], "::1"),
    ("::1", ["127.0.0.1"], None),
    ("127.0.0.1", ["::1"], None),
    ("fd00::5", ["fd00::/8"], "fd00::/8"),
    ("::ffff:192.168.1.5", ["192.168.1.0/24"], "192.168.1.0/24"),
    ("192.168.1.5", ["192.168.1.0/24", "192.168.1.5", "192.168.0.0/16"], "192.168.1.5"),
])
def test_matching_rules(client, entries, expected):
    winner = trusted_addresses.best_match(ipaddress.ip_address(client), [_entry(a) for a in entries])
    assert (winner["address"] if winner else None) == expected


def test_equal_ranges_go_to_the_older_entry():
    older, newer = _entry("10.0.0.0/8", 1), {**_entry("10.0.0.0/8", 2), "id": "newer"}
    assert trusted_addresses.best_match(ipaddress.ip_address("10.1.1.1"), [newer, older]) is older


def test_an_unreadable_row_trusts_nobody():
    assert trusted_addresses.best_match(ipaddress.ip_address("10.1.1.1"), [_entry("garbage")]) is None


# --- deny by default -----------------------------------------------------------------


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "192.168.1.50", "203.0.113.9"])
def test_no_entry_means_key_required(store, peer):
    response = _post(peer)
    assert response.status_code == 401 and _code(response) == "key_required"


def test_a_fresh_install_has_no_entries(store):
    assert admin_store.list_trusted_addresses(path=store) == []


def test_an_entry_does_not_open_other_addresses(store):
    _trusted_app(store, "192.168.1.50")
    for peer in ("192.168.1.51", "127.0.0.1", "203.0.113.9"):
        assert _code(_post(peer)) == "key_required"


def test_a_trusted_address_runs_as_its_app(store):
    _trusted_app(store, "192.168.1.0/24")
    response = _post("192.168.1.50")
    assert _reaches_plan(response)


# --- precedence of keys --------------------------------------------------------------


def test_a_sent_key_decides_even_from_a_trusted_address(store):
    _trusted_app(store, "192.168.1.50")
    response = _post("192.168.1.50", headers={"Authorization": "Bearer opk_not_a_real_key"})
    assert response.status_code == 401 and _code(response) == "key_invalid"


def test_a_real_key_runs_as_its_own_app_not_the_trusted_one(store):
    trusted_id, _ = _trusted_app(store, "192.168.1.50", name="Display")
    person = _person(store)
    app = admin_store.create_app("Keyed", kind="service", owner_user_id=person, access_plan="person", path=store)
    raw = f"opk_{uuid.uuid4().hex}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key(raw),
                                       prefix=raw[:18], created_by=person, path=store)
    identity = proxy.runtime_identity_for_app_credential_hash(hash_api_key(raw), path=store)
    assert identity.app_id == app["id"] != trusted_id
    assert _reaches_plan(_post("192.168.1.50", headers={"Authorization": f"Bearer {raw}"}))


# --- proxy headers -------------------------------------------------------------------


def test_a_spoofed_forwarded_header_from_an_ordinary_peer_is_refused(store):
    _trusted_app(store, "192.168.1.50")
    response = _post("203.0.113.9", headers={"X-Forwarded-For": "192.168.1.50"})
    assert _code(response) == "key_required"


@pytest.mark.parametrize("header", [("X-Forwarded-For", "203.0.113.9"), ("X-Real-IP", "203.0.113.9"),
                                    ("Forwarded", "for=203.0.113.9"), ("X-Forwarded-Proto", "https"),
                                    ("CF-Connecting-IP", "203.0.113.9"), ("Via", "1.1 proxy")])
def test_public_traffic_through_a_loopback_proxy_never_matches(store, header):
    """The public reverse proxy connects from loopback and adds forwarding headers."""
    _trusted_app(store, "127.0.0.1")
    response = _post("127.0.0.1", headers={header[0]: header[1]})
    assert _code(response) == "key_required"


def test_loopback_without_forwarding_headers_matches(store):
    _trusted_app(store, "127.0.0.1")
    assert _reaches_plan(_post("127.0.0.1"))


def test_forwarded_address_counts_only_from_a_configured_trusted_proxy(store, monkeypatch):
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("10.9.9.9/32")])
    _trusted_app(store, "192.168.1.50")
    assert _reaches_plan(_post("10.9.9.9", headers={"X-Forwarded-For": "192.168.1.50"}))
    # The same header from a peer that is not the trusted proxy is refused.
    assert _code(_post("10.9.9.8", headers={"X-Forwarded-For": "192.168.1.50"})) == "key_required"


def test_a_client_supplied_forwarded_entry_cannot_be_used_behind_a_trusted_proxy(store, monkeypatch):
    """The proxy appends the real peer; a client-written leading entry is ignored."""
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("10.9.9.9/32")])
    _trusted_app(store, "192.168.1.50")
    response = _post("10.9.9.9", headers={"X-Forwarded-For": "192.168.1.50, 203.0.113.9"})
    assert _code(response) == "key_required"


def test_a_trusted_proxy_forwarding_an_internet_client_never_matches_a_private_entry(store, monkeypatch):
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("127.0.0.1/32")])
    _trusted_app(store, "127.0.0.1")
    response = _post("127.0.0.1", headers={"X-Forwarded-For": "203.0.113.9"})
    assert _code(response) == "key_required"


def test_a_trusted_proxy_with_a_malformed_forwarded_header_matches_nothing(store, monkeypatch):
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("127.0.0.1/32")])
    _trusted_app(store, "127.0.0.1")
    assert _code(_post("127.0.0.1", headers={"X-Forwarded-For": "not-an-ip"})) == "key_required"


# --- plan limits, disabling, receipts ------------------------------------------------


def test_the_apps_access_plan_limits_what_runs(store):
    embed_only = _plan(store, can_use={"embed": {"items": ALL}})
    _trusted_app(store, "192.168.1.50", access_plan=embed_only)
    response = _post("192.168.1.50")
    assert response.status_code == 403 and _code(response) == "not_in_access_plan"
    assert response.json()["error"]["details"]["side"] == "app"


def test_an_app_without_a_plan_assignment_is_denied(store):
    app_id, _ = _trusted_app(store, "192.168.1.50")
    conn = admin_store.sqlite3.connect(store)
    conn.execute("UPDATE apps SET access_plan = NULL WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()
    assert _code(_post("192.168.1.50")) == "no_access_plan"


def test_disabling_the_app_stops_it_and_enabling_restores_it(store):
    app_id, _ = _trusted_app(store, "192.168.1.50")
    assert _reaches_plan(_post("192.168.1.50"))
    admin_store.disable_app(app_id, path=store)
    response = _post("192.168.1.50")
    assert response.status_code == 403 and _code(response) == "app_disabled"
    admin_store.enable_app(app_id, path=store)
    assert _reaches_plan(_post("192.168.1.50"))


def test_removing_the_entry_stops_it_at_once(store):
    app_id, entry = _trusted_app(store, "192.168.1.50")
    assert _reaches_plan(_post("192.168.1.50"))
    assert admin_store.delete_trusted_address(app_id, entry["id"], path=store)
    assert _code(_post("192.168.1.50")) == "key_required"


def test_a_disabled_person_stops_the_apps_trusted_address(store):
    app_id, _ = _trusted_app(store, "192.168.1.50")
    owner = admin_store.get_app(app_id, path=store)["owner_user_id"]
    accounts.change_person(owner, "test", disabled=True, path=store)
    assert _code(_post("192.168.1.50")) == "person_disabled"


def test_a_trusted_address_has_no_saved_prompts_or_library(store):
    app_id, _ = _trusted_app(store, "192.168.1.50")
    identity = proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("192.168.1.50"), path=store)
    assert identity.app_id == app_id and identity.person_id is None and identity.accountable_person_id


def test_the_narrowest_entry_wins_across_apps(store):
    wide_id, _ = _trusted_app(store, "192.168.1.0/24", name="Wide")
    narrow_id, _ = _trusted_app(store, "192.168.1.50", name="Narrow")
    identity = proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("192.168.1.50"), path=store)
    assert identity.app_id == narrow_id
    assert proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("192.168.1.51"), path=store).app_id == wide_id


def test_a_session_app_cannot_be_trusted_by_address(store):
    person = _person(store)
    session_app = admin_store.get_session_app_for_user(person, path=store)
    if session_app is None:
        session_app = admin_store.ensure_session_app_for_user(person, name="Azizam", path=store)
    with pytest.raises(ValueError):
        admin_store.add_trusted_address(session_app["id"], "192.168.1.50", max_entries=32, path=store)


def test_receipts_carry_the_app_and_the_address(store):
    app_id, _ = _trusted_app(store, "192.168.1.50", name="Display")
    seen = {}
    original = receipts.RouteReceipt.to_dict

    def spy(self, audience="internal"):
        data = original(self, audience)
        if self.app_id == app_id:
            seen.update(client_address=self.client_address, api_key_name=self.api_key_name)
        return data

    receipts.RouteReceipt.to_dict = spy
    try:
        response = _post("192.168.1.50")
    finally:
        receipts.RouteReceipt.to_dict = original
    assert _reaches_plan(response)
    assert seen == {"client_address": "192.168.1.50", "api_key_name": "Display"}


# --- admin API -----------------------------------------------------------------------


def _admin_client(tmp_path, roles=("owner",)):
    from fastapi import FastAPI
    from gateway.admin_api import AdminContext, create_admin_router
    from gateway.admin_runtime import AdminSQLiteStore

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    person = admin_store.upsert_oidc_user("https://sso.test", uuid.uuid4().hex, email="o@example.test",
                                          display_name="Owner", path=path)
    accounts.change_person(person["id"], "test", plan_id=admin_store.create_access_plan(
        {**PLAN, "name": "p"}, path=path)["id"], path=path)
    app = admin_store.create_app("Display", kind="service", owner_user_id=person["id"], access_plan="person", path=path)
    api = FastAPI()
    api.include_router(create_admin_router(
        AdminSQLiteStore(path),
        auth=lambda request: AdminContext(subject="u", roles=frozenset(roles), auth_method="bearer"),
    ))
    return TestClient(api), app["id"], path


def test_admin_api_adds_lists_and_removes_an_entry(tmp_path):
    client, app_id, path = _admin_client(tmp_path)
    url = f"/admin/api/apps/{app_id}/trusted-addresses"
    assert client.get(url).json() == {"trusted_addresses": []}

    created = client.post(url, json={"address": " 192.168.1.0/24 ", "note": "Living room"})
    assert created.status_code == 201, created.text
    entry = created.json()["trusted_address"]
    assert entry["address"] == "192.168.1.0/24" and entry["note"] == "Living room" and entry["app_id"] == app_id
    assert [e["id"] for e in client.get(url).json()["trusted_addresses"]] == [entry["id"]]
    assert {e["action"] for e in admin_store.list_audit(limit=10, path=path)} >= {"app.trusted_address.add"}

    assert client.delete(f"{url}/{entry['id']}").status_code == 200
    assert client.get(url).json() == {"trusted_addresses": []}
    assert client.delete(f"{url}/{entry['id']}").status_code == 404
    assert {e["action"] for e in admin_store.list_audit(limit=10, path=path)} >= {"app.trusted_address.remove"}


@pytest.mark.parametrize("address", ["", "0.0.0.0/0", "nonsense", "10.0.0.7/24"])
def test_admin_api_refuses_bad_addresses(tmp_path, address):
    client, app_id, _ = _admin_client(tmp_path)
    response = client.post(f"/admin/api/apps/{app_id}/trusted-addresses", json={"address": address})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_trusted_address"


def test_admin_api_refuses_a_duplicate_and_an_unknown_app(tmp_path):
    client, app_id, _ = _admin_client(tmp_path)
    url = f"/admin/api/apps/{app_id}/trusted-addresses"
    assert client.post(url, json={"address": "10.0.0.1"}).status_code == 201
    assert client.post(url, json={"address": "10.0.0.1"}).status_code == 400
    assert client.post("/admin/api/apps/app_missing/trusted-addresses", json={"address": "10.0.0.1"}).status_code == 404
    assert client.get("/admin/api/apps/app_missing/trusted-addresses").status_code == 404


def test_only_an_owner_can_trust_an_address(tmp_path):
    client, app_id, path = _admin_client(tmp_path, roles=("admin",))
    response = client.post(f"/admin/api/apps/{app_id}/trusted-addresses", json={"address": "10.0.0.1"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "owner_required"
    assert admin_store.list_trusted_addresses(path=path) == []


def test_an_app_holds_a_bounded_number_of_entries(tmp_path):
    client, app_id, _ = _admin_client(tmp_path)
    url = f"/admin/api/apps/{app_id}/trusted-addresses"
    for index in range(trusted_addresses.MAX_ENTRIES_PER_APP):
        assert client.post(url, json={"address": f"10.0.0.{index}"}).status_code == 201
    assert client.post(url, json={"address": "10.0.1.1"}).status_code == 400
