"""Internal calls run as the caller they serve, never as the gateway's own address.

Protocol frontends (Anthropic, Ollama) loop a request back to the chat and
embeddings routes over 127.0.0.1, and the MCP adapter dispatches in process.
The owner may trust 127.0.0.1 for local scripts, so an internal call that
re-authenticated by its network position would run as that App instead of the
caller (the loopback itself is covered in test_runtime_route_policy.py).
Internal hops that only know their parent's request id must still record
whose request they served.
"""
from __future__ import annotations

import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

import proxy
from gateway import accounts, admin_store
from gateway.receipts import RouteReceipt
from tests.support.access import store_plan


def _request(client: tuple[str, int] | None, headers: dict[str, str] | None = None,
             path: str = "/v1/messages") -> Request:
    return Request({
        "type": "http", "method": "POST", "path": path, "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 5000), "client": client,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    })


class InternalCallIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.admin = Path(tmp.name) / "admin.db"
        admin_store.init_db(self.admin)
        accounts.initialize(self.admin)
        owner = admin_store.upsert_oidc_user("https://idp.test", "ann", email="ann@example.test", path=self.admin)
        plan = store_plan("Everything", path=self.admin)
        accounts.change_person(owner["id"], "test", path=self.admin, plan_id=plan["id"])
        self.local = admin_store.create_app("This computer", kind="service", owner_user_id=owner["id"],
                                            access_plan=plan["id"], path=self.admin)
        self.kitchen = admin_store.create_app("Kitchen tablet", kind="service", owner_user_id=owner["id"],
                                              access_plan=plan["id"], path=self.admin)
        admin_store.add_trusted_address(self.local["id"], "127.0.0.1", max_entries=10, path=self.admin)
        admin_store.add_trusted_address(self.kitchen["id"], "192.0.2.7", max_entries=10, path=self.admin)
        patcher = patch.object(proxy.ADMIN_STORE, "path", self.admin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def kitchen_context(self) -> proxy.AuthContext:
        identity = proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("192.0.2.7"), path=self.admin)
        assert identity is not None and identity.app_id == self.kitchen["id"]
        return proxy.AuthContext(identity=identity)

    def test_a_plain_request_from_the_gateway_host_is_the_trusted_app(self) -> None:
        # The premise: 127.0.0.1 really is trusted, so a leak would be visible.
        auth = proxy.authenticate_request(_request(("127.0.0.1", 40000), path="/v1/chat/completions"),
                                          "/v1/chat/completions")
        self.assertIsInstance(auth, proxy.AuthContext)
        self.assertEqual(auth.identity.app_id, self.local["id"])

    def test_internal_forward_never_falls_back_to_the_trusted_address(self) -> None:
        inner = _request(("127.0.0.1", 40000), {
            proxy._INTERNAL_PROTOCOL_FORWARD_HEADER: proxy._INTERNAL_PROTOCOL_FORWARD_SECRET,
        }, path="/v1/chat/completions")
        auth = proxy.authenticate_request(inner, "/v1/chat/completions")
        self.assertIsInstance(auth, Response)
        self.assertEqual(auth.status_code, 401)

    async def test_mcp_dispatch_without_a_client_address_is_not_the_gateway_host(self) -> None:
        async def whoami(request: Request):
            auth = proxy.authenticate_request(request, request.url.path)
            if isinstance(auth, Response):
                return auth
            return JSONResponse({"app_id": auth.identity.app_id})

        route = APIRoute("/v1/test-whoami", whoami, methods=["GET"])
        with patch.object(proxy.app.router, "routes", [route, *proxy.app.router.routes]):
            response = await proxy._mcp_dispatch_call(_request(None, path="/mcp"), "GET", "/v1/test-whoami")
        self.assertEqual(response.status_code, 401, response.text)


class ChildReceiptIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = RouteReceipt(
            protocol="openai.chat", path="/v1/chat/completions", intent="chat.generate",
            app_id="app_parent", api_key_name="This computer (local scripts)",
            owner_key_hash="app:app_parent", app_profile="plan_local",
        )
        self.recorded: list[RouteReceipt] = []
        patcher = patch.object(proxy.receipt_store, "record", lambda receipt, **_k: self.recorded.append(receipt))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_child_of_parent(self, child: RouteReceipt) -> None:
        self.assertEqual(child.parent_request_id, self.parent.request_id)
        self.assertEqual(child.app_id, "app_parent")
        self.assertEqual(child.api_key_name, "This computer (local scripts)")
        self.assertEqual(child.owner_key_hash, "app:app_parent")
        self.assertEqual(child.app_profile, "plan_local")
        self.assertEqual(child.source_kind, "internal")

    def test_internal_hop_receipt_carries_the_parents_app(self) -> None:
        proxy._record_internal_hop_receipt(
            request_path="/v1/chat/completions", parent_request_id=self.parent.request_id,
            body={"model": "m", "messages": []}, status_code=200, terminal_reason="completed",
        )
        self.assertEqual(len(self.recorded), 1)
        self.assert_child_of_parent(self.recorded[0])

    def test_loopback_receipt_carries_the_parents_app(self) -> None:
        child = proxy._start_litellm_route_receipt(
            request_path="/v1/chat/completions", auth_context=proxy.NO_AUTH_CONTEXT,
            requested_model="m", parent_request_id=self.parent.request_id,
        )
        self.assert_child_of_parent(child)

    def test_unknown_parent_records_no_app(self) -> None:
        child = RouteReceipt(protocol="openai.chat", path="/v1/chat/completions", intent="chat.generate",
                             parent_request_id="req_unknown")
        self.assertIsNone(child.app_id)


if __name__ == "__main__":
    unittest.main()
