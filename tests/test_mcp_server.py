"""MCP server (gateway/mcp_server.py) with a fake core: protocol eras, auth, tools, results."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import mcp_server
from gateway.mcp_server import McpDeps

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _entry(short, intent, *, aliases=(), props=None, required=(), recommended="sync", sync=None, async_=None,
           route_inputs=()):
    return {
        "id": f"{intent}.{short}", "short_id": short, "intent": intent, "display_name": short,
        "description": f"{short} workflow", "aliases": list(aliases), "typical_seconds": 5.0,
        "outputs": [], "route_inputs": list(route_inputs),
        "inputs": {"type": "object", "properties": props or {}, "required": list(required), "additionalProperties": False,
                   "x-azizam-workflow-id": f"{intent}.{short}"},
        "execution": {"recommended": recommended, "sync": sync, "async": async_},
    }


CATALOG = [
    _entry(
        "krea", "image.generate", aliases=["image"], required=["prompt"],
        props={"prompt": {"type": "string"}, "width": {"type": "integer", "minimum": 64, "maximum": 2048},
               "height": {"type": "integer", "minimum": 64, "maximum": 2048}, "steps": {"type": "integer", "minimum": 1, "maximum": 20},
               "seed": {"type": "integer"}},
        sync={"method": "POST", "path": "/v1/images/generations", "content_type": "application/json", "model": "krea",
              "fields": {"prompt": {"in": "body", "field": "prompt"},
                         "width": {"in": "body", "field": "size", "combine": ["width", "height"]},
                         "height": {"in": "body", "field": "size", "combine": ["width", "height"]},
                         "steps": {"in": "extension", "field": "steps"}, "seed": {"in": "none"}}},
        async_={"kind": "media_job", "submit": {"method": "POST", "path": "/gateway/v1/media/jobs", "content_type": "application/json",
                                                "body": {"intent": "image.generate", "request": "<>"}, "model": "krea"},
                "fields": {"prompt": {"in": "body", "field": "prompt"}}},
        route_inputs=[{"name": "n", "type": "integer", "minimum": 1, "maximum": 10, "in": "body", "field": "n"}],
    ),
    _entry(
        "qwen-edit", "image.edit", aliases=["image", "image-edit"], required=["prompt", "source_image"],
        props={"prompt": {"type": "string"}, "source_image": {"type": "string"}},
        sync={"method": "POST", "path": "/v1/images/edits", "content_type": "multipart/form-data", "model": "qwen-edit",
              "fields": {"prompt": {"in": "form", "field": "prompt"}, "source_image": {"in": "file", "field": "image"}}},
    ),
    _entry(
        "sfx", "audio.generate", aliases=["sfx"], required=["prompt"], recommended="async",
        props={"prompt": {"type": "string"}, "duration": {"type": "number"}},
        sync={"method": "POST", "path": "/v1/audio/generate", "content_type": "application/json", "model": "sfx",
              "fields": {"prompt": {"in": "body", "field": "prompt"}, "duration": {"in": "body", "field": "duration"}}},
        async_={"kind": "media_job", "submit": {"method": "POST", "path": "/gateway/v1/media/jobs", "content_type": "application/json",
                                                "body": {"intent": "audio.generate", "request": "<>"}, "model": "sfx"},
                "fields": {"prompt": {"in": "body", "field": "prompt"}, "duration": {"in": "body", "field": "duration"}}},
    ),
    _entry(
        "ltx", "video.generate", aliases=["video"], required=["prompt"], recommended="async",
        props={"prompt": {"type": "string"}, "image": {"type": "string"}},
        async_={"kind": "video", "submit": {"method": "POST", "path": "/v1/videos", "content_type": "application/json", "model": "video-ltx"},
                "fields": {"prompt": {"in": "body", "field": "prompt"}, "image": {"in": "body", "field": "input_reference"}}},
    ),
]
ALIASES = {"operations": {"speech": {"stable": [{"alias": "tts"}, {"alias": "tts-fast"}]}}}


class Core:
    """Fake core: records calls, answers from a route table, supports job progression."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.routes: dict[tuple[str, str], Any] = {}
        self.polls: dict[str, list[dict[str, Any]]] = {}
        self.stored: list[tuple[str, str, int]] = []
        self.assets: dict[str, bytes] = {}
        self.authed = True

    async def call(self, request, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        queue = self.polls.get(path)
        if queue:
            return httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])
        reply = self.routes.get((method, path))
        if callable(reply):
            reply = reply(kwargs)
        if reply is None:
            return httpx.Response(404, json={"error": {"message": f"no route {method} {path}", "code": "not_found"}})
        return reply

    def store_asset(self, request, auth, content, kind, suffix):
        self.stored.append((kind, suffix, len(content)))
        asset_id = f"{kind}_{'a' * 64}"
        return {"asset_id": asset_id, "url": f"https://gw.test/v1/assets/{asset_id}/content?sig=x", "media_type": "x", "size_bytes": len(content)}

    def read_asset(self, request, auth, asset_id):
        return self.assets.get(asset_id)


