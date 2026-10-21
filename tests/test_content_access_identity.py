"""Step-1 identity gate: real auth/store builders, with test-owned state only."""
from ipaddress import ip_address
from unittest.mock import patch

import proxy
from gateway import accounts, admin_runtime, admin_store, me_media, ownership
from gateway.content_access import ChatRecord, ContentOrigin, CreationRecord, Deny, Own, Viewer, decide
from gateway.runtime_auth import hash_api_key
from proxy import AuthContext
from starlette.requests import Request
from tests.support.access import store_plan
from tests.test_mcp_oauth import RESOURCE, World


def test_viewers_from_real_session_app_key_and_trusted_address_auth(tmp_path):
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
    key = Viewer(identity.person_id, identity.app_id, auth_kind="app_key", credential_id=cred["id"],
                 owner_keys=frozenset(ownership.readable_owner_keys(AuthContext(identity=identity), path=db)))
    assert (key.person_id, key.app_id, key.credential_id) == (person, personal["id"], cred["id"])
    assert f"app:{service['id']}" not in key.owner_keys
    browser_identity = admin_runtime.runtime_identity_for_session_app(session_app["id"], person, path=db)
    scope = me_media._member_media_scope(browser_identity.person_id, db)
    browser = Viewer(browser_identity.person_id, browser_identity.app_id, auth_kind="session", owner_keys=scope.owner_keys)
    for app in (personal, service, session_app):
        assert f"app:{app['id']}" in browser.owner_keys
    admin_store.add_trusted_address(service["id"], "192.0.2.7", max_entries=32, path=db)
    trusted_identity = admin_runtime.runtime_identity_for_trusted_address(ip_address("192.0.2.7"), path=db)
    trusted = Viewer(trusted_identity.person_id, trusted_identity.app_id, auth_kind="trusted_address",
                     trusted_address=trusted_identity.trusted_address,
                     owner_keys=frozenset(ownership.readable_owner_keys(AuthContext(identity=trusted_identity), path=db)))
    assert (trusted.person_id, trusted.trusted_address, trusted.key_hash, trusted.credential_id) == (None, "192.0.2.7", None, None)
    assert isinstance(decide(trusted, ChatRecord(None, service["id"]), "read"), Own)
    assert isinstance(decide(browser, ChatRecord(None, service["id"]), "read"), Deny)
    assert isinstance(decide(browser, CreationRecord("asset", frozenset({f"app:{service['id']}"})), "read"), Own)
    row = ChatRecord(person, personal["id"], origin=ContentOrigin("session"))
    assert isinstance(decide(key, row, "read"), Deny)
    assert isinstance(decide(browser, row, "read"), Own)


def test_viewer_from_real_mcp_oauth_grant_auth(tmp_path):
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
    viewer = Viewer(identity.person_id, identity.app_id, auth_kind="mcp_grant", connection_id=identity.connection_id,
                    owner_keys=scope.owner_keys)
    assert (viewer.person_id, viewer.app_id, viewer.connection_id) == (person, app["id"], grant.grant_id)
    row = ChatRecord(person, app["id"], origin=ContentOrigin("mcp_grant", connection_id=grant.grant_id))
    assert isinstance(decide(viewer, row, "read"), Own)
    assert isinstance(decide(viewer, ChatRecord(person, app["id"], origin=ContentOrigin("session")), "read"), Deny)
