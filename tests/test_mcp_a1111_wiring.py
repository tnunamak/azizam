"""The MCP and A1111 adapters mounted on the real proxy app: auth, catalog and in-process dispatch."""

from __future__ import annotations

from contextlib import nullcontext

import sys
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import access_context  # noqa: E402
from tests.support.access import authenticated  # noqa: E402

CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}


def _tool(client: TestClient, name: str, arguments: dict) -> dict:
    resp = client.post("/mcp", json={**CALL, "params": {"name": name, "arguments": arguments}})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


class ProxyWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def test_mcp_and_sdapi_need_a_key_when_auth_is_on(self) -> None:
        with nullcontext():
            resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(resp.status_code, 401)
            self.assertIn("resource_metadata=", resp.headers["www-authenticate"])
            self.assertEqual(self.client.get("/sdapi/v1/options").status_code, 401)
            self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").status_code, 200)

    def test_tools_and_catalog_come_from_the_real_manifests(self) -> None:
        with authenticated():
            tools = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["capabilities", "run", "job"])
            rows = _tool(self.client, "capabilities", {})["structuredContent"]["workflows"]
            targets = {r["target"] for r in rows}
            self.assertTrue({"krea2-turbo", "speech"} <= targets, targets)
            detail = _tool(self.client, "capabilities", {"target": "krea2-turbo"})["structuredContent"]
            self.assertIn("prompt", detail["inputs"]["properties"])
            self.assertIn("n", detail["inputs"]["properties"])  # route-owned field merged in

    def test_a_run_reaches_the_core_route_and_a_core_refusal_comes_back_as_a_tool_error(self) -> None:
        from fastapi.responses import JSONResponse
        from fastapi.routing import APIRoute

        calls = []

        async def core_refusal(request: proxy.Request):
            calls.append(await request.json())
            return JSONResponse(
                {
                    "error": {
                        "message": "fixture core refusal",
                        "type": "invalid_request_error",
                        "code": "fixture_core_refusal",
                    }
                },
                status_code=409,
            )

        route = APIRoute("/v1/images/generations", core_refusal, methods=["POST"])
        with (
            authenticated(),
            patch.object(proxy.app.router, "routes", [route, *proxy.app.router.routes]),
        ):
            result = _tool(
                self.client,
                "run",
                {
                    "target": "krea2-turbo",
                    "mode": "sync",
                    "inputs": {"prompt": "audit-sentinel", "width": 256, "height": 256},
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "krea2-turbo")
        self.assertEqual(calls[0]["prompt"], "audit-sentinel")
        self.assertEqual(calls[0]["size"], "256x256")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["http_status"], 409)
        self.assertEqual(result["structuredContent"]["code"], "fixture_core_refusal")

    def test_sdapi_lists_the_real_image_models(self) -> None:
        with authenticated():
            titles = [m["title"] for m in self.client.get("/sdapi/v1/sd-models").json()]
            self.assertEqual(titles[:2], ["image", "image-fast"])
            self.assertIn("krea2-turbo", titles)
            self.assertEqual(self.client.get("/sdapi/v1/options").json()["sd_model_checkpoint"], "image")
            bad = self.client.post("/sdapi/v1/txt2img", json={"prompt": "x", "enable_hr": True})
            self.assertEqual(bad.status_code, 422)

    def test_stored_assets_are_signed_and_readable_only_by_their_owner(self) -> None:
        from starlette.requests import Request

        request = Request({"type": "http", "method": "POST", "path": "/mcp", "query_string": b"", "scheme": "https",
                           "headers": [(b"host", b"gw.example"), (b"x-forwarded-proto", b"https")], "client": ("127.0.0.1", 1)})
        owner = access_context(name="owner-a", key_hash="sha256:owner-a", app_id="app_a")
        other = access_context(name="owner-b", key_hash="sha256:owner-b", app_id="app_b")
        output = io.BytesIO()
        Image.new("RGB", (1, 1), (1, 2, 3)).save(output, format="PNG")
        png = output.getvalue()
        stored = proxy._mcp_store_asset(request, owner, png, "img", ".png")
        self.assertTrue(stored["url"].startswith("https://gw.example/v1/assets/img_"))
        self.assertIn("sig=", stored["url"])
        self.assertEqual(stored["media_type"], "image/png")
        self.assertEqual(proxy._mcp_read_asset(request, owner, stored["asset_id"]), png)
        self.assertIsNone(proxy._mcp_read_asset(request, other, stored["asset_id"]))
        self.assertIsNone(proxy._mcp_read_asset(request, owner, "img_" + "0" * 64))

    def test_the_adapters_are_listed_in_llms_txt(self) -> None:
        text = self.client.get("/llms.txt").text
        self.assertIn("/mcp", text)
        self.assertIn("/sdapi/v1/txt2img", text)


if __name__ == "__main__":
    unittest.main()