@pytest.fixture()
def env():
    core = Core()

    def authenticate(request, _path):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": {"message": "Missing API key."}}, status_code=401)
        return SimpleNamespace(identity="k")

    deps = McpDeps(authenticate=authenticate, call=core.call, catalog=lambda: CATALOG, aliases=lambda: ALIASES,
                   store_asset=core.store_asset, read_asset=core.read_asset,
                   public_base_url=lambda r: "https://gw.test", poll_interval=0.0,
                   allowed_origins=lambda r: ["https://gw.test"])
    app = FastAPI()
    mcp_server.register_mcp_routes(app, deps)
    return SimpleNamespace(core=core, client=TestClient(app))


H = {"authorization": "Bearer good"}


def rpc(env, method, params=None, *, headers=None, modern=False, id_=1):
    params = dict(params or {})
    hdrs = dict(H)
    if modern:
        params["_meta"] = META
        hdrs.update({"mcp-protocol-version": MODERN, "mcp-method": method})
        if method == "tools/call":
            hdrs["mcp-name"] = params["name"]
    elif method != "initialize":
        hdrs["mcp-protocol-version"] = "2025-11-25"  # sent after initialize since 2025-06-18
    hdrs.update(headers or {})
    return env.client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, headers=hdrs)


def call(env, name, arguments, **kw):
    resp = rpc(env, "tools/call", {"name": name, "arguments": arguments}, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


# ---------------------------------------------------------------- transport


def test_missing_key_is_401_with_resource_metadata_challenge(env):
    resp = env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://gw.test/.well-known/oauth-protected-resource/mcp"')


def test_presented_but_rejected_token_gets_invalid_token_and_scope():
    deps = McpDeps(authenticate=lambda r, p: JSONResponse({"error": {"message": "no"}}, status_code=401),
                   call=None, catalog=lambda: [], aliases=lambda: {}, store_asset=None, read_asset=None,
                   public_base_url=lambda r: "https://gw.test", scopes=("mcp",),
                   authorization_servers=lambda r: ["https://gw.test"])
    app = FastAPI()
    mcp_server.register_mcp_routes(app, deps)
    client = TestClient(app)
    bare = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert bare.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://gw.test/.well-known/oauth-protected-resource/mcp", scope="mcp"')
    stale = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"authorization": "Bearer old"})
    assert stale.headers["www-authenticate"].startswith('Bearer error="invalid_token", error_description=')
    assert 'scope="mcp"' in stale.headers["www-authenticate"]
    body = client.get("/.well-known/oauth-protected-resource").json()
    assert body["authorization_servers"] == ["https://gw.test"] and body["scopes_supported"] == ["mcp"]
    assert "offline_access" not in body["scopes_supported"]


