"""Step-1 identity gate: real auth/store builders, with test-owned state only."""
from ipaddress import ip_address
from dataclasses import replace
from unittest.mock import patch

import proxy
from tests.support.app_keys import accountable_person
from gateway import accounts, admin_runtime, admin_store, me_media
from gateway.content_access import ChatRecord, ContentOrigin, CreationRecord, Deny, Own, Viewer, decide
from gateway.runtime_auth import hash_api_key
from proxy import AuthContext
from starlette.requests import Request
from tests.support.access import store_plan
from tests.test_mcp_oauth import RESOURCE, World


def test_viewers_from_real_session_app_key_and_trusted_address_auth(tmp_path):
    from gateway.content_access import viewer_from_auth
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    accounts.initialize(db)
    person = admin_store.upsert_oidc_user("https://idp.test", "member-a", email="a@example.test", path=db)["id"]
    plan = store_plan("Local", path=db)
    accounts.change_person(person, "test", plan_id=plan["id"], path=db)
    personal = admin_store.create_app("Personal", kind="personal", owner_user_id=person, access_plan="person", path=db)
    service = admin_store.create_app("Service", kind="service", owner_user_id=person, access_plan="person", path=db)
    session_app = admin_store.ensure_session_app_for_user(person, name="Browser", path=db)
    credential_hash = hash_api_key("synthetic-viewer-key")
    cred = admin_store.create_credential_hash(subject_kind="app", subject_id=personal["id"], hash=credential_hash,
                                               prefix="synthetic", created_by=person, path=db)
    identity = admin_runtime.runtime_identity_for_app_credential_hash(credential_hash, path=db)
    key = viewer_from_auth(AuthContext(identity=identity), path=db)
    assert key.auth_kind == "app_key"
    assert (key.person_id, key.app_id, key.credential_id) == (person, personal["id"], cred["id"])
    assert f"app:{service['id']}" not in key.owner_keys
    service_hash = hash_api_key("synthetic-service-viewer-key")
    service_cred = admin_store.create_credential_hash(subject_kind="app", subject_id=service["id"], hash=service_hash,
                                                     prefix="synthetic", created_by=person, path=db)
    service_identity = admin_runtime.runtime_identity_for_app_credential_hash(service_hash, path=db)
    service_viewer = viewer_from_auth(AuthContext(identity=service_identity), path=db)
    assert (service_viewer.auth_kind, service_viewer.credential_id, service_viewer.person_id) == ("service_key", service_cred["id"], None)
    assert service_viewer.app_id == service["id"]
    assert isinstance(decide(service_viewer, ChatRecord(None, service["id"]), "read"), Own)
    browser_identity = admin_runtime.runtime_identity_for_session_app(session_app["id"], person, path=db)
    scope = me_media._member_media_scope(browser_identity.person_id, db)
    browser = viewer_from_auth(browser_identity, path=db, owner_keys=scope.owner_keys)
    assert browser.auth_kind == "session"
    for app in (personal, service, session_app):
        assert f"app:{app['id']}" in browser.owner_keys
    for app, address in ((service, "192.0.2.7"), (personal, "192.0.2.8")):
        admin_store.add_trusted_address(app["id"], address, max_entries=32, path=db, created_by=accountable_person(db, app["id"]))
        trusted_identity = admin_runtime.runtime_identity_for_trusted_address(ip_address(address), path=db)
        trusted = viewer_from_auth(trusted_identity, path=db)
        assert trusted.auth_kind == "trusted_address"
        assert (trusted.person_id, trusted.trusted_address, trusted.key_hash, trusted.credential_id) == (None, address, None, None)
        assert isinstance(decide(trusted, ChatRecord(None, app["id"]), "read"), Own)
        assert isinstance(decide(browser, ChatRecord(None, app["id"]), "read"), Deny)
    assert isinstance(decide(browser, CreationRecord("asset", frozenset({f"app:{service['id']}"})), "read"), Own)
    row = ChatRecord(person, personal["id"], origin=ContentOrigin("session"))
    assert isinstance(decide(key, row, "read"), Deny)
    assert isinstance(decide(key, replace(row, origin=ContentOrigin("app_key", credential_id=cred["id"])), "read"), Own)
    assert isinstance(decide(browser, row, "read"), Own)


