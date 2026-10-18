"""The differential harness fails when the gateway's behaviour changes.

Each test replays the committed ``image_sync_b64`` scenario with one small change made to the
gateway at run time, and requires the golden compare to fail at the part of the trace that
change touches. A harness that passed these would prove nothing about a refactor.
"""
from __future__ import annotations

import io

import pytest

import proxy
from tests.support.adapter_differential_rig import comfy_exchanges, play, scenario


def _png(color=(200, 40, 40)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


def replay_with(tmp_path, change, *, golden: str = "image_sync_b64") -> str:
    """Run the golden's scenario with ``change(rig)`` applied; return the failure message (fails if none)."""
    def setup(rig):
        fake = rig.engine("comfy", scenario("comfyui", "sensitivity", comfy_exchanges(views=[(_png(), "image/png")])),
                          host="comfy.fk.test")
        rig.set(proxy, "COMFY_HOST", fake.base_url)
        change(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate one image as b64_json",
                       json_body={"prompt": "a red fox", "response_format": "b64_json", "background": "opaque"})

    with pytest.raises(pytest.fail.Exception) as failure:
        play(golden, tmp_path, lambda *_: None, setup, drive,
             description="POST /v1/images/generations, one image, base64 in the response")
    return str(failure.value)


def test_the_unchanged_gateway_matches_the_golden(tmp_path):
    """The control: with no change the same replay passes, so a failure below comes from the change."""
    def setup(rig):
        fake = rig.engine("comfy", scenario("comfyui", "sensitivity", comfy_exchanges(views=[(_png(), "image/png")])),
                          host="comfy.fk.test")
        rig.set(proxy, "COMFY_HOST", fake.base_url)

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate one image as b64_json",
                       json_body={"prompt": "a red fox", "response_format": "b64_json", "background": "opaque"})

    play("image_sync_b64", tmp_path, lambda *_: None, setup, drive,
         description="POST /v1/images/generations, one image, base64 in the response")


def test_an_extra_field_in_the_engine_request_body_is_caught(tmp_path):
    real = proxy._build_graph_submit_payload

    def change(rig):
        rig.set(proxy, "_build_graph_submit_payload", lambda workflow, **kw: {**real(workflow, **kw), "front": True})

    message = replay_with(tmp_path, change)
    assert "engine_requests" in message and ".body.text" in message


def test_a_different_engine_request_header_is_caught(tmp_path):
    def change(rig):
        rig.set(proxy, "configured_downstream_headers", lambda *_a, **_k: {"x-adapter": "1"})

    message = replay_with(tmp_path, change)
    assert "engine_requests" in message and "x-adapter" in message


def test_one_engine_request_more_or_fewer_is_caught(tmp_path):
    async def no_stats(_client):
        return None

    message = replay_with(tmp_path, lambda rig: rig.set(proxy, "graph_free_vram_bytes", no_stats))
    assert "engine_requests" in message


def test_a_different_response_to_the_caller_is_caught(tmp_path):
    real = proxy.encode_image_bytes

    def change(rig):
        # another valid picture in place of the engine's (a stray trailing byte would be stripped by the gateway itself)
        rig.set(proxy, "encode_image_bytes", lambda raw, *a, **k: real(_png((1, 2, 3)), *a, **k))

    message = replay_with(tmp_path, change)
    assert "caller" in message or "assets" in message


def test_a_different_receipt_is_caught(tmp_path):
    real = proxy.finish_image_route_receipt

    def finish(receipt, response, **kw):
        receipt.metadata["extra"] = "changed"
        return real(receipt, response, **kw)

    message = replay_with(tmp_path, lambda rig: rig.set(proxy, "finish_image_route_receipt", finish))
    assert "receipts" in message


def test_a_different_stored_asset_is_caught(tmp_path):
    real = proxy.store_image_bytes

    # store the same asset under another suffix: the suffix is part of the asset listing
    def store_suffix(content, *, owner_key_hash=None, suffix=".png"):
        return real(content, owner_key_hash=owner_key_hash, suffix=".webp")

    message = replay_with(tmp_path, lambda rig: rig.set(proxy, "store_image_bytes", store_suffix))
    assert "assets" in message or "caller" in message