def test_protected_resource_metadata_names_header_bearer_only(env):
    body = env.client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert body["resource"] == "https://gw.test/mcp"
    assert body["bearer_methods_supported"] == ["header"]


def test_foreign_origin_is_403_and_listed_origin_passes(env):
    assert rpc(env, "ping", headers={"origin": "https://evil.example"}).status_code == 403
    assert rpc(env, "ping", headers={"origin": "https://GW.test"}).status_code == 200


def test_dns_rebinding_page_cannot_vouch_for_itself_with_host(env):
    # A rebinding page controls both Host and Origin; only the list counts.
    headers = {"origin": "http://evil.example", "host": "evil.example"}
    assert rpc(env, "ping", headers=headers).status_code == 403


def test_get_and_delete_are_405_because_there_is_no_stream(env):
    assert env.client.get("/mcp", headers=H).status_code == 405
    assert env.client.delete("/mcp", headers=H).headers["allow"] == "POST"


def test_garbage_body_is_a_parse_error(env):
    resp = env.client.post("/mcp", content=b"{nope", headers=H)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == mcp_server.PARSE_ERROR


# ------------------------------------------------------------- legacy era


def test_modern_request_without_required_meta_is_400_invalid_params(env):
    headers = {**H, "mcp-protocol-version": MODERN, "mcp-method": "tools/list"}
    for meta in (None, {"io.modelcontextprotocol/clientCapabilities": {}},
                 {"io.modelcontextprotocol/protocolVersion": MODERN}):
        params = {} if meta is None else {"_meta": meta}
        resp = env.client.post("/mcp", headers=headers,
                               json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": params})
        assert resp.status_code == 400, meta
        assert resp.json()["error"]["code"] == mcp_server.INVALID_PARAMS and resp.json()["id"] == 7


def test_modern_era_has_no_initialize_or_ping(env):
    for method in ("initialize", "ping"):
        resp = rpc(env, method, modern=True)
        assert resp.status_code == 404 and resp.json()["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_modern_results_name_the_server_in_meta(env):
    result = rpc(env, "tools/list", modern=True).json()["result"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "azizam"


def test_batches_are_refused_outside_2025_03_26(env):
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]
    resp = env.client.post("/mcp", json=batch, headers={**H, "mcp-protocol-version": "2025-11-25"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == mcp_server.INVALID_REQUEST
    mixed = env.client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"id": 2}], headers=H)
    assert [r.get("error", {}).get("code") for r in mixed.json()] == [None, mcp_server.INVALID_REQUEST]


def test_2025_03_26_results_carry_text_only(env):
    resp = env.client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                     "params": {"name": "capabilities", "arguments": {}}})
    result = resp.json()["result"]
    assert "structuredContent" not in result
    assert [c["type"] for c in result["content"]] == ["text"]


def test_legacy_initialize_negotiates_and_mints_no_session(env):
    resp = rpc(env, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}})
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "mcp-session-id" not in resp.headers
    assert "resultType" not in result


def test_legacy_initialize_with_unknown_version_gets_our_newest_legacy(env):
    result = rpc(env, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == "2025-11-25"


def test_notification_is_202_without_body(env):
    resp = env.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=H)
    assert resp.status_code == 202 and resp.content == b""


