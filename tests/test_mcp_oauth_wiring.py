"""MCP authorization on the real proxy app: scripted handshakes per auth mode.

Each mode runs the way a client would: 401 discovery, then (for OAuth)
registration, sign-in, consent and token exchange, then a legacy and a current
handshake and a tool call that the adapter dispatches in process to a core
route. No model runs; the tool call asks for a job that does not exist.
"""
from __future__ import annotations

import asyncio
import json
import secrets
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
from gateway import accounts, admin_store, mcp_oauth, media_jobs
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
             lambda request: SimpleNamespace(subject=request.headers["x-test-person"])
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
            self.assertEqual([t["name"] for t in tools.json()["result"]["tools"]], ["capabilities", "run", "job"])
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
                                           hash=proxy.hash_api_key(key), prefix=key[:8], path=self.admin)
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
