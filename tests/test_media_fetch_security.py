"""Public media inputs cannot read local files or connect to private services."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

import proxy
from tests.support.access import access_context  # noqa: E402
from gateway import media_fetch
from tests.media_test_bytes import mp4_bytes


def _response(status=200, *, headers=None, data=b"media"):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.1.2", "192.168.1.1",
    "169.254.169.254", "169.254.1.2", "::1", "fd00::1", "fd00:ec2::254",
    "fe80::1", "0.0.0.0", "::", "224.0.0.1", "ff02::1", "::ffff:127.0.0.1",
    "::127.0.0.1", "::ffff:0:7f00:1", "64:ff9b::7f00:1", "fec0::1",
    "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "::ffff:0:a9fe:a9fe",
])
def test_media_url_blocks_nonpublic_addresses_before_connecting(monkeypatch, address):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "")
    resolver = AsyncMock(return_value=[address])
    contacted = []
    with patch.object(media_fetch, "_resolve_addresses", resolver):
        with pytest.raises(media_fetch.MediaFetchError, match="blocked network"):
            asyncio.run(media_fetch.safe_fetch_media(
                "https://media.example/image.png",
                transport=httpx.MockTransport(lambda request: contacted.append(request)),
            ))
    assert contacted == []


def test_redirect_to_private_address_is_rejected_before_second_hop(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "")
    contacted = []

    def respond(request):
        contacted.append(request)
        return _response(302, headers={"location": "http://metadata.example/source"})

    resolver = AsyncMock(side_effect=[["93.184.216.34"], ["169.254.169.254"]])
    with patch.object(media_fetch, "_resolve_addresses", resolver):
        with pytest.raises(media_fetch.MediaFetchError, match="blocked network"):
            asyncio.run(media_fetch.safe_fetch_media(
                "https://media.example/image.png", transport=httpx.MockTransport(respond),
            ))
    assert len(contacted) == 1
    assert resolver.await_count == 2


@pytest.mark.parametrize("redirect", [False, True])
def test_public_media_reference_uses_safe_fetch_boundary(monkeypatch, redirect):
    contacted = []

    def respond(request):
        contacted.append(request)
        if redirect and len(contacted) == 1:
            return _response(302, headers={"location": "http://169.254.169.254/private.png"})
        return _response(data=b"private service contents")

    client_class = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", client)
    monkeypatch.setattr(proxy, "_resolve_gateway_media_url", AsyncMock(return_value=None))
    monkeypatch.setattr(media_fetch, "_resolve_addresses", AsyncMock(
        side_effect=[["93.184.216.34"], ["169.254.169.254"]] if redirect else [["127.0.0.1"]],
    ))
    url = "https://media.example/source.png" if redirect else "http://127.0.0.1/private.png"
    with pytest.raises(proxy._InputReferenceError, match="blocked network"):
        asyncio.run(proxy._resolve_input_reference(url, access_context()))
    assert len(contacted) == (1 if redirect else 0)


def test_dns_rebinding_cannot_change_the_connected_host(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "")
    contacted = []

    def respond(request):
        contacted.append(request)
        return _response()

    # A second hostname lookup would return loopback. The transport sees a
    # literal checked IP, so there is no second hostname lookup to rebind.
    resolver = AsyncMock(side_effect=[["93.184.216.34"], ["127.0.0.1"]])
    with patch.object(media_fetch, "_resolve_addresses", resolver):
        result = asyncio.run(media_fetch.safe_fetch_media(
            "https://media.example:8443/image.png", transport=httpx.MockTransport(respond),
        ))
    assert result.data == b"media"
    assert resolver.await_count == 1
    assert contacted[0].url.host == "93.184.216.34"
    assert contacted[0].headers["host"] == "media.example:8443"
    assert contacted[0].extensions["sni_hostname"] == "media.example"


def test_nat64_public_ipv4_destination_still_works():
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["64:ff9b::5db8:d822"])):
        result = asyncio.run(media_fetch.safe_fetch_media(
            "https://media.example/source", transport=httpx.MockTransport(lambda request: _response()),
        ))
    assert result.data == b"media"


def test_fetch_falls_back_only_to_previously_checked_addresses():
    contacted = []

    def respond(request):
        contacted.append(request.url.host)
        if len(contacted) == 1:
            raise httpx.ConnectError("first public address unavailable", request=request)
        return _response()

    resolver = AsyncMock(return_value=["2606:4700:4700::1111", "93.184.216.34"])
    with patch.object(media_fetch, "_resolve_addresses", resolver):
        result = asyncio.run(media_fetch.safe_fetch_media(
            "https://media.example/source", transport=httpx.MockTransport(respond),
        ))
    assert result.data == b"media"
    assert contacted == ["2606:4700:4700::1111", "93.184.216.34"]
    assert resolver.await_count == 1


def test_mixed_public_private_dns_answers_fail_closed(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "")
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["93.184.216.34", "10.0.0.1"])):
        with pytest.raises(media_fetch.MediaFetchError, match="blocked network"):
            asyncio.run(media_fetch.safe_fetch_media("https://media.example/a"))


def test_operator_can_allow_exact_lan_host_but_not_metadata(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "storage.lan,metadata.lan")
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["192.168.1.2"])):
        result = asyncio.run(media_fetch.safe_fetch_media(
            "http://storage.lan/source", transport=httpx.MockTransport(lambda request: _response()),
        ))
    assert result.data == b"media"
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["fd00:ec2::254"])):
        with pytest.raises(media_fetch.MediaFetchError, match="blocked network"):
            asyncio.run(media_fetch.safe_fetch_media("http://metadata.lan/source"))


def test_media_download_cap_applies_without_content_length(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_MAX_BYTES", "4")
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["93.184.216.34"])):
        with pytest.raises(media_fetch.MediaFetchError, match="byte limit"):
            asyncio.run(media_fetch.safe_fetch_media(
                "https://media.example/source", transport=httpx.MockTransport(lambda request: _response(data=b"12345")),
            ))


def test_media_download_rejects_declared_oversize_without_reading(monkeypatch):
    monkeypatch.setenv("AZIZAM_MEDIA_FETCH_MAX_BYTES", "4")

    class UnreadableStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("oversized response must not be read")
            yield b""

    response = httpx.Response(200, headers={"content-length": "5"}, stream=UnreadableStream())
    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["93.184.216.34"])):
        with pytest.raises(media_fetch.MediaFetchError, match="byte limit"):
            asyncio.run(media_fetch.safe_fetch_media(
                "https://media.example/source", transport=httpx.MockTransport(lambda request: response),
            ))


def test_media_redirect_loop_is_bounded():
    contacted = []

    def respond(request):
        contacted.append(request)
        return _response(302, headers={"location": "/another"})

    with patch.object(media_fetch, "_resolve_addresses", AsyncMock(return_value=["93.184.216.34"])):
        with pytest.raises(media_fetch.MediaFetchError, match="redirect limit"):
            asyncio.run(media_fetch.safe_fetch_media("https://media.example/source", transport=httpx.MockTransport(respond)))
    assert len(contacted) == media_fetch.MAX_REDIRECTS + 1


def test_video_create_rejects_existing_filesystem_path_with_400(tmp_path):
    image = tmp_path / "private.png"
    image.write_bytes(b"private file contents")
    request = SimpleNamespace(
        headers={"content-type": "application/json"},
        json=AsyncMock(return_value={"prompt": "x", "seconds": 2, "input_reference": str(image)}),
    )
    with patch.object(proxy, "_service_availability_response", return_value=None):
        with patch.object(proxy.httpx, "AsyncClient", side_effect=AssertionError("must not upload a local file")):
            response = asyncio.run(proxy.proxy_comfy_video_create(request, access_context()))
    assert response.status_code == 400
    message = json.loads(response.body)["error"]["message"]
    assert "multipart upload" in message
    assert "data:image/...;base64" in message


def test_jpeg_base64_starting_with_slash_remains_accepted():
    image = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(image, format="JPEG")
    encoded = base64.b64encode(image.getvalue()).decode("ascii")
    assert encoded.startswith("/")
    data, _ = asyncio.run(proxy._resolve_input_reference(encoded))
    assert data == image.getvalue()


@pytest.mark.parametrize("route", ["images", "assets", "gateway/assets"])
@pytest.mark.parametrize("host", [
    "ai.vivid.fish", "openai.vivid.fish", "gateway.lan",
    "localhost", "192.168.100.10", "unconfigured.example",
])
def test_gateway_signed_image_url_resolves_without_network_with_ownership(monkeypatch, tmp_path, route, host):
    monkeypatch.setenv("AZIZAM_PUBLIC_URL", "http://gateway.lan")
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    image = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(image, format="PNG")
    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    other = SimpleNamespace(identity=SimpleNamespace(key_hash="other"))
    asset_id = proxy.store_image_bytes(image.getvalue(), owner_key_hash="owner")
    request = SimpleNamespace(headers={"host": host}, url=SimpleNamespace(scheme="http", netloc=host))
    builder = proxy.build_signed_image_url if route == "images" else proxy.build_signed_asset_url
    url = builder(request, asset_id, "none")
    if route == "gateway/assets":
        url = url.replace("/v1/assets/", "/gateway/v1/assets/")
    with patch.object(media_fetch, "_resolve_addresses", side_effect=AssertionError("no network for own media")):
        data, filename = asyncio.run(proxy._resolve_input_reference(url, owner, request=request))
        assert data == image.getvalue()
        assert filename.endswith(".png")
        with pytest.raises(proxy._InputReferenceError, match="not accessible"):
            asyncio.run(proxy._resolve_input_reference(url, other, request=request))
        with pytest.raises(proxy._InputReferenceError, match="signature"):
            asyncio.run(proxy._resolve_input_reference(url.replace("sig=", "sig=invalid"), owner, request=request))


def test_gateway_video_asset_signed_url_resolves_internally(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    asset_id = proxy.store_asset_bytes(mp4_bytes(), kind="vid", suffix=".mp4", owner_key_hash="owner")
    clean = proxy.asset_cache_path(asset_id).read_bytes()
    request = SimpleNamespace(headers={"host": "ai.vivid.fish"}, url=SimpleNamespace(scheme="https", netloc="ai.vivid.fish"))
    url = proxy.build_signed_asset_url(request, asset_id, "none")
    with patch.object(media_fetch, "_resolve_addresses", side_effect=AssertionError("no network for own media")):
        data, filename, mime = asyncio.run(proxy._resolve_video_workflow_asset(url, kind="video", param="source_video", auth_context=owner))
    assert data == clean
    assert filename.endswith(".mp4")
    assert mime == "video/mp4"


def test_forwarded_host_minted_signed_asset_resolves_internally(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    asset_id = proxy.store_asset_bytes(mp4_bytes(), kind="vid", suffix=".mp4", owner_key_hash="owner")
    clean = proxy.asset_cache_path(asset_id).read_bytes()
    request = SimpleNamespace(headers={
        "host": "internal.lan:8080", "x-forwarded-host": "localhost:8081",
    }, url=SimpleNamespace(scheme="http", netloc="internal.lan:8080"))
    url = proxy.build_signed_asset_url(request, asset_id, "none")
    with patch.object(media_fetch, "_resolve_addresses", side_effect=AssertionError("no network for own media")):
        result = asyncio.run(proxy._fetch_media_url(url, owner, request=request))
    assert result.data == clean


def test_redirect_into_gateway_signed_asset_uses_internal_resolver(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    asset_id = proxy.store_asset_bytes(mp4_bytes(), kind="vid", suffix=".mp4", owner_key_hash="owner")
    clean = proxy.asset_cache_path(asset_id).read_bytes()
    request = SimpleNamespace(headers={"host": "ai.vivid.fish"}, url=SimpleNamespace(scheme="https", netloc="ai.vivid.fish"))
    signed_url = proxy.build_signed_asset_url(request, asset_id, "none")
    resolver = AsyncMock(return_value=["93.184.216.34"])
    with patch.object(media_fetch, "_resolve_addresses", resolver):
        result = asyncio.run(media_fetch.safe_fetch_media(
            "https://media.example/source",
            internal_resolver=lambda url: proxy._resolve_gateway_media_url(url, owner),
            transport=httpx.MockTransport(lambda request: _response(302, headers={"location": signed_url})),
        ))
    assert result.data == clean
    assert resolver.await_count == 1


def test_saved_instance_base_url_identifies_internal_signed_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    monkeypatch.setattr(proxy, "instance_settings", lambda *args: {"base_url": "http://instance.lan"})
    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    asset_id = proxy.store_asset_bytes(mp4_bytes(), kind="vid", suffix=".mp4", owner_key_hash="owner")
    clean = proxy.asset_cache_path(asset_id).read_bytes()
    request = SimpleNamespace(headers={"host": "instance.lan"}, url=SimpleNamespace(scheme="http", netloc="instance.lan"))
    signed_url = proxy.build_signed_asset_url(request, asset_id, "none")
    with patch.object(media_fetch, "_resolve_addresses", side_effect=AssertionError("no network for own media")):
        result = asyncio.run(proxy._fetch_media_url(signed_url, owner))
    assert result.data == clean


def test_legacy_gateway_video_url_checks_ownership_and_streams_under_cap(monkeypatch):
    from _fake_comfy_video import FakeComfy, SOURCE_BYTES, SOURCE_PROMPT_ID

    owner = SimpleNamespace(identity=SimpleNamespace(key_hash="owner"))
    other = SimpleNamespace(identity=SimpleNamespace(key_hash="other"))
    monkeypatch.setitem(proxy._video_job_records, SOURCE_PROMPT_ID, {"owner_key_hash": "owner"})
    comfy = FakeComfy()
    monkeypatch.setattr(proxy.httpx, "AsyncClient", comfy.client_class())
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *args: {})
    url = f"https://ai.vivid.fish/v1/videos/video_{SOURCE_PROMPT_ID}/content"
    with patch.object(media_fetch, "_resolve_addresses", side_effect=AssertionError("no public URL network")):
        fetched = asyncio.run(proxy._fetch_media_url(url, owner))
        assert fetched.data == SOURCE_BYTES
        before = list(comfy.gets)
        with pytest.raises(media_fetch.MediaFetchError, match="not accessible"):
            asyncio.run(proxy._fetch_media_url(url, other))
        assert comfy.gets == before
        monkeypatch.setenv("AZIZAM_MEDIA_FETCH_MAX_BYTES", "4")
        with pytest.raises(proxy._VideoAssetError, match="byte limit"):
            asyncio.run(proxy._load_completed_video_bytes(f"video_{SOURCE_PROMPT_ID}", owner))