def test_legacy_tools_list_has_exactly_the_three_meta_tools(env):
    tools = rpc(env, "tools/list", headers={"mcp-protocol-version": "2025-06-18"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["capabilities", "run", "job"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)


def test_legacy_unknown_method_is_a_200_json_rpc_error(env):
    resp = rpc(env, "resources/list")
    assert resp.status_code == 200 and resp.json()["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_batch_is_answered_in_order_and_notifications_dropped(env):
    resp = env.client.post("/mcp", headers=H, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ])
    assert [r["id"] for r in resp.json()] == [1, 2]


# ------------------------------------------------------------- modern era


def test_modern_discover_lists_versions_and_identity(env):
    result = rpc(env, "server/discover", modern=True).json()["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == MODERN
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "azizam"


def test_modern_request_needs_matching_headers(env):
    bad_method = rpc(env, "tools/list", modern=True, headers={"mcp-method": "tools/call"})
    assert bad_method.status_code == 400 and bad_method.json()["error"]["code"] == mcp_server.HEADER_MISMATCH
    bad_name = rpc(env, "tools/call", {"name": "run", "arguments": {}}, modern=True, headers={"mcp-name": "job"})
    assert bad_name.status_code == 400 and bad_name.json()["error"]["code"] == mcp_server.HEADER_MISMATCH
    bad_version = rpc(env, "tools/list", modern=True, headers={"mcp-protocol-version": "2025-06-18"})
    assert bad_version.status_code == 400 and bad_version.json()["error"]["code"] == mcp_server.HEADER_MISMATCH


def test_modern_base64_sentinel_in_mcp_name_is_decoded(env):
    sentinel = "=?base64?" + base64.b64encode(b"capabilities").decode() + "?="
    resp = rpc(env, "tools/call", {"name": "capabilities", "arguments": {}}, modern=True, headers={"mcp-name": sentinel})
    assert resp.status_code == 200


def test_modern_unsupported_version_lists_supported(env):
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "2030-01-01"}
    resp = env.client.post("/mcp", headers={**H, "mcp-protocol-version": "2030-01-01", "mcp-method": "tools/list"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}})
    err = resp.json()["error"]
    assert resp.status_code == 400 and err["code"] == mcp_server.UNSUPPORTED_VERSION
    assert "2026-07-28" in err["data"]["supported"] and err["data"]["requested"] == "2030-01-01"


def test_modern_unknown_method_is_404_with_32601(env):
    resp = rpc(env, "resources/list", modern=True)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_modern_results_carry_result_type_and_cache_hints(env):
    result = rpc(env, "tools/list", modern=True).json()["result"]
    assert result["resultType"] == "complete" and result["cacheScope"] == "public"


def test_unknown_tool_is_a_protocol_error(env):
    resp = rpc(env, "tools/call", {"name": "nope", "arguments": {}})
    assert resp.json()["error"]["code"] == mcp_server.INVALID_PARAMS


# ------------------------------------------------------------ capabilities


def test_capabilities_list_is_compact_and_includes_speech(env):
    result = call(env, "capabilities", {})
    rows = {r["target"]: r for r in result["structuredContent"]["workflows"]}
    assert {"krea", "qwen-edit", "sfx", "ltx", "speech"} <= set(rows)
    assert rows["speech"]["aliases"] == ["tts", "tts-fast"]
    assert rows["ltx"]["mode"] == "async"


def test_capabilities_filters_by_query_and_intent(env):
    rows = call(env, "capabilities", {"intent": "video.generate"})["structuredContent"]["workflows"]
    assert [r["target"] for r in rows] == ["ltx"]
    rows = call(env, "capabilities", {"query": "SFX"})["structuredContent"]["workflows"]
    assert [r["target"] for r in rows] == ["sfx"]


def test_capabilities_target_returns_typed_inputs_with_route_fields(env):
    detail = call(env, "capabilities", {"target": "image"})["structuredContent"]
    assert detail["target"] == "krea"  # ambiguous alias: generate wins with no inputs
    assert detail["inputs"]["required"] == ["prompt"]
    assert detail["inputs"]["properties"]["n"]["maximum"] == 10
    assert "x-azizam-workflow-id" not in detail["inputs"]
    assert detail["modes_available"] == ["sync", "async"]


def test_capabilities_speech_describes_its_schema_and_voices(env):
    env.core.routes[("GET", "/v1/audio/voices")] = httpx.Response(200, json={"default_voice": "alloy", "voices": [{"voice_id": "alloy", "name": "Alloy"}]})
    detail = call(env, "capabilities", {"target": "tts"})["structuredContent"]
    assert detail["inputs"]["required"] == ["input"] and detail["voices"] == {"default": "alloy", "ids": ["alloy"]}


def test_unknown_target_lists_valid_ones(env):
    result = call(env, "capabilities", {"target": "gpt-image-9"})
    assert result["isError"] is True
    assert "krea" in result["structuredContent"]["targets"]


# --------------------------------------------------------------------- run


def _image_reply(body):
    return httpx.Response(200, json={"data": [{"url": f"https://gw.test/v1/assets/img_{'b' * 64}/content?sig=1"}]})


def test_run_sync_image_builds_the_core_request_and_links_the_result(env):
    env.core.routes[("POST", "/v1/images/generations")] = lambda kw: _image_reply(kw["json"])
    result = call(env, "run", {"target": "image", "inputs": {"prompt": "a fish", "width": 512, "height": 512, "steps": 4, "n": 2}})
    method, path, kwargs = env.core.calls[-1]
    assert (method, path) == ("POST", "/v1/images/generations")
    assert kwargs["json"] == {
        "model": "krea", "prompt": "a fish", "n": 2, "size": "512x512", "response_format": "url",
        "extensions": {"com.azizam.workflow": {"steps": 4}},
    }
    assert result["structuredContent"]["status"] == "succeeded"
    link = result["content"][1]
    assert link["type"] == "resource_link" and link["uri"].startswith("https://gw.test/v1/assets/img_")
    assert result["structuredContent"]["assets"][0]["asset_id"] == f"img_{'b' * 64}"


def test_run_never_returns_inline_base64(env):
    env.core.routes[("POST", "/v1/images/generations")] = httpx.Response(
        200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x"}})
    assert env.core.stored == [("img", ".png", len(PNG))]
    assert "b64" not in json.dumps(result) and "iVBOR" not in json.dumps(result)
    assert result["content"][1]["type"] == "resource_link"


def test_run_rejects_unknown_missing_and_mistyped_inputs_before_calling_core(env):
    result = call(env, "run", {"target": "krea", "inputs": {"width": "big", "bogus": 1}})
    problems = result["structuredContent"]["invalid_inputs"]
    assert result["isError"] is True and env.core.calls == []
    assert any("bogus" in p for p in problems) and any("prompt" in p for p in problems) and any("width" in p for p in problems)


def test_run_range_violation_is_reported(env):
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x", "steps": 99}})
    assert result["isError"] and "at most 20" in result["structuredContent"]["error"]


def test_run_refuses_a_gateway_managed_input_instead_of_ignoring_it(env):
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x", "seed": 1}})
    out = result["structuredContent"]
    assert result["isError"] and out["error"] == "The gateway sets input 'seed' itself. Remove it and try again."
    assert out["layer"] == "request" and out["input"] == "seed"


def test_run_needs_both_width_and_height(env):
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x", "width": 512}})
    assert result["isError"] and "together" in result["structuredContent"]["error"]


def test_run_mode_not_offered_is_an_error(env):
    result = call(env, "run", {"target": "ltx", "mode": "sync", "inputs": {"prompt": "x"}})
    assert result["isError"] and "not available" in result["structuredContent"]["error"]


def test_core_errors_pass_through_with_status_and_code(env):
    env.core.routes[("POST", "/v1/images/generations")] = httpx.Response(
        403, json={"error": {"message": "profile may not run this", "code": "policy_denied", "type": "permission_error"}})
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x"}})
    out = result["structuredContent"]
    assert result["isError"] and out["http_status"] == 403 and out["code"] == "policy_denied" and "profile" in out["error"]


def test_run_image_edit_sends_multipart_with_the_asset_bytes(env):
    asset = f"img_{'c' * 64}"
    env.core.assets[asset] = PNG
    env.core.routes[("POST", "/v1/images/edits")] = lambda kw: _image_reply(kw)
    call(env, "run", {"target": "qwen-edit", "inputs": {"prompt": "red hat", "source_image": f"https://gw.test/v1/assets/{asset}/content?sig=1"}})
    _, _, kwargs = env.core.calls[-1]
    assert kwargs["data"]["model"] == "qwen-edit" and kwargs["data"]["response_format"] == "url"
    name, content, mime = kwargs["files"]["image"]
    assert content == PNG and mime == "image/png"


def test_ambiguous_alias_with_an_image_input_resolves_to_the_edit_workflow(env):
    asset = f"img_{'d' * 64}"
    env.core.assets[asset] = PNG
    env.core.routes[("POST", "/v1/images/edits")] = lambda kw: _image_reply(kw)
    call(env, "run", {"target": "image", "inputs": {"prompt": "hat", "source_image": asset}})
    assert env.core.calls[-1][1] == "/v1/images/edits"


def test_run_accepts_a_data_uri_and_refuses_external_urls(env):
    env.core.routes[("POST", "/v1/images/edits")] = lambda kw: _image_reply(kw)
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    ok = call(env, "run", {"target": "qwen-edit", "inputs": {"prompt": "x", "source_image": uri}})
    assert not ok.get("isError")
    bad = call(env, "run", {"target": "qwen-edit", "inputs": {"prompt": "x", "source_image": "https://example.com/a.png"}})
    assert bad["isError"] and "external URLs" in bad["structuredContent"]["error"]


def test_run_missing_or_foreign_asset_is_an_error(env):
    asset = f"img_{'e' * 64}"
    result = call(env, "run", {"target": "qwen-edit", "inputs": {"prompt": "x", "source_image": asset}})
    out = result["structuredContent"]
    assert result["isError"] and out["error"].startswith("The file for input 'source_image' isn't available.")
    assert "expired" not in out["error"] and asset not in out["error"] and out["asset_id"] == asset


def test_core_error_without_a_message_is_unknown_not_a_code(env):
    env.core.routes[("POST", "/v1/images/generations")] = httpx.Response(500, json={"error": {"code": "boom_code"}})
    out = call(env, "run", {"target": "krea", "inputs": {"prompt": "x"}})["structuredContent"]
    assert out["error"] == "We couldn't tell where this request stopped. Try again."
    assert out["code"] == "boom_code" and out["layer"] == "unknown" and out["http_status"] == 500


def test_run_speech_returns_a_stored_audio_link(env):
    env.core.routes[("POST", "/v1/audio/speech")] = httpx.Response(200, content=b"ID3" + b"\x00" * 20, headers={"content-type": "audio/mpeg"})
    result = call(env, "run", {"target": "tts-fast", "inputs": {"input": "hello", "voice": "alloy"}})
    _, _, kwargs = env.core.calls[-1]
    assert kwargs["json"] == {"model": "tts-fast", "input": "hello", "voice": "alloy", "response_format": "mp3"}
    assert env.core.stored == [("aud", ".mp3", 23)]
    assert result["content"][1]["type"] == "resource_link"


def test_run_async_media_job_waits_then_returns_links(env):
    env.core.routes[("POST", "/gateway/v1/media/jobs")] = httpx.Response(202, json={"job_id": "mj_1", "status": "queued"})
    env.core.polls["/gateway/v1/media/jobs/mj_1"] = [{"status": "queued"}, {"status": "running", "progress": 40}, {"status": "succeeded"}]
    env.core.routes[("GET", "/gateway/v1/media/jobs/mj_1/content")] = httpx.Response(
        200, json={"data": [{"url": f"https://gw.test/v1/assets/aud_{'f' * 64}/content?sig=1", "asset_id": f"aud_{'f' * 64}", "media_type": "audio/wav"}]})
    result = call(env, "run", {"target": "sfx", "inputs": {"prompt": "door", "duration": 3}, "wait_seconds": 5})
    submit = env.core.calls[0]
    assert submit[2]["json"] == {"intent": "audio.generate", "request": {"model": "sfx", "prompt": "door", "duration": 3}}
    out = result["structuredContent"]
    assert out["status"] == "succeeded" and out["job_id"] == "mj_1" and out["assets"][0]["kind"] == "audio"


def test_run_slow_job_returns_a_job_id_after_wait(env):
    env.core.routes[("POST", "/v1/videos")] = httpx.Response(200, json={"id": "video_9", "status": "queued"})
    env.core.polls["/v1/videos/video_9"] = [{"id": "video_9", "status": "in_progress", "progress": 10}]
    out = call(env, "run", {"target": "video", "inputs": {"prompt": "waves"}, "wait_seconds": 0})["structuredContent"]
    assert out["status"] == "running" and out["job_id"] == "video_9" and out["progress"] == 10 and "next" in out
    assert env.core.calls[0][2]["json"] == {"model": "video-ltx", "prompt": "waves"}


def test_run_slow_job_waits_until_positive_deadline(monkeypatch):
    async def scenario():
        core = Core()
        core.routes[("POST", "/v1/videos")] = httpx.Response(
            200, json={"id": "video_9", "status": "queued"})
        core.polls["/v1/videos/video_9"] = [
            {"id": "video_9", "status": "in_progress", "progress": 10}]
        now = [100.0]
        sleeps = asyncio.Queue()

        async def controlled_sleep(delay):
            assert delay > 0, "S6: polling must not busy-loop"
            wake = asyncio.get_running_loop().create_future()
            await sleeps.put((delay, wake))
            await wake

        # Rebind this module's references, never mutate global asyncio/time.
        monkeypatch.setattr(mcp_server, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(mcp_server, "asyncio", SimpleNamespace(
            sleep=controlled_sleep, wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError, to_thread=asyncio.to_thread))
        deps = McpDeps(
            authenticate=lambda request, path: SimpleNamespace(identity="k"),
            call=core.call, catalog=lambda: CATALOG, aliases=lambda: ALIASES,
            store_asset=core.store_asset, read_asset=core.read_asset,
            public_base_url=lambda request: "https://gw.test", poll_interval=1.0)
        app = FastAPI()
        mcp_server.register_mcp_routes(app, deps)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gw.test"
        ) as client:
            task = asyncio.create_task(client.post("/mcp", headers={**H, "mcp-protocol-version": "2025-11-25"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "run", "arguments": {
                    "target": "video", "inputs": {"prompt": "waves"}, "wait_seconds": 5}}}))
            try:
                delay, wake = await asyncio.wait_for(sleeps.get(), timeout=1)
                assert delay == 1.0
                assert now[0] == 100.0 and not task.done()
                now[0] = 104.999
                wake.set_result(None)
                delay, wake = await asyncio.wait_for(sleeps.get(), timeout=1)
                assert delay == pytest.approx(0.001)
                assert not task.done(), "S6: running job returned before its requested deadline"
                now[0] = 105.0
                wake.set_result(None)
                response = await asyncio.wait_for(task, timeout=1)
                assert response.status_code == 200
                result = response.json()["result"]
                assert not result.get("isError", False)
                out = result["structuredContent"]
                assert (out["status"], out["job_id"], out["progress"], out["target"]) == (
                    "running", "video_9", 10, "ltx")
                assert out["next"] == "Call `job` with this job_id to keep waiting (set wait_seconds)."
                assert core.calls == [
                    ("POST", "/v1/videos", {"json": {"model": "video-ltx", "prompt": "waves"}}),
                    ("GET", "/v1/videos/video_9", {}),
                    ("GET", "/v1/videos/video_9", {}),
                    ("GET", "/v1/videos/video_9", {}),
                ]
                assert core.stored == []
                assert sleeps.empty()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)

    asyncio.run(scenario())


