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

def test_asset_id_shape_at_and_around_the_boundary():
    """Non-string URLs pass through; only exact lowercase asset IDs are resolved."""
    asset_id = "img_" + "a" * 64
    seen = []
    data = _png(8)

    def read(key):
        seen.append(key)
        return data

    non_string = [{"content": [{"type": "image_url", "image_url": {"url": 7}}]}]
    assert _resolve(non_string, read) == (non_string, None)
    messages = [{"content": [_ref(asset_id)]}]
    resolved, error = _resolve(messages, read)
    assert error is None
    assert resolved[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen == [asset_id]
    for invalid in (asset_id + "a", "img_A" + "a" * 63):
        messages = [{"content": [_ref(invalid)]}]
        resolved, error = _resolve(messages, read)
        assert resolved == messages
        assert error is not None
        assert (error.code, error.param) == ("invalid_image_url", "messages[0].content[0].image_url.url")
    assert seen == [asset_id]


def test_duplicate_refs_cache_only_within_one_resolve():
    """Two parts with the same stored ID use one read per resolve."""
    asset_id = "img_" + "d" * 64
    data = _png(8)
    seen = []

    def read(key):
        seen.append(key)
        return data

    messages = [{"content": [_ref(asset_id), _ref(asset_id)]}]
    for call_count in (1, 2):
        resolved, error = _resolve(messages, read)
        assert error is None
        first, second = resolved[0]["content"]
        assert first["image_url"] == second["image_url"]
        assert first["image_url"]["url"].startswith("data:image/png;base64,")
        assert seen == [asset_id] * call_count


def test_later_string_ref_names_its_message_and_shape_on_refusal():
    """A string-form reference in a later message ends the param at image_url."""
    missing = "img_" + "e" * 64
    messages = [{"content": "earlier"}, {"content": [
        {"type": "text", "text": "later"}, {"type": "image_url", "image_url": missing},
    ]}]
    resolved, error = _resolve(messages, lambda _id: None)
    assert resolved == messages
    assert error is not None
    assert (error.code, error.param) == ("asset_missing", "messages[1].content[1].image_url")


def test_a_later_refusal_discards_a_successful_image_replacement():
    """A successful earlier image part does not leak into the refused result."""
    given = "img_" + "a" * 64
    missing = "img_" + "b" * 64
    data = _png(8)
    seen = []

    def read(key):
        seen.append(key)
        return data if key == given else None

    messages = [{"content": [_ref(given), _ref(missing)]}]
    resolved, error = _resolve(messages, read)
    assert error is not None
    assert (error.code, error.param) == ("asset_missing", "messages[0].content[1].image_url.url")
    assert seen == [given, missing]
    assert resolved == messages == [{"content": [_ref(given), _ref(missing)]}]


@pytest.mark.parametrize(("format_name", "media_type"), [("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_stored_image_ref_preserves_non_png_media_type_and_bytes(format_name, media_type):
    """Stored JPEG and WebP data URIs preserve the detected type and exact bytes."""
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 40, 60)).save(output, format=format_name)
    data = output.getvalue()
    asset_id = "img_" + "f" * 64
    resolved, error = _resolve([{"content": [_ref(asset_id)]}], lambda _id: data)
    assert error is None
    uri = resolved[0]["content"][0]["image_url"]["url"]
    assert uri == "data:" + media_type + ";base64," + base64.b64encode(data).decode("ascii")


def test_one_request_cannot_name_more_images_than_the_limit(monkeypatch):
    monkeypatch.setattr("gateway.chat_asset_refs.MAX_REFERENCES", 3)
    asset_id = "img_" + "f" * 64
    data = _png(8)
    at_limit = [{"content": [_ref(asset_id)] * 3}]
    resolved, error = _resolve(at_limit, lambda _id: data)
    assert error is None and len(resolved[0]["content"]) == 3
    over = [{"content": [_ref(asset_id)] * 3}, {"content": [_ref(asset_id)]}]
    resolved, error = _resolve(over, lambda _id: data)
    assert resolved == over
    assert error is not None
    assert (error.code, error.param) == ("too_many_images", "messages[1].content[0].image_url.url")


def test_the_stored_bytes_one_request_swaps_in_are_counted_per_reference(monkeypatch):
    data = _png(8)
    monkeypatch.setattr("gateway.chat_asset_refs.MAX_STORED_BYTES", len(data) * 2)
    asset_id = "img_" + "9" * 64
    reads = []

    def read(key):
        reads.append(key)
        return data

    resolved, error = _resolve([{"content": [_ref(asset_id)] * 2}], read)
    assert error is None and len(resolved[0]["content"]) == 2
    # The same picture a third time still adds its bytes to the request, though it is read once.
    over = [{"content": [_ref(asset_id)] * 3}]
    resolved, error = _resolve(over, read)
    assert resolved == over
    assert error is not None
    assert (error.code, error.param) == ("request_too_large", "messages[0].content[2].image_url.url")
    assert reads == [asset_id, asset_id]


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_a_stored_image_is_swapped_in_whatever_the_role_of_its_message(role):
    asset_id = "img_" + "b" * 64
    resolved, error = _resolve([{"role": role, "content": [_ref(asset_id)]}], lambda key: _png(8))
    assert error is None
    assert resolved[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_an_id_one_hex_digit_short_is_refused_and_never_read():
    seen = []
    messages = [{"role": "user", "content": [_ref("img_" + "a" * 63)]}]
    resolved, error = _resolve(messages, lambda key: seen.append(key))
    assert resolved == messages and seen == []
    assert (error.code, error.param) == ("invalid_image_url", "messages[0].content[0].image_url.url")


def test_an_id_inside_a_text_part_or_a_nested_list_is_left_as_it_came():
    asset_id = "img_" + "c" * 64
    seen = []
    messages = [{"role": "user", "content": [{"type": "text", "text": asset_id}, [_ref(asset_id)]]}]
    resolved, error = _resolve(messages, lambda key: seen.append(key))
    assert (resolved, error, seen) == (messages, None, [])
