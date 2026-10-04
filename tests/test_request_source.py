"""Request source facts are durable, safe, and isolated between clients."""
import asyncio
import unittest

from gateway.receipts import RouteReceipt
from gateway.request_source import RequestSourceMiddleware, current_client_hint


class RequestSourceTests(unittest.TestCase):
    def test_concurrent_clients_keep_only_known_product_hints(self):
        async def run():
            receipts = []

            async def app(scope, receive, send):
                await asyncio.sleep(0)
                receipts.append(RouteReceipt(protocol="openai", intent="chat.generate"))

            wrapped = RequestSourceMiddleware(app)
            async def request(agent):
                await wrapped({"type": "http", "headers": [(b"user-agent", agent)]}, None, None)
            await asyncio.gather(request(b"SillyTavern/1.0 private-value"), request(b"OpenWebUI/2.0 private-value"), request(b"unknown-private-agent"))
            self.assertEqual({r.client_hint for r in receipts}, {"SillyTavern", "Open WebUI", None})
            self.assertTrue(all(r.source_kind == "local-network" for r in receipts))
            self.assertTrue(all("no key" in r.source_label for r in receipts))
            self.assertNotIn("private", str([r.to_dict("admin") for r in receipts]))
            self.assertIsNone(current_client_hint())
        asyncio.run(run())

    def test_apps_and_internal_hops_have_distinct_principals(self):
        app = RouteReceipt(protocol="openai", intent="chat.generate", app_id="app_service", api_key_name="Shared client")
        internal = RouteReceipt(protocol="openai", intent="chat.generate", parent_request_id="request_outer")
        self.assertEqual((app.source_kind, app.source_label), ("app", "Shared client"))
        self.assertEqual(internal.source_kind, "internal")

    def test_facade_can_attach_app_after_constructing_receipt(self):
        receipt = RouteReceipt(protocol="provider-native", intent="provider.native")
        receipt.app_id = "app_service"
        receipt.api_key_name = "Service client"
        value = receipt.to_dict("admin")
        self.assertEqual(value["source_kind"], "app")
        self.assertEqual(value["source_label"], "Service client")

    def test_context_resets_when_request_raises(self):
        async def fail(scope, receive, send):
            self.assertEqual(current_client_hint(), "Daisy/pi")
            raise RuntimeError("fixture")
        with self.assertRaises(RuntimeError):
            asyncio.run(RequestSourceMiddleware(fail)({"type": "http", "headers": [(b"user-agent", b"Daisy/1")]}, None, None))
        self.assertIsNone(current_client_hint())