def test_job_returns_a_finished_video_as_a_stored_link(env):
    env.core.polls["/v1/videos/video_9"] = [{"id": "video_9", "status": "completed"}]
    env.core.routes[("GET", "/v1/videos/video_9/content")] = httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
    result = call(env, "job", {"job_id": "video_9"})
    assert result["structuredContent"]["status"] == "succeeded"
    assert env.core.stored == [("vid", ".mp4", len(MP4))]
    assert result["content"][1]["mimeType"] == "x"  # from the fake store; real store reports video/mp4


def test_job_failure_reports_the_core_reason(env):
    env.core.polls["/gateway/v1/media/jobs/mj_2"] = [{"status": "failed", "error": "out of memory"}]
    out = call(env, "job", {"job_id": "mj_2"})["structuredContent"]
    assert out["status"] == "failed" and out["error"] == "out of memory"


def test_job_cancel_uses_the_family_route(env):
    env.core.routes[("DELETE", "/v1/videos/video_3")] = httpx.Response(200, json={})
    env.core.polls["/v1/videos/video_3"] = [{"id": "video_3", "status": "failed", "error": {"message": "cancelled"}}]
    call(env, "job", {"job_id": "video_3", "cancel": True})
    assert ("DELETE", "/v1/videos/video_3") in [(m, p) for m, p, _ in env.core.calls]