def test_viewer_from_real_mcp_oauth_grant_auth(tmp_path):
    from gateway.content_access import viewer_from_auth
    world = World(tmp_path)
    # The grant's person goes through the production session-App resolver.
    person = admin_store.upsert_oidc_user("https://idp.test", "mcp-person", email="m@example.test", path=world.db)["id"]
    plan = store_plan("Local", path=world.db)
    accounts.change_person(person, "test", plan_id=plan["id"], path=world.db)
    app = admin_store.ensure_session_app_for_user(person, name="MCP session", path=world.db)
    # World issues a real grant for its trusted fixture person; bind that same
    # person to a real runtime session App rather than inventing a Viewer id.
    world.people[person] = {"id": person, "email": "m@example.test"}
    _, tokens = world.connect(person=person)
    grant = world.oauth.verify_access_token(tokens["access_token"], RESOURCE)
    request = Request({"type": "http", "method": "POST", "scheme": "https", "server": ("gw.test", 443),
                       "path": proxy.MCP_PATH, "query_string": b"person_id=forged&connection_id=forged", "headers": []})
    with patch.object(proxy.ADMIN_STORE, "path", world.db), patch.object(proxy, "MCP_OAUTH", world.oauth), patch.object(proxy, "MCP_OAUTH_ENABLED", True):
        authenticated = proxy._authenticate_mcp_access_token(request, proxy.MCP_PATH, tokens["access_token"])
    assert isinstance(authenticated, AuthContext)
    identity = authenticated.identity
    scope = me_media._member_media_scope(identity.person_id, world.db)
    viewer = viewer_from_auth(authenticated, path=world.db, owner_keys=scope.owner_keys)
    assert viewer.auth_kind == "mcp_grant"
    assert (viewer.person_id, viewer.app_id, viewer.connection_id) == (person, app["id"], grant.grant_id)
    row = ChatRecord(person, app["id"], origin=ContentOrigin("mcp_grant", connection_id=grant.grant_id))
    assert isinstance(decide(viewer, row, "read"), Own)
    assert isinstance(decide(viewer, ChatRecord(person, app["id"], origin=ContentOrigin("session")), "read"), Deny)


def test_viewer_authentication_invariants():
    import pytest
    for kind in ("trusted_address", "service_key"):
        with pytest.raises(ValueError, match="person"):
            Viewer(person_id="person:a", auth_kind=kind)
    for connection in (None, ""):
        with pytest.raises(ValueError, match="connection_id"):
            Viewer(auth_kind="mcp_grant", connection_id=connection)


def test_principal_fast_path_strips_empty_keys_once():
    from gateway.content_access import decide_own
    viewer = Viewer(auth_kind="app_key", owner_keys=frozenset({"", "app:a"}))
    assert viewer.owner_keys == frozenset({"app:a"})
    assert decide_own(viewer, ("app:a",))
    assert not decide_own(viewer, ("",))
    assert not decide_own(viewer, ())


def test_viewer_preserves_legacy_hash_only_content_scopes():
    from types import SimpleNamespace
    from gateway.content_access import ReceiptRecord, viewer_from_auth
    auth = AuthContext(identity=SimpleNamespace(key_hash="synthetic-owner"))
    viewer = viewer_from_auth(auth, owner_keys=("synthetic-owner",))
    assert viewer.person_id is viewer.app_id is viewer.credential_id is None
    assert not viewer.privacy_permission
    assert isinstance(decide(viewer, ReceiptRecord(frozenset({"synthetic-owner"})), "read"), Own)


def test_asset_and_video_lists_build_one_viewer_per_request():
    from gateway import content_access, request_connections
    from gateway.runtime_auth import RuntimeIdentity
    auth = AuthContext(identity=RuntimeIdentity(name="synthetic", key_hash="synthetic-owner"))
    with patch.object(proxy, "_asset_read_owner_keys", return_value=("synthetic-owner",)), \
            patch.object(proxy, "_read_asset_metadata", return_value={"owner_key_hashes": ["synthetic-owner"]}), \
            patch.object(proxy, "_video_job_records", {"job": {"owner_key_hash": "synthetic-owner"}}), \
            patch.object(content_access, "viewer_from_auth", wraps=content_access.viewer_from_auth) as build:
        with request_connections.request_scope():
            for _ in range(100):
                assert proxy._asset_access_allowed("synthetic-asset", auth)
                assert proxy._video_job_access_allowed("job", auth)
        assert build.call_count == 1


def test_decide_own_rejects_string_principals():
    import pytest
    from gateway.content_access import decide_own
    viewer = Viewer(person_id=None, app_id=None, auth_kind="session", owner_keys=frozenset({"x"}))
    with pytest.raises(TypeError):
        decide_own(viewer, "xy")
