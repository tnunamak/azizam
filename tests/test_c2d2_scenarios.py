"""C2D2 route oracles, partial: S3, S6 (expected to fail until built), and the typo half of S7.

S1 requires the file-input description and Library upload/reference contracts.
S2's multi-result fetch grammar exists; its ordered pair scenario is covered here.
S4/S5 require the owner alias write route and target/fallback schema.
The other half of S7 requires the audio job request shape; the smell JSON
fixture alone does not establish that contract. Do not replace these gaps
with unconditional xfails or product-function monkeypatches.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from tests.support.fake_upstream import fake_upstreams  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway  # noqa: F401
from tests.test_owner_engine_chain_smell import (
    HOST, MOLECULE, SMELL, SmellEngine, _add_engine, _app_key, _enabled_engine,
    _install_description, _write_headers, owner, smell_engine,
)  # noqa: F401 - imported fixtures are deliberately registered here

# A real, tiny PNG, not JSON wearing an image content type.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII="
)
VECTOR = [0.25, -1.5, 3.0, 0.0]


class CapabilityGap(AssertionError):
    """Only the named missing product capability may satisfy an xfail."""


class PngEngine(SmellEngine):
    def __init__(self, content=PNG, media_type="image/png"):
        super().__init__()
        self.result_content = content
        self.result_media_type = media_type

    async def __call__(self, scope, receive, send):
        if scope["method"] == "GET" and scope["path"] == "/v1/infer/smell-1/molecule":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", self.result_media_type.encode()),
                                    (b"content-length", str(len(self.result_content)).encode())]})
            await send({"type": "http.response.body", "body": self.result_content})
            return
        await super().__call__(scope, receive, send)


def _headers(key):
    # Fixture-generated credential; no actual external account or model API.
    return {"Authorization": "Bearer " + key}


def _job(public, key, engine):
    response = public.post("/gateway/v1/media/jobs", headers=_headers(key),
                           json={"intent": "smell.infer", "model": "smell-1",
                                 "input": {"vector": VECTOR}})
    assert response.status_code == 202, response.text
    submitted = response.json()
    for _ in range(100):
        response = public.get("/gateway/v1/media/jobs/" + submitted["id"],
                              headers=_headers(key))
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    assert job["status"] == "succeeded", job
    assert engine.posts == [{"input": {"vector": VECTOR}}]
    return submitted, job


class PairEngine(SmellEngine):
    """The existing synthetic wire, with two ordered result handles."""

    def __init__(self):
        super().__init__()
        self.fetches = []
        self.wav = bytes.fromhex(
            "524946462600000057415645666d74201000000001000100"
            "401f0000803e00000200100064617461020000000000"
        )

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        responses = {
            "/v1/infer/smell-1": (
                json.dumps({"state": "ready", "files": ["a", "b"]}).encode(),
                "application/json",
            ),
            "/v1/infer/smell-1/files/a": (PNG, "image/png"),
            "/v1/infer/smell-1/files/b": (self.wav, "audio/wav"),
        }
        if scope["method"] == "GET" and path in responses:
            content, media_type = responses[path]
            if "/files/" in path:
                self.fetches.append(path)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", media_type.encode()),
                                    (b"content-length", str(len(content)).encode())]})
            await send({"type": "http.response.body", "body": content})
            return
        await super().__call__(scope, receive, send)


def test_s2_one_job_returns_png_and_wav_in_engine_order(owner, gateway, fake_upstreams):
    client, _, owner_id, plan_id = owner
    installed = client.put(
        "/admin/api/engine-protocols/d0-smell-pair",
        content=SMELL.with_name("smell-pair.yml").read_text(encoding="utf-8"),
        headers={**csrf_for(client), "Content-Type": "application/yaml",
                 "If-None-Match": "*"},
    )
    assert installed.status_code == 201, installed.text
    engine = PairEngine()
    fake_upstreams.router.add("127.0.0.1:18302", engine)
    created = _add_engine(
        client, capabilities=["smell.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": "http://127.0.0.1:18302", "dispatch": "protocol",
                "protocol_description": "d0-smell-pair"},
    )
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        assert engine.fetches == ["/v1/infer/smell-1/files/a", "/v1/infer/smell-1/files/b"]
        assert len(job["assets"]) == 2, job
        assert [(asset["kind"], asset["media_type"], asset["size_bytes"])
                for asset in job["assets"]] == [
                    ("image", "image/png", len(PNG)),
                    ("audio", "audio/wav", len(engine.wav)),
                ], job


def test_s3_one_png_is_an_image_asset(owner, gateway, fake_upstreams):
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    engine = PngEngine()
    fake_upstreams.router.add("127.0.0.1:18302", engine)
    created = _add_engine(
        client, capabilities=["smell.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": "http://127.0.0.1:18302", "dispatch": "protocol",
                "protocol_description": "d0-smell"},
    )
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200, content.text
        assert content.content == PNG
        assert len(job["assets"]) == 1, job
        asset = job["assets"][0]
        assert asset["media_type"] == "image/png", asset
        assert asset["size_bytes"] == len(PNG), asset
        assert asset["kind"] != "file", "a fetched PNG must have kind=image, not file"
        assert asset["kind"] == "image", asset


@pytest.mark.parametrize(
    "payload, media_type, expected_kind",
    [
        pytest.param(
            # Mono PCM WAV with one silent sample.
            bytes.fromhex(
                "524946462600000057415645666d74201000000001000100"
                "401f0000803e00000200100064617461020000000000"
            ),
            "audio/wav", "audio", id="wav",
        ),
        pytest.param(
            b"\x00unclassified-result\xff\x01",
            "application/octet-stream", "file", id="unknown",
        ),
    ],
)
def test_s3_non_image_results_keep_bytes_type_size_and_kind(
    owner, gateway, fake_upstreams, payload, media_type, expected_kind,
):
    # Partial coverage: successful MP4/GLB fixtures still require qualification.
    # Do not turn failed jobs into passes or replace storage with a mock.
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    engine = PngEngine(content=payload, media_type=media_type)
    fake_upstreams.router.add("127.0.0.1:18302", engine)
    created = _add_engine(
        client, capabilities=["smell.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": "http://127.0.0.1:18302", "dispatch": "protocol",
                "protocol_description": "d0-smell"},
    )
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200, content.text
        assert content.content == payload
        assert len(job["assets"]) == 1, job
        asset = job["assets"][0]
        assert asset["media_type"] == media_type, asset
        assert asset["size_bytes"] == len(payload), asset
        assert asset["kind"] == expected_kind, asset


@pytest.mark.xfail(
    strict=True, raises=CapabilityGap,
    reason="c2d2 S6: validate_protocol_dispatch_local_only rejects non-loopback addresses",
)
def test_s6_owner_opted_in_remote_engine_serves_a_job(owner, gateway, fake_upstreams):
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    engine = SmellEngine()
    # This documentation address is served in-process, never on the network.
    fake_upstreams.router.add("192.0.2.10:18301", engine)
    created = _add_engine(
        client, capabilities=["smell.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": "http://192.0.2.10:18301", "dispatch": "protocol",
                "protocol_description": "d0-smell",
                # Proposed owner opt-in field, not a claim of an existing API.
                "allow_remote": True},
    )
    if created.status_code in {400, 422} and (
            "protocol dispatch does not enforce paid-provider access or spend limits" in created.text
            or ("no field with one of the names sent" in created.text and created.json()["error"].get("field") == "values")):
        # Today the opt-in field does not exist (refused as an unknown field), and without it the address is loopback-only.
        raise CapabilityGap("explicitly opted-in fake remote engine must be accepted")
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200 and content.content == MOLECULE
    records = client.get("/admin/api/activity/requests")
    assert records.status_code == 200, records.text
    receipts = [row["receipt"] for row in records.json()["records"]
                if row["receipt"].get("job_id") == job["id"]]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "succeeded"
    assert receipts[0]["backend"] == "smell-box"


def test_s7_unknown_capability_is_rejected_before_storage(owner):
    # Already true at f09015d73 for protocol engines: a task the installed description does not declare is refused
    # on write (400 invalid_engine, field capabilities) and nothing is stored. This test keeps it true.
    client, *_ = owner
    assert _install_description(client).status_code == 201
    created = _add_engine(
        client, capabilities=["smel.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
    )
    assert created.status_code == 400, created.text
    error = created.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "capabilities"), error
    state = client.get("/admin/api/operator/service-registry")
    assert state.status_code == 200, state.text
    # Rejected writes must leave no engine behind; use the same registry view
    # returned by the owner service route.
    assert '"smell-box"' not in json.dumps(state.json())


# Conservative partial S4/S5 evidence: pure boundaries only, not owner/API or
# post-acceptance retry qualification. No invented capability-gap xfail.
def test_s4_partial_declared_alias_resolves_one_service_without_mutating_policy():
    """Owner API admission for operation aliases still needs an end-to-end oracle."""
    import copy
    from gateway.alias_policy import AliasPolicyResolutionError, resolve_request_target

    config = {"backends": {"fixture-service": {"models": [
        {"id": "base", "transforms": ["base-transform"]},
        {"id": "public-alias", "base_model_id": "base",
         "policy_overlay": ["alias-transform"]},
    ]}}}
    before = copy.deepcopy(config)
    target = resolve_request_target("public-alias", runtime_config=config,
                                    aliases_only=True, required=True)
    assert target is not None
    assert (target.requested_model, target.logical_service_id, target.base_model_id) == (
        "public-alias", "fixture-service", "base")
    assert target.base_transform_specs == ("base-transform",)
    assert target.alias_overlay_specs == ("alias-transform",)
    # A reference cannot silently resolve against a different declared service.
    with pytest.raises(AliasPolicyResolutionError, match="no declared logical alias policy"):
        resolve_request_target("public-alias", runtime_config=config,
                               service_id="other-service", aliases_only=True, required=True)
    assert config == before


def test_s5_partial_pre_execution_availability_preserves_order_and_remaining_candidates():
    """Post-acceptance timeout retry safety still needs an execution-outcome oracle."""
    import copy
    from gateway.stable_aliases import resolve_alias

    # This is resolver input, not a claim that an owner API accepts this section.
    config = {"smell.infer": {
        "stable": {"public-alias": ["first", "second", "third"]},
        "engines": {
            "first": {"service": "service-a", "model": "model-a"},
            "second": {"service": "service-b", "model": "model-b"},
            "third": {"service": "service-c", "model": "model-c"},
        },
    }}
    before = copy.deepcopy(config)
    all_ready = resolve_alias(config, "smell.infer", "public-alias",
                              engine_available=lambda _id, _spec: True)
    assert all_ready is not None
    assert (all_ready.engine, all_ready.service, all_ready.upstream_model) == (
        "first", "service-a", "model-a")
    assert all_ready.fallbacks == (("second", "service-b"), ("third", "service-c"))
    assert all_ready.skipped_engines == ()
    assert all_ready.fallback_declared is True

    unavailable_first = resolve_alias(
        config, "smell.infer", "public-alias",
        engine_available=lambda engine_id, _spec: engine_id != "first")
    assert unavailable_first is not None
    assert (unavailable_first.engine, unavailable_first.service,
            unavailable_first.upstream_model) == ("second", "service-b", "model-b")
    assert unavailable_first.skipped_engines == ("first",)
    assert unavailable_first.fallbacks == (("third", "service-c"),)
    assert unavailable_first.fallback_declared is True
    assert config == before


def test_s5_partial_direct_engine_name_does_not_inherit_alias_fallbacks():
    """Execution refusal versus uncertain acceptance still needs a dispatch oracle."""
    from gateway.stable_aliases import AliasUnavailableError, resolve_alias

    config = {"smell.infer": {
        "stable": {"public-alias": ["first", "second"]},
        "engines": {
            "first": {"service": "service-a", "model": "model-a"},
            "second": {"service": "service-b", "model": "model-b"},
        },
    }}
    direct = resolve_alias(config, "smell.infer", "first")
    assert direct is not None
    assert direct.engine == "first"
    assert direct.fallbacks == ()
    assert direct.fallback_declared is False
    with pytest.raises(AliasUnavailableError) as refused:
        resolve_alias(config, "smell.infer", "first",
                      engine_available=lambda engine_id, _spec: engine_id == "second")
    assert refused.value.code == "model_not_deployed"
    assert refused.value.fallback_declared is False



def test_s2_partial_conservative_single_wav_result_is_audio(owner, gateway, fake_upstreams):
    """Multiple ordered heterogeneous job assets still need an end-to-end oracle."""
    # Conservative assumption: the demonstrated v1 description fetches one file.
    import struct

    samples = bytes(2)
    wav = (b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
           + b"data" + struct.pack("<I", len(samples)) + samples)
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    engine = PngEngine(content=wav, media_type="audio/wav")
    fake_upstreams.router.add("127.0.0.1:18302", engine)
    created = _add_engine(
        client, capabilities=["smell.infer"],
        models=[{"id": "smell-1", "public_name": "smell-1"}],
        values={"address": "http://127.0.0.1:18302", "dispatch": "protocol",
                "protocol_description": "d0-smell"},
    )
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200, content.text
        assert content.content == wav
        assert len(job["assets"]) == 1, job
        asset = job["assets"][0]
        assert asset["media_type"] == "audio/wav", asset
        assert asset["size_bytes"] == len(wav), asset
        assert asset["kind"] == "audio", asset