def test_job_unknown_id_is_a_tool_error(env):
    result = call(env, "job", {"job_id": "mj_missing"})
    assert result["isError"] and result["structuredContent"]["http_status"] == 404


def test_wait_seconds_is_bounded(env):
    result = call(env, "run", {"target": "krea", "inputs": {"prompt": "x"}, "wait_seconds": 9999})
    assert result["isError"] and "wait_seconds" in result["structuredContent"]["error"]


def test_an_unexpected_failure_becomes_a_tool_error_without_a_traceback(env):
    async def boom(request, method, path, **kwargs):
        raise RuntimeError("secret internal detail")

    env.core.call = boom
    # the deps hold the bound method from fixture time, so rebuild the app with the failing call
    deps = McpDeps(authenticate=lambda r, p: SimpleNamespace(), call=boom, catalog=lambda: CATALOG, aliases=lambda: ALIASES,
                   store_asset=env.core.store_asset, read_asset=env.core.read_asset, public_base_url=lambda r: "https://gw.test")
    app = FastAPI()
    mcp_server.register_mcp_routes(app, deps)
    resp = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "run", "arguments": {"target": "krea", "inputs": {"prompt": "x"}}}})
    result = resp.json()["result"]
    assert result["isError"] is True and "secret" not in json.dumps(result)


