"""A chat image part can name a stored asset instead of carrying the bytes.

``{"type": "image_url", "image_url": {"url": "img_<sha256>"}}`` is resolved by the gateway, after the same owner check the
Library uses, into the data URI an engine already accepts. An asset the caller cannot read is a 400 that names the part.
``POST /v1/assets`` stores a large image outside the JSON body cap and returns the id.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json

from unittest.mock import AsyncMock

import httpx
import pytest

import proxy
from gateway import admin_store
from gateway.request_body_limits import RequestBodyLimits
from tests.support.fake_upstream import FakeUpstreams, fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.support.gateway import ResponseTap, gateway_client, register_litellm, use_admin_db

MODEL = "fk-model"
_OK = {"id": "c", "object": "chat.completion", "created": 1, "model": MODEL,
       "choices": [{"index": 0, "message": {"role": "assistant", "content": "A cat."}, "finish_reason": "stop"}]}


def _png(size: int = 200) -> bytes:
    """Noise, so the file is large enough that a data URI of it is bigger than the request that names it."""
    from PIL import Image
    import os
    image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class _World:
    def __init__(self, fake_upstreams, monkeypatch, tmp_path):
        fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", ResponseTap(proxy.app))
        # Which local model serves a picture is another test's question; here the request goes on to the engine as it is.
        monkeypatch.setattr(proxy, "_prepare_litellm_local_llm_request",
                            AsyncMock(side_effect=lambda *args, **kwargs: (None, kwargs["body_json"])))
        self.db = use_admin_db(monkeypatch, tmp_path)
        monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
        data = {"protocol": "openai-compatible", "name": "asset-refs", "provenance": {"source": "hand-written", "reference": "asset refs"},
                "exchanges": [{"id": "chat", "match": {"method": "POST", "path": "/v1/chat/completions"},
                               "response": {"status": 200, "json": _OK}, "required": False, "repeat": True}]}
        self.engine = fake_upstreams.asgi(scenario_from_data(data), host="litellm.fk.test")
        register_litellm(monkeypatch, self.db, self.engine.base_url)
        with admin_store._conn(self.db) as conn:
            self.app_id = conn.execute("SELECT id FROM apps ORDER BY created_at LIMIT 1").fetchone()[0]

    def store(self, data: bytes, *, owner: str | None = None) -> str:
        return proxy.store_asset_bytes(data, kind="img", suffix=".png", owner_key_hash=owner or f"app:{self.app_id}",
                                       maker_person_id=None, keep_prompt=False)

    def post(self, path: str, **options) -> httpx.Response:
        async def call() -> httpx.Response:
            async with asyncio.timeout(30), gateway_client() as client:
                return await client.post(path, **options)
        return asyncio.run(call())

    def chat(self, *parts) -> tuple[httpx.Response, int]:
        body = {"model": MODEL, "messages": [{"role": "user", "content": [{"type": "text", "text": "What is this?"}, *parts]}]}
        encoded = json.dumps(body).encode()
        return self.post("/v1/chat/completions", content=encoded, headers={"content-type": "application/json"}), len(encoded)

    def engine_bodies(self) -> list[dict]:
        return [call.json for call in self.engine.calls("chat")]


@pytest.fixture
def world(fake_upstreams, monkeypatch, tmp_path):
    return _World(fake_upstreams, monkeypatch, tmp_path)


def _ref(asset_id: str, **extra) -> dict:
    return {"type": "image_url", "image_url": {"url": asset_id, **extra}}


def test_inline_data_uri_still_reaches_the_engine_unchanged(world):
    uri = "data:image/png;base64," + base64.b64encode(_png(16)).decode()
    response, _ = world.chat({"type": "image_url", "image_url": {"url": uri}})
    assert response.status_code == 200, response.text
    assert world.engine_bodies()[0]["messages"][0]["content"][1]["image_url"]["url"] == uri


def test_an_asset_reference_reaches_the_engine_as_a_data_uri_of_the_stored_bytes(world):
    asset_id = world.store(_png())
    stored = proxy.asset_cache_path(asset_id).read_bytes()
    response, request_size = world.chat(_ref(asset_id, detail="low"))
    assert response.status_code == 200, response.text
    (sent,) = world.engine_bodies()
    text, image = sent["messages"][0]["content"]
    assert text == {"type": "text", "text": "What is this?"}
    assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(stored).decode()
    assert image["image_url"]["detail"] == "low"
    # The caller's request stays small: it holds an id, not the picture.
    assert request_size < 1024 < len(stored)


def test_an_unknown_asset_is_a_400_that_names_the_part_and_reaches_no_engine(world):
    response, _ = world.chat(_ref("img_" + "0" * 64))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "asset_missing"
    assert error["param"] == "messages[0].content[1].image_url.url"
    assert world.engine_bodies() == []


def test_another_owners_asset_answers_like_an_unknown_one(world):
    foreign = world.store(_png(), owner="app:someone-else")
    refused, _ = world.chat(_ref(foreign))
    unknown, _ = world.chat(_ref("img_" + "1" * 64))
    assert refused.status_code == 400 and world.engine_bodies() == []
    normalized = lambda body, asset: json.loads(json.dumps(body).replace(asset, "X"))  # noqa: E731
    assert normalized(refused.json(), foreign) == normalized(unknown.json(), "img_" + "1" * 64)


def test_a_value_that_is_neither_a_url_nor_an_asset_id_is_refused_not_forwarded(world):
    response, _ = world.chat(_ref("img_not-an-id"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"
    assert response.json()["error"]["param"] == "messages[0].content[1].image_url.url"
    assert world.engine_bodies() == []


def test_upload_then_reference_carries_a_large_image_outside_the_json_body(world):
    image = _png(600)
    uploaded = world.post("/v1/assets", files={"file": ("crop.png", image, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    meta = uploaded.json()
    assert meta["object"] == "gateway.asset" and meta["media_type"] == "image/png"
    assert meta["id"].startswith("img_") and meta["size_bytes"] > 0
    response, request_size = world.chat(_ref(meta["id"]))
    assert response.status_code == 200, response.text
    assert request_size < 1024
    sent = world.engine_bodies()[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert sent.startswith("data:image/png;base64,") and len(sent) > len(image)


def test_upload_refuses_what_is_not_an_image(world):
    refused = world.post("/v1/assets", files={"file": ("notes.txt", b"not a picture", "text/plain")})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_image_format"
    assert world.post("/v1/assets", files={"file": ("e.png", b"", "image/png")}).status_code == 400
    assert world.post("/v1/assets", data={"x": "y"}).status_code == 400


def test_upload_larger_than_the_media_input_limit_is_refused(world, monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_MAX_BYTES", "1000")
    refused = world.post("/v1/assets", files={"file": ("big.png", _png(100), "image/png")})
    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == "file_too_large"


def test_the_upload_route_has_the_media_body_limit_and_chat_keeps_the_json_limit():
    limits = RequestBodyLimits()
    assert limits.for_path("/v1/assets") == limits.media_bytes
    assert limits.for_path("/openai/v1/assets") == limits.media_bytes
    assert limits.for_path("/v1/chat/completions") == limits.json_bytes


# -- the resolver on its own -------------------------------------------------------------------------------------------

def _resolve(messages, reader):
    from gateway import chat_asset_refs
    return chat_asset_refs.resolve(messages, reader)


def test_only_asset_shaped_urls_are_resolved():
    seen = []
    reader = lambda asset_id: seen.append(asset_id) or _png(8)  # noqa: E731
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.test/cat.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "img_" + "a" * 64},
    ]}, {"role": "assistant", "content": "plain text"}]
    resolved, error = _resolve(messages, reader)
    assert error is None and resolved == messages and seen == []


def test_a_string_image_url_is_resolved_and_the_input_is_not_mutated():
    asset_id = "img_" + "b" * 64
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": asset_id}]}]
    resolved, error = _resolve(messages, lambda _id: _png(8))
    assert error is None
    assert resolved[0]["content"][0]["image_url"].startswith("data:image/png;base64,")
    assert messages[0]["content"][0]["image_url"] == asset_id


def test_bytes_that_are_not_an_image_are_refused():
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "img_" + "c" * 64}}]}]
    _, error = _resolve(messages, lambda _id: b"RIFF....WAVEfmt ")
    assert error is not None and error.code == "invalid_image_format"
    assert error.param == "messages[0].content[0].image_url.url"
