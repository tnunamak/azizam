"""MCP authorization on the real proxy app: scripted handshakes per auth mode.

Each mode runs the way a client would: 401 discovery, then (for OAuth)
registration, sign-in, consent and token exchange, then a legacy and a current
handshake and a tool call that the adapter dispatches in process to a core
route. No model runs; the tool call asks for a job that does not exist.
"""
from __future__ import annotations

import asyncio
from tests.support.app_keys import accountable_person
import json
import secrets
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store, mcp_oauth, mcp_oauth_store, media_jobs
from gateway.receipts import RouteReceipt
from tests.support.access import store_plan

BASE = "https://gw.test"
CALLBACK = "https://client.example/callback"
MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "handshake", "version": "1"}}


class McpAuthorizationWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(root / "jobs.db")
        self.sessions: dict[str, str] = {}
        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", root / "jobs.db"),
            (proxy, "_public_base_url", lambda request: BASE),
            (proxy, "_oidc_session_from_request",
             lambda request: SimpleNamespace(subject=request.headers["x-test-person"], last_authenticated=time.time(),
                                             session_id="test-session", family_id="test-session")
             if request.headers.get("x-test-person") else None),
            (proxy, "_require_user_action_header", lambda request, session: None),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        proxy.MCP_OAUTH.register_limit = mcp_oauth._RateLimit(1000, 3600)
        self.person = admin_store.upsert_oidc_user("issuer", "ann", email="ann@example.test", path=self.admin)
        accounts.initialize(self.admin)
        self.plan = store_plan("Ann's plan", path=self.admin)
        accounts.change_person(self.person["id"], "test", path=self.admin, plan_id=self.plan["id"])
        self.client = TestClient(proxy.app, base_url=BASE, follow_redirects=False)

    # ---- client side of the protocol

    def post(self, token: str | None, method: str, params: dict | None = None, *, modern: bool,
             headers: dict | None = None):
        params = dict(params or {})
        hdrs = {"authorization": f"Bearer {token}"} if token else {}
        if modern:
            params["_meta"] = META
            hdrs.update({"mcp-protocol-version": MODERN, "mcp-method": method})
            if method == "tools/call":
                hdrs["mcp-name"] = params["name"]
        elif method != "initialize":
            hdrs["mcp-protocol-version"] = "2025-11-25"
        hdrs.update(headers or {})
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                headers=hdrs)

    def handshake(self, token: str, person_id: str | None = None) -> None:
        legacy = self.post(token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                 "clientInfo": {"name": "h", "version": "1"}}, modern=False)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                          headers={"authorization": f"Bearer {token}"}).status_code, 202)
        for modern in (False, True):
            tools = self.post(token, "tools/list", modern=modern)
            self.assertEqual(tools.status_code, 200, tools.text)
            self.assertEqual([t["name"] for t in tools.json()["result"]["tools"]],
                             ["capabilities", "run", "job", "rename_app", "rename_key"])
        discover = self.post(token, "server/discover", modern=True).json()["result"]
        self.assertIn(MODERN, discover["supportedVersions"])
        # Dispatched in process to GET /gateway/v1/media/jobs/{id}. A stand-in
        # core route authenticates the call exactly as the real ones do and
        # says whose request it saw.
        async def job_route(job_id: str, request: Request):
            auth = await asyncio.to_thread(proxy.authenticate_request, request, request.url.path)
            if isinstance(auth, Response):
                return auth
            return JSONResponse({"error": {"message": f"no job {job_id} for {auth.identity.person_id}",
                                           "code": "media_job_not_found"}}, status_code=404)

        route = APIRoute("/gateway/v1/media/jobs/{job_id}", job_route, methods=["GET"])
        with patch.object(proxy.app.router, "routes", [route, *proxy.app.router.routes]):
            job = self.post(token, "tools/call", {"name": "job", "arguments": {"job_id": "mj_missing"}}, modern=True)
        self.assertEqual(job.status_code, 200, job.text)
        result = job.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], f"no job mj_missing for {person_id}")

    def discover(self) -> dict:
        challenge = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(challenge.status_code, 401)
        header = challenge.headers["www-authenticate"]
        self.assertIn(f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"', header)
        self.assertIn('scope="mcp"', header)
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], f"{BASE}/mcp")
        self.assertEqual(resource["authorization_servers"], [BASE])
        issuer = resource["authorization_servers"][0]
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["issuer"], issuer)
        return metadata

    def sign_in(self, metadata: dict, client_id: str) -> dict:
        verifier = secrets.token_urlsafe(48)
        start = self.client.get(urlsplit(metadata["authorization_endpoint"]).path, params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s",
            "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
            "resource": f"{BASE}/mcp", "scope": "mcp"})
        self.assertEqual(start.status_code, 302, start.text)
        self.assertTrue(start.headers["location"].startswith("/authorize/?request="))
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
        details = self.client.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": self.person["id"]})
        self.assertEqual(details.status_code, 200, details.text)
        self.assertTrue(details.json()["can_connect"])
        decision = self.client.post(f"/me/api/mcp/requests/{request_id}", json={"decision": "allow"},
                                    headers={"x-test-person": self.person["id"]})
        self.assertEqual(decision.status_code, 200, decision.text)
        query = parse_qs(urlsplit(decision.json()["redirect"]).query)
        self.assertEqual(query["iss"], [BASE])
        token = self.client.post(urlsplit(metadata["token_endpoint"]).path, data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
            "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp"})
        self.assertEqual(token.status_code, 200, token.text)
        return token.json()

    def register(self, metadata: dict) -> str:
        registered = self.client.post(urlsplit(metadata["registration_endpoint"]).path, json={
            "client_name": "Handshake", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"]})
        self.assertEqual(registered.status_code, 201, registered.text)
        return registered.json()["client_id"]

    # ---- modes

    def test_no_credentials_get_a_challenge_that_leads_to_sign_in(self) -> None:
        metadata = self.discover()
        self.assertTrue(metadata["client_id_metadata_document_supported"])
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])

    def test_api_key_mode_still_works(self) -> None:
        app = admin_store.create_app("Key app", kind="personal", owner_user_id=self.person["id"],
                                     access_plan="person", path=self.admin)
        key = "opk_" + secrets.token_urlsafe(24)
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                           hash=proxy.hash_api_key(key), prefix=key[:8], path=self.admin, created_by=accountable_person(self.admin, app["id"]))
        self.handshake(key)  # an App key acts as its App, not as a person

    def test_oauth_dynamic_registration_mode(self) -> None:
        metadata = self.discover()
        tokens = self.sign_in(metadata, self.register(metadata))
        self.handshake(tokens["access_token"], self.person["id"])

    def test_oauth_metadata_document_mode(self) -> None:
        document_url = "https://client.example/oauth/client.json"
        document = {"client_id": document_url, "client_name": "Doc client", "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none"}

        async def fetch(url: str):
            self.assertEqual(url, document_url)
            return json.dumps(document).encode(), {}

        with patch.object(proxy.MCP_OAUTH.deps, "fetch_client_document", fetch):
            tokens = self.sign_in(self.discover(), document_url)
        self.handshake(tokens["access_token"], self.person["id"])

    def test_refresh_then_revoke_from_the_ui(self) -> None:
        metadata = self.discover()
        client_id = self.register(metadata)
        tokens = self.sign_in(metadata, client_id)
        refreshed = self.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                            "refresh_token": tokens["refresh_token"]}).json()
        self.handshake(refreshed["access_token"], self.person["id"])
        listed = self.client.get("/me/api/mcp/connections", headers={"x-test-person": self.person["id"]}).json()
        grant_id = listed["connections"][0]["id"]
        self.assertEqual(self.client.post(f"/me/api/mcp/connections/{grant_id}/revoke",
                                          headers={"x-test-person": self.person["id"]}).status_code, 200)
        gone = self.post(refreshed["access_token"], "tools/list", modern=True)
        self.assertEqual(gone.status_code, 401)
        self.assertIn('error="invalid_token"', gone.headers["www-authenticate"])

    def test_access_token_is_only_good_at_the_mcp_endpoint(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        bearer = {"authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/gateway/v1/media/jobs/mj_missing", headers=bearer).status_code, 401)
        forged = {**bearer, "x-azizam-mcp-dispatch": "guess"}
        self.assertEqual(self.client.get("/gateway/v1/media/jobs/mj_missing", headers=forged).status_code, 401)
        self.assertEqual(self.client.get("/v1/models", headers={"x-api-key": token}).status_code, 401)

    def test_a_sign_out_between_the_cookie_check_and_the_grant_leaves_no_connection(self) -> None:
        """Decision D41: the consent writes a lasting grant, so the sign-in is checked in that write's transaction."""
        metadata = self.discover()
        client_id = self.register(metadata)
        verifier = secrets.token_urlsafe(48)
        start = self.client.get(urlsplit(metadata["authorization_endpoint"]).path, params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s",
            "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
            "resource": f"{BASE}/mcp", "scope": "mcp"})
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
        real = mcp_oauth_store.Store.create_grant

        def sign_out_then_grant(store, **kwargs):
            accounts.revoke_session("test-session", int(time.time()) + 3600, path=self.admin, family_id="test-session")
            return real(store, **kwargs)

        with patch.object(mcp_oauth_store.Store, "create_grant", sign_out_then_grant):
            decision = self.client.post(f"/me/api/mcp/requests/{request_id}", json={"decision": "allow"},
                                        headers={"x-test-person": self.person["id"]})

        self.assertEqual(decision.status_code, 401, decision.text)
        self.assertEqual(proxy.MCP_OAUTH.deps.store.grants_for_person(self.person["id"]), [])

    def test_person_losing_access_is_refused_at_once(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        self.assertEqual(self.post(token, "tools/list", modern=True).status_code, 200)
        app = admin_store.get_session_app_for_user(self.person["id"], path=self.admin)
        with admin_store._conn(self.admin) as conn:
            conn.execute("UPDATE apps SET disabled_at = 1 WHERE id = ?", (app["id"],))
            conn.commit()
        self.assertEqual(self.post(token, "tools/list", modern=True).status_code, 403)

    def test_invalid_oauth_token_is_never_treated_as_an_app_key(self) -> None:
        resp = self.post(mcp_oauth.ACCESS_TOKEN_PREFIX + "forged", "tools/list", modern=True)
        self.assertEqual(resp.status_code, 401)

    def test_a_person_without_an_access_plan_cannot_connect(self) -> None:
        accounts.change_person(self.person["id"], "test", path=self.admin, plan_id=None)
        metadata = self.discover()
        verifier = secrets.token_urlsafe(48)
        start = self.client.get(urlsplit(metadata["authorization_endpoint"]).path, params={
            "response_type": "code", "client_id": self.register(metadata), "redirect_uri": CALLBACK, "state": "s",
            "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
            "resource": f"{BASE}/mcp", "scope": "mcp"})
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
        details = self.client.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": self.person["id"]})
        self.assertEqual(details.status_code, 200, details.text)
        self.assertFalse(details.json()["can_connect"])
        self.assertEqual(details.json()["plan"]["name"], "No access plan")

    def test_token_work_runs_under_the_persons_current_access_plan(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        dispatched = {"authorization": f"Bearer {token}", "x-azizam-mcp-dispatch": proxy._MCP_DISPATCH_SECRET}
        accounts.change_person(self.person["id"], "test", path=self.admin, plan_id=None)
        # Deny by default: with no plan, a core route refuses the token's work.
        refused = self.client.post("/v1/chat/completions", headers=dispatched,
                                   json={"model": "default", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "no_access_plan")

    def rename(self, token: str, tool: str, arguments: dict) -> dict:
        response = self.post(token, "tools/call", {"name": tool, "arguments": arguments}, modern=True)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_a_connected_client_renames_the_persons_own_app_and_key_and_nothing_else(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        mine = admin_store.create_app("Laptop", kind="personal", owner_user_id=self.person["id"],
                                      access_plan="person", path=self.admin)
        key = admin_store.create_credential_hash(subject_kind="app", subject_id=mine["id"], hash="hash-1",
                                                 prefix="opk_secretsecret1", display_name="Editor", path=self.admin, created_by=accountable_person(self.admin, mine["id"]))
        other = admin_store.upsert_oidc_user("issuer", "bo", email="bo@example.test", path=self.admin)
        theirs = admin_store.create_app("Theirs", kind="personal", owner_user_id=other["id"],
                                        access_plan="person", path=self.admin)

        app = self.rename(token, "rename_app", {"app": "laptop", "name": "  Work laptop "})
        renamed = self.rename(token, "rename_key", {"app": "Work laptop", "key": key["id"], "name": "Code editor"})
        stolen = self.rename(token, "rename_app", {"app": theirs["id"], "name": "Mine now"})

        self.assertNotIn("isError", app)
        self.assertEqual(app["structuredContent"], {"status": "renamed", "app": {"id": mine["id"], "name": "Work laptop"}})
        self.assertEqual(renamed["structuredContent"], {
            "status": "renamed", "key": {"id": key["id"], "name": "Code editor", "app_id": mine["id"]}})
        self.assertNotIn("secretsecret", json.dumps(renamed))
        self.assertTrue(stolen["isError"])
        self.assertEqual(stolen["structuredContent"]["code"], "app_not_found")
        self.assertEqual(admin_store.get_app(theirs["id"], path=self.admin)["name"], "Theirs")
        events = {event["action"]: event for event in admin_store.list_audit(path=self.admin)}
        self.assertEqual(events["app.self_rename"]["actor_id"], self.person["id"])
        self.assertEqual(events["app.self_rename"]["metadata"]["via"], "mcp")
        self.assertEqual(events["credential.self_rename"]["metadata"]["previous_name"], "Editor")

    def test_the_rename_tools_say_what_is_wrong_in_words(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        for name in ("Laptop", "Phone", "Phone", "Tablet"):
            admin_store.create_app(name, kind="personal", owner_user_id=self.person["id"], access_plan="person",
                                   path=self.admin)
        laptop = next(app for app in admin_store.list_apps_for_user(self.person["id"], path=self.admin)
                      if app["name"] == "Laptop")
        first = admin_store.create_credential_hash(subject_kind="app", subject_id=laptop["id"], hash="h1", prefix="opk_x",
                                                   display_name="Editor", path=self.admin, created_by=accountable_person(self.admin, laptop["id"]))
        second = admin_store.create_credential_hash(subject_kind="app", subject_id=laptop["id"], hash="h2", prefix="opk_x",
                                                    display_name="Editor", path=self.admin, created_by=accountable_person(self.admin, laptop["id"]))

        def refused(tool: str, arguments: dict) -> dict:
            result = self.rename(token, tool, arguments)
            self.assertTrue(result["isError"], result)
            return result["structuredContent"]

        ambiguous_app = refused("rename_app", {"app": "Phone", "name": "Tablet"})
        self.assertEqual(ambiguous_app["code"], "app_ambiguous")
        self.assertIn("app_", ambiguous_app["error"])
        ambiguous_key = refused("rename_key", {"app": "Laptop", "key": "Editor", "name": "Notes"})
        self.assertEqual(ambiguous_key["code"], "key_ambiguous")
        self.assertIn(first["id"], ambiguous_key["error"])
        self.assertIn(second["id"], ambiguous_key["error"])
        self.assertEqual(refused("rename_app", {"app": "Nope", "name": "X"})["code"], "app_not_found")
        self.assertEqual(refused("rename_key", {"app": "Laptop", "key": "Nope", "name": "X"})["code"], "credential_not_found")
        self.assertEqual(refused("rename_app", {"app": "Laptop", "name": "x" * 81})["code"], "app_name_too_long")
        self.assertEqual(refused("rename_app", {"app": "Laptop", "name": "Tablet"})["code"], "app_name_taken")
        self.assertEqual(refused("rename_app", {"app": "Laptop", "name": "Try"})["code"], "app_name_reserved")
        self.assertEqual(refused("rename_key", {"app": "Laptop", "key": first["id"], "name": "a\nb"})["code"],
                         "key_label_not_allowed")
        self.assertIn("`name` is required", refused("rename_app", {"app": "Laptop"})["error"])
        self.assertIn("unknown argument", refused("rename_app", {"app": "Laptop", "name": "X", "owner": "me"})["error"])
        self.assertEqual(admin_store.get_app(laptop["id"], path=self.admin)["name"], "Laptop")

    def test_an_app_key_cannot_rename_anything_over_mcp(self) -> None:
        app = admin_store.create_app("Key app", kind="personal", owner_user_id=self.person["id"],
                                     access_plan="person", path=self.admin)
        key = "opk_" + secrets.token_urlsafe(24)
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                           hash=proxy.hash_api_key(key), prefix=key[:8], created_by=self.person["id"],
                                           display_name="Its key", path=self.admin)

        for tool, arguments in (("rename_app", {"app": "Key app", "name": "Taken"}),
                                ("rename_key", {"app": "Key app", "key": "Its key", "name": "Taken"})):
            result = self.rename(key, tool, arguments)
            self.assertTrue(result["isError"], result)
            self.assertIn("not for an app key", result["structuredContent"]["error"])
        self.assertEqual(admin_store.get_app(app["id"], path=self.admin)["name"], "Key app")
        self.assertEqual(admin_store.list_audit(action_prefix="app.self_rename", path=self.admin), [])

    def test_a_new_consent_records_the_rename_permission_on_the_grant_and_the_client_cannot_ask_for_it(self) -> None:
        metadata = self.discover()
        self.sign_in(metadata, self.register(metadata))

        [grant] = proxy.MCP_OAUTH.deps.store.grants_for_person(self.person["id"])

        self.assertIn(mcp_oauth.RENAME_SCOPE, grant.scope.split())
        self.assertIn(mcp_oauth.SCOPE, grant.scope.split())
        self.assertNotIn(mcp_oauth.RENAME_SCOPE, mcp_oauth.SCOPES_SUPPORTED)
        asked = mcp_oauth.Client(proxy.MCP_OAUTH.deps.store.client(self.register(metadata)))
        self.assertEqual(asked.get_allowed_scope(f"mcp {mcp_oauth.RENAME_SCOPE}"), "mcp")

    def test_a_connection_made_before_renaming_was_offered_is_refused_in_words_and_changes_nothing(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        laptop = admin_store.create_app("Laptop", kind="personal", owner_user_id=self.person["id"],
                                        access_plan="person", path=self.admin)
        key = admin_store.create_credential_hash(subject_kind="app", subject_id=laptop["id"], hash="old-h",
                                                 prefix="opk_x", display_name="Editor", path=self.admin, created_by=accountable_person(self.admin, laptop["id"]))
        # What a grant made before the permission existed looks like: its scope names only the MCP scope.
        with admin_store._conn(self.admin) as conn:
            conn.execute("UPDATE mcp_oauth_grants SET scope = 'mcp offline_access'")
            conn.commit()

        for tool, arguments in (("rename_app", {"app": "Laptop", "name": "Late"}),
                                ("rename_key", {"app": "Laptop", "key": "Editor", "name": "Late"})):
            result = self.rename(token, tool, arguments)
            self.assertTrue(result["isError"], result)
            self.assertEqual(result["structuredContent"]["code"], "reconnect_required")
            self.assertIn("connect it again", result["structuredContent"]["error"])
        self.assertEqual(admin_store.get_app(laptop["id"], path=self.admin)["name"], "Laptop")
        self.assertEqual(admin_store.get_credential(key["id"], path=self.admin)["display_name"], "Editor")
        # Its other tools still work: the connection keeps what it was granted.
        self.assertEqual(self.post(token, "tools/list", modern=True).status_code, 200)

    def test_starting_the_store_again_does_not_rewrite_old_grants(self) -> None:
        metadata = self.discover()
        self.sign_in(metadata, self.register(metadata))
        with admin_store._conn(self.admin) as conn:
            conn.execute("UPDATE mcp_oauth_grants SET scope = 'mcp'")
            conn.commit()

        reopened = mcp_oauth_store.Store(lambda: self.admin)
        reopened.grants_for_person(self.person["id"])
        accounts.initialize(self.admin)
        admin_store.init_db(self.admin)

        self.assertEqual([g.scope for g in reopened.grants_for_person(self.person["id"])], ["mcp"])

    def test_a_disconnected_client_can_no_longer_rename(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        laptop = admin_store.create_app("Laptop", kind="personal", owner_user_id=self.person["id"],
                                        access_plan="person", path=self.admin)
        grant_id = self.client.get("/me/api/mcp/connections",
                                   headers={"x-test-person": self.person["id"]}).json()["connections"][0]["id"]
        self.client.post(f"/me/api/mcp/connections/{grant_id}/revoke", headers={"x-test-person": self.person["id"]})

        gone = self.post(token, "tools/call", {"name": "rename_app", "arguments": {"app": "Laptop", "name": "Late"}},
                         modern=True)

        self.assertEqual(gone.status_code, 401)
        self.assertEqual(admin_store.get_app(laptop["id"], path=self.admin)["name"], "Laptop")

    def test_the_consent_page_says_the_client_may_rename_apps_and_keys(self) -> None:
        metadata = self.discover()
        verifier = secrets.token_urlsafe(48)
        start = self.client.get(urlsplit(metadata["authorization_endpoint"]).path, params={
            "response_type": "code", "client_id": self.register(metadata), "redirect_uri": CALLBACK, "state": "s",
            "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
            "resource": f"{BASE}/mcp", "scope": "mcp"})
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]

        details = self.client.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": self.person["id"]})

        self.assertTrue(any("Rename your own apps and keys" in line for line in details.json()["access"]), details.json()["access"])

    def test_receipts_record_the_connection_that_made_the_request(self) -> None:
        metadata = self.discover()
        token = self.sign_in(metadata, self.register(metadata))["access_token"]
        grant_id = self.client.get("/me/api/mcp/connections",
                                   headers={"x-test-person": self.person["id"]}).json()["connections"][0]["id"]

        async def who(request: Request):
            auth = await asyncio.to_thread(proxy.authenticate_request, request, request.url.path)
            if isinstance(auth, Response):
                return auth
            receipt = RouteReceipt(protocol="test", intent="test.who", app_id=auth.identity.app_id,
                                   api_key_name=auth.identity.name)
            # Another App's receipt in the same request is not the connection's.
            other = RouteReceipt(protocol="test", intent="test.other", app_id="app_other")
            return JSONResponse({"connection_id": receipt.to_dict()["connection_id"], "name": receipt.api_key_name,
                                 "other": other.to_dict()["connection_id"]})

        routes = [APIRoute("/gateway/v1/test/who", who, methods=["GET"])]
        dispatched = {"authorization": f"Bearer {token}", "x-azizam-mcp-dispatch": proxy._MCP_DISPATCH_SECRET}
        with patch.object(proxy.app.router, "routes", [*routes, *proxy.app.router.routes]):
            signed_in = self.client.get("/gateway/v1/test/who", headers=dispatched)
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        self.assertEqual(signed_in.json()["connection_id"], grant_id)
        self.assertEqual(signed_in.json()["name"], "Handshake (MCP)")
        self.assertIsNone(signed_in.json()["other"])