def test_admission_refusal_keeps_safe_reasons_and_actions(env):
    from gateway.resource_admission_http import advise_route_admission, admission_http_response
    decision = advise_route_admission(intent="image.generate", service_available=False,
                                     service_reasons=("private other-person capacity: 98765",))
    refusal = admission_http_response(decision, wait_deadline=456)
    env.core.routes[("POST", "/gateway/v1/media/jobs")] = httpx.Response(503, json=refusal.body)
    result = call(env, "run", {"target": "krea", "mode": "async", "inputs": {"prompt": "x"}})
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["code"] == "resource_admission_denied"
    assert payload["decision"] == refusal.body["error"]["decision"]
    assert payload["decision"]["next_actions"][0]["deadline"] == 456
    assert "other-person" not in json.dumps(result)
    assert "98765" not in json.dumps(result)


def test_job_waits_through_unknown_nonterminal_state_and_exposes_phases(env):
    env.core.polls['/gateway/v1/media/jobs/mj_1'] = [
        {'status': 'cancel_requested'},
        {'status': 'canceled', 'started_at': 10, 'finished_at': 20, 'attempt_number': 1},
    ]
    out = call(env, 'job', {'job_id': 'mj_1', 'wait_seconds': 5})['structuredContent']
    assert out['status'] == 'canceled'
    assert (out['started_at'], out['finished_at'], out['attempt_number']) == (10, 20, 1)
