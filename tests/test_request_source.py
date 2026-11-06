"""Request source facts are durable, safe, and isolated between clients."""
import asyncio
import unittest

from gateway.receipts import RouteReceipt
from gateway.request_source import RequestSourceMiddleware, connection_for, current_client_hint, note_connection


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

    def test_concurrent_connections_each_tag_only_their_own_session_app(self):
        async def run():
            receipts = {}

            async def app(scope, receive, send):
                connection = scope["connection"]
                note_connection("app_session", connection)
                await asyncio.sleep(0)
                receipts[connection] = (
                    RouteReceipt(protocol="mcp", intent="chat.generate", app_id="app_session"),
                    RouteReceipt(protocol="mcp", intent="chat.generate", app_id="app_other"),
                )

            wrapped = RequestSourceMiddleware(app)
            await asyncio.gather(*(wrapped({"type": "http", "headers": [], "connection": name}, None, None)
                                   for name in ("mcg_one", "mcg_two")))
            for name, (session, other) in receipts.items():
                self.assertEqual(session.connection_id, name)
                self.assertEqual(session.to_dict("admin")["connection_id"], name)
                self.assertIsNone(other.connection_id)
            self.assertIsNone(connection_for("app_session"))  # nothing leaks past the request
            self.assertIsNone(RouteReceipt(protocol="mcp", intent="x", app_id="app_session").connection_id)
        asyncio.run(run())

    def test_facade_that_attaches_its_app_late_still_gets_the_connection(self):
        async def run():
            seen = []

            async def app(scope, receive, send):
                note_connection("app_session", "mcg_late")
                receipt = RouteReceipt(protocol="provider-native", intent="provider.native")
                receipt.app_id = "app_session"
                seen.append(receipt.to_dict("internal")["connection_id"])

            await RequestSourceMiddleware(app)({"type": "http", "headers": []}, None, None)
            self.assertEqual(seen, ["mcg_late"])
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


def test_gateway_ui_tests_keep_app_identity_and_reset_attribution():
    from gateway.request_source import current_gateway_ui, gateway_ui_request
    from gateway import receipt_store
    async def run():
        rows = []
        async def app(scope, receive, send):
            await asyncio.sleep(0)
            rows.append(RouteReceipt(protocol="openai", intent="chat.generate", app_id="owned-app"))
        middleware = RequestSourceMiddleware(app)
        async def call(referer):
            await middleware({"type": "http", "headers": [(b"host", b"gateway.test"), (b"referer", referer)]}, None, None)
        await asyncio.gather(call(b"http://gateway.test/try/"), call(b"http://external.test/try/"), call(b"http://[malformed"))
        assert {row.source_kind for row in rows} == {"gateway-ui", "app"}
        assert all(row.app_id == "owned-app" for row in rows)
        with gateway_ui_request():
            await call(b"")
            assert current_gateway_ui()
        assert not current_gateway_ui()
        assert rows[-1].source_kind == "gateway-ui"
        assert receipt_store._with_source(rows[-1].to_dict("internal"))["source"]["app_id"] == "owned-app"
    asyncio.run(run())


def test_request_context_restores_in_the_same_task_on_success_and_failure():
    import pytest
    from gateway.request_source import current_gateway_ui

    async def exercise():
        assert current_client_hint() is None
        assert current_gateway_ui() is False

        for fail in (False, True):
            async def app(scope, receive, send):
                assert current_client_hint() == "Daisy/pi"
                assert current_gateway_ui() is True
                if fail:
                    raise RuntimeError("fixture")

            wrapped = RequestSourceMiddleware(app)
            scope = {
                "type": "http",
                "headers": [(b"user-agent", b"Daisy/1 private"),
                            (b"host", b"gateway.test"),
                            (b"referer", b"http://gateway.test/try/")],
            }
            if fail:
                with pytest.raises(RuntimeError, match="fixture"):
                    await wrapped(scope, None, None)
            else:
                await wrapped(scope, None, None)

            # These checks must be inside this coroutine, after await.
            assert current_client_hint() is None
            assert current_gateway_ui() is False

    asyncio.run(exercise())
