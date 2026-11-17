"""C2D2 route oracles, partial: S3, S6 (expected to fail until built), and the typo half of S7.

S1 lacks the public media-job link from library assets to named engine file inputs.
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
from urllib.parse import urlsplit

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
    assert job["status"] == "succeeded", json.dumps({'job': job, 'paths': getattr(engine, 'paths', None)}, sort_keys=True)
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
        assert job["assets"][0]["asset_id"] != job["assets"][1]["asset_id"], job
        # Metadata alone cannot prove that each fetched body survived storage.
        for asset, expected in zip(job["assets"], (PNG, engine.wav), strict=True):
            # The content route needs a signed URL; the asset record carries it.
            record = public.get("/gateway/v1/assets/" + asset["asset_id"], headers=_headers(key))
            assert record.status_code == 200, record.text
            signed = urlsplit(record.json()["content_url"])
            path = signed.path + "?" + signed.query
            content = public.get(path)
            evidence = (path, content.status_code, content.content[:160])
            assert content.status_code == 200, evidence
            assert content.headers.get("content-type", "").startswith(
                asset["media_type"]
            ), (content.headers.get("content-type"), evidence)
            assert content.content == expected, evidence



@pytest.mark.xfail(
    strict=True, raises=CapabilityGap,
    reason="c2d2 S1: public media jobs cannot bind library assets to named file inputs",
)
def test_s1_library_png_requires_public_job_file_binding(owner, gateway, fake_upstreams):
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
        assert (asset["kind"], asset["media_type"], asset["size_bytes"]) == (
            "image", "image/png", len(PNG))
        asset_id = asset["asset_id"]
        assert asset_id, asset
        response = public.post(
            "/gateway/v1/media/jobs", headers=_headers(key),
            json={"intent": "smell.infer", "model": "smell-1",
                  "input": {"vector": VECTOR},
                  # Proposed binding, not a claim of an existing product field.
                  "files": {"image": {"asset_id": asset_id}}},
        )
        refusal = response.text.lower()
        if response.status_code in {400, 422} and (
                "files" in refusal
                or ("body" in refusal and "not allowed" in refusal)):
            raise CapabilityGap(
                "Public media jobs lack the library-asset to named-file input link."
            )
        assert response.status_code == 202, response.text
        accepted = response.json()
        for _ in range(100):
            response = public.get("/gateway/v1/media/jobs/" + accepted["id"],
                                  headers=_headers(key))
            assert response.status_code == 200, response.text
            second_job = response.json()
            if second_job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert second_job["status"] in {"succeeded", "failed", "cancelled"}, second_job
        assert engine.posts[1:] == [{"input": {"vector": VECTOR}}], (
            "Expected the existing engine request with no file; observed",
            engine.posts[1:], second_job,
        )
        pytest.fail("The accepted job silently ignored files and sent no file to the engine.")


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


def test_s6_owner_opted_in_remote_engine_serves_a_job(owner, gateway, fake_upstreams, monkeypatch):
    from tests.support.remote_protocol import gateway_case, add_remote
    client, _, owner_id, plan_id = owner
    engine = gateway_case(gateway, monkeypatch, fake_upstreams)
    assert _install_description(client).status_code == 201
    created = add_remote(client)
    assert created.status_code == 201, created.text
    enabled = client.patch("/admin/api/operator/services/smell-box",
                           headers=_write_headers(client), json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200 and content.content == b'remote-result'
    assert engine.connections == [('93.184.216.34', 443)] * 3
    assert engine.tls == ['engine.example'] * 3
    records = client.get("/admin/api/activity/requests")
    assert records.status_code == 200, records.text
    receipts = [row["receipt"] for row in records.json()["records"]
                if row["receipt"].get("job_id") == submitted["id"]]
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


def test_s6_remote_policy_owner_authentication_production_path(owner, gateway, monkeypatch):
    """Store policy data on a local record; this grants no remote egress."""
    from tests.test_signin_who_can_enter import signed_in
    from gateway import remote_engine
    monkeypatch.setattr(remote_engine, 'PRODUCTION_READY', True)

    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    assert _add_engine(client).status_code == 201
    state = client.get("/admin/api/operator/service-registry").json()
    connection = dict(state["service_registry"]["services"]["smell-box"]["config"]["connection"])
    connection["remote_dispatch"] = {"enabled": True}
    connection['address'] = 'https://engine.example'
    path = "/admin/api/operator/services/smell-box"
    body = {"connection": connection, 'base_url': 'https://engine.example', 'address': 'https://engine.example',
            'host': 'engine.example', 'port': 443, 'scheme': 'https', 'native_passthrough': False, 'cost_class': 'free-local'}
    with gateway.client() as anonymous:
        assert anonymous.patch(path, json=body).status_code in {401, 403}
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        assert public.patch(path, headers=_headers(key), json=body).status_code in {401, 403}
    admin, _ = signed_in(gateway, sub="remote-policy-admin", email="admin@example.test", groups=["/Trusted"])
    denied = admin.patch(path, headers=_write_headers(admin), json=body)
    assert denied.status_code == 403, denied.text
    assert client.get("/admin/api/operator/service-registry").json() == state
    accepted = client.patch(path, headers=_write_headers(client), json=body)
    assert accepted.status_code == 200, accepted.text
    from gateway import admin_store
    stored = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["smell-box"]
    assert stored["config"]["connection"]["remote_dispatch"] == {"enabled": True}
    # Bulk writes must not let an administrator relabel a service's billing class.
    before_bulk = client.get('/admin/api/operator/service-registry').json()['service_registry']
    edited = json.loads(json.dumps(before_bulk))
    edited['services']['smell-box']['config']['provider_type'] = 'local'
    denied_bulk = admin.put('/admin/api/operator/service-registry', headers=_write_headers(admin), json=edited)
    assert denied_bulk.status_code == 403, denied_bulk.text
    assert client.get('/admin/api/operator/service-registry').json()['service_registry'] == before_bulk


def test_s6_remote_without_owner_opt_in_keeps_existing_refusal(owner, gateway, monkeypatch, fake_upstreams):
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    response = _add_engine(client, values={
        "address": "https://remote.example.test", "dispatch": "protocol",
        "protocol_description": "d0-smell"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == (
        "protocol dispatch does not enforce paid-provider access or spend limits; "
        "use the existing provider route")
    state = client.get("/admin/api/operator/service-registry")
    assert "smell-box" not in state.json()["service_registry"]["services"]
    # Publish a local-only registry snapshot before starting the public gateway.
    assert _add_engine(client).status_code == 201
    from gateway import remote_engine
    from tests.test_owner_engine_chain_smell import EmptyModelCatalog
    for host in ('127.0.0.1:7890', '127.0.0.1:4011'):
        if host not in fake_upstreams.router._routes:
            fake_upstreams.router.add(host, EmptyModelCatalog())
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        # Keep display timestamps and cached host facts fixed across this comparison.
        import status_endpoint
        fixed_time = time.time()
        monkeypatch.setattr(time, 'time', lambda: fixed_time)
        monkeypatch.setattr(status_endpoint, '_SNAPSHOT', {})
        monkeypatch.setattr(status_endpoint, '_SNAPSHOT_CLOSED', True)
        monkeypatch.setattr(remote_engine, 'PRODUCTION_READY', False)
        models_before = public.get('/v1/models', headers=_headers(key))
        schema_before = public.get('/openapi.json')
        monkeypatch.setattr(remote_engine, 'PRODUCTION_READY', True)
        models_after = public.get('/v1/models', headers=_headers(key))
        schema_after = public.get('/openapi.json')
        assert models_before.status_code == models_after.status_code == 200
        assert schema_before.status_code == schema_after.status_code == 200
        assert models_before.content == models_after.content
        assert schema_before.content == schema_after.content


def test_s6_remote_generic_job_lifecycle_and_leases_production_path(owner, gateway, fake_upstreams):
    """Qualify the local generic runner before giving it any remote authority."""
    client, _, owner_id, plan_id = owner
    assert _install_description(client).status_code == 201
    created = _add_engine(client, capabilities=["smell.infer"],
                          values={"address": "http://127.0.0.1:18303", "dispatch": "protocol", "protocol_description": "d0-smell"},
                          models=[{"id": "smell-1", "public_name": "smell-1"}])
    assert created.status_code == 201, created.text
    configured = client.patch("/admin/api/operator/services/smell-box", headers=_write_headers(client), json={
        "protocol": "d0-smell",
        "residency": {"domain": "scenario-domain", "transition": {"enabled": True, "activation": "pass_through"}},
        "enabled": True})
    assert configured.status_code == 200, configured.text
    proxy = gateway.proxy
    policy = proxy._transition_policy("smell-box")
    assert policy.enabled and policy.pass_through
    seen = []

    class LeasedEngine(SmellEngine):
        async def __call__(self, scope, receive, send):
            proxy._require_egress_lease("smell-box")
            seen.append((scope["method"], scope["path"], proxy._swap_drain.in_flight("smell-box")))
            await super().__call__(scope, receive, send)

    engine = LeasedEngine()
    fake_upstreams.router.add("127.0.0.1:18303", engine)
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        # A closed serving gate must terminate the admitted job before any upstream request.
        submitted, job = _job(public, key, engine)
        content = public.get(submitted["content_url"], headers=_headers(key))
        assert content.status_code == 200 and content.content == MOLECULE
        successful_requests = list(seen)
        gates = proxy._transition_gates()
        gates.force("scenario-domain", "closed")
        denied = public.post("/gateway/v1/media/jobs", headers=_headers(key), json={
            "intent": "smell.infer", "model": "smell-1", "input": {"vector": VECTOR}})
        assert denied.status_code == 202, denied.text
        for _ in range(100):
            refused = public.get("/gateway/v1/media/jobs/" + denied.json()["id"], headers=_headers(key))
            assert refused.status_code == 200, refused.text
            if refused.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert refused.json()["status"] == "failed", refused.text
        assert seen == successful_requests
    assert [(method, path) for method, path, _ in seen] == [
        ("POST", "/v1/infer"), ("GET", "/v1/infer/smell-1"), ("GET", "/v1/infer/smell-1/molecule")]
    assert all(count == 1 for _, _, count in seen), seen
    assert proxy._swap_drain.in_flight("smell-box") == 0
    records = client.get("/admin/api/activity/requests")
    assert records.status_code == 200, records.text
    receipts = [row["receipt"] for row in records.json()["records"] if row["receipt"].get("job_id") == submitted["id"]]
    assert len(receipts) == 1
    assert (receipts[0]["status"], receipts[0]["backend"]) == ("succeeded", "smell-box")


def test_s6_only_owner_can_enable_remote_dispatch(owner, gateway, monkeypatch):
    test_s6_remote_policy_owner_authentication_production_path(owner, gateway, monkeypatch)


def test_s6_remote_job_observation_poll_asset_security(owner, gateway, fake_upstreams, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    from tests.support.remote_protocol import gateway_case, add_remote
    import yaml
    client, _, owner_id, plan_id = owner
    recorder = gateway_case(gateway, monkeypatch, fake_upstreams)
    raw = yaml.safe_load(SMELL.read_text())
    raw['auth'] = {'inference': {'kind': 'bearer'}, 'control': {'kind': 'bearer'}}
    raw['operations']['select'] = {'class': 'control', 'request': {'method': 'POST', 'path': '/select'},
                                    'response': {'framing': 'json'}}
    installed = client.put('/admin/api/engine-protocols/d0-smell', content=yaml.safe_dump(raw),
                           headers={**csrf_for(client), 'Content-Type': 'application/yaml', 'If-None-Match': '*'})
    assert installed.status_code == 201, installed.text
    created = add_remote(client, observations=True)
    assert created.status_code == 201, created.text
    connection = created.json()['service_registry']['services']['smell-box']['config']['connection']
    monkeypatch.setenv('REMOTE_JOB_TEST_CREDENTIAL', 'synthetic-service-job')
    connection['credentials'] = {cls: {'secret_ref': 'os.environ/REMOTE_JOB_TEST_CREDENTIAL',
                                      'origin': 'https://engine.example', 'header': 'Authorization', 'scheme': 'Bearer'}
                                  for cls in ('inference', 'control')}
    enabled = client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client),
                           json={'connection': connection, 'enabled': True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted, job = _job(public, key, recorder)
        content = public.get(submitted['content_url'], headers=_headers(key))
        assert content.status_code == 200 and content.content == b'remote-result'
        assert len(recorder.connections) == 4
        def json_response(payload):
            encoded = json.dumps(payload).encode()
            return (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' +
                    str(len(encoded)).encode() + b'\r\n\r\n' + encoded)
        recorder.responses.extend([
            json_response({'value': 'smell-1'}), json_response({'job': {'id': 'j'}}),
            json_response({'state': 'ready'}),
            b'HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/\r\nContent-Length: 0\r\n\r\n'])
        denied_job = public.post('/gateway/v1/media/jobs', headers=_headers(key), json={
            'intent': 'smell.infer', 'model': 'smell-1', 'input': {'vector': VECTOR}})
        assert denied_job.status_code == 202, denied_job.text
        for _ in range(100):
            denied_status = public.get('/gateway/v1/media/jobs/' + denied_job.json()['id'], headers=_headers(key)).json()
            if denied_status['status'] in {'succeeded', 'failed', 'canceled'}:
                break
            time.sleep(0.01)
        assert denied_status['status'] == 'failed' and denied_status['assets'] == [], denied_status
    sent = b''.join(recorder.writes)
    assert b'synthetic-service-job' in sent
    assert key.encode() not in sent
    assert 'synthetic-service-job' not in caplog.text
    assert len(recorder.connections) == 8  # four requests per job; no redirect target connection
    assert all(peer == ('93.184.216.34', 443) for peer in recorder.connections)
    records = client.get('/admin/api/activity/requests').json()['records']
    receipts = [row['receipt'] for row in records if row['receipt'].get('job_id') == submitted['id']]
    assert len(receipts) == 1 and receipts[0]['status'] == 'succeeded'
    from gateway import admin_store, media_jobs
    internal = media_jobs.get(submitted['id'], audience='internal', path=media_jobs.DB_PATH)
    assert internal['metadata']['remote_binding']['policy_generation']


def test_s6_remote_opt_out_cancels_running_job(owner, gateway, fake_upstreams, monkeypatch):
    from tests.support.remote_protocol import gateway_case, add_remote
    client, _, owner_id, plan_id = owner
    recorder = gateway_case(gateway, monkeypatch, fake_upstreams)
    recorder.pending = True
    assert _install_description(client).status_code == 201
    created = add_remote(client)
    assert created.status_code == 201, created.text
    enabled = client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client), json={'enabled': True})
    assert enabled.status_code == 200, enabled.text
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        response = public.post('/gateway/v1/media/jobs', headers=_headers(key), json={
            'intent': 'smell.infer', 'model': 'smell-1', 'input': {'vector': VECTOR}})
        assert response.status_code == 202, response.text
        job_id = response.json()['id']
        for _ in range(100):
            if any(b'GET /v1/infer/j ' in chunk for chunk in recorder.writes):
                break
            time.sleep(0.01)
        assert any(b'GET /v1/infer/j ' in chunk for chunk in recorder.writes)
        connection = created.json()['service_registry']['services']['smell-box']['config']['connection']
        connection['remote_dispatch']['enabled'] = False
        revoked = client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client), json={'connection': connection})
        assert revoked.status_code == 200, revoked.text
        count = len(recorder.connections)
        for _ in range(100):
            job = public.get('/gateway/v1/media/jobs/' + job_id, headers=_headers(key)).json()
            if job['status'] in {'succeeded', 'failed', 'canceled'}:
                break
            time.sleep(0.01)
        assert job['status'] == 'canceled', job
        assert len(recorder.connections) == count and not job['assets']
        refused = public.post('/gateway/v1/media/jobs', headers=_headers(key), json={
            'intent': 'smell.infer', 'model': 'smell-1', 'input': {'vector': VECTOR}})
        assert refused.status_code == 502, refused.text
        assert len(recorder.connections) == count
    receipts = [row['receipt'] for row in client.get('/admin/api/activity/requests').json()['records']
                if row['receipt'].get('job_id') == job_id]
    assert len(receipts) == 1 and receipts[0]['status'] == 'cancelled'



def test_s6_unrelated_registry_generation_leaves_running_remote_job(owner, gateway, fake_upstreams, monkeypatch):
    from tests.support.remote_protocol import gateway_case, add_remote
    client, _, owner_id, plan_id = owner
    recorder = gateway_case(gateway, monkeypatch, fake_upstreams)
    recorder.pending = True
    assert _install_description(client).status_code == 201
    assert add_remote(client).status_code == 201
    assert client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client),
                        json={'enabled': True}).status_code == 200
    key = _app_key(client, owner_id, plan_id)
    with gateway.client() as public:
        submitted = public.post('/gateway/v1/media/jobs', headers=_headers(key), json={
            'intent': 'smell.infer', 'model': 'smell-1', 'input': {'vector': VECTOR}})
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()['id']
        for _ in range(100):
            if '/v1/infer/j' in recorder.paths:
                break
            time.sleep(.01)
        assert '/v1/infer/j' in recorder.paths
        before = client.get('/admin/api/operator/service-registry').json()
        changed = client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client),
                               json={'description': 'Updated display metadata'})
        assert changed.status_code == 200, changed.text
        after = client.get('/admin/api/operator/service-registry').json()
        assert after != before
        recorder.pending = False
        for _ in range(200):
            job = public.get('/gateway/v1/media/jobs/' + job_id, headers=_headers(key)).json()
            if job['status'] in {'succeeded', 'failed', 'canceled'}:
                break
            time.sleep(.01)
        assert job['status'] == 'succeeded', job
        assert job['assets']


@pytest.mark.parametrize('writer', ['registry_put', 'config_patch', 'config_import', 'service_patch', 'service_add'])
def test_s6_non_owner_cannot_write_remote_policy_through_any_api(owner, gateway, writer):
    from tests.test_signin_who_can_enter import signed_in
    client, _, _, _ = owner
    assert _install_description(client).status_code == 201
    from tests.support.remote_protocol import add_remote
    assert add_remote(client).status_code == 201
    before = client.get('/admin/api/operator/service-registry').json()['service_registry']
    edited = json.loads(json.dumps(before))
    connection = edited['services']['smell-box']['config']['connection']
    connection['remote_dispatch'] = {'enabled': True, 'limits': {'total_s': 2}}
    admin, _ = signed_in(gateway, sub='remote-writer-admin', email='admin@example.test', groups=['/Trusted'])
    routes = {
        'registry_put': ('PUT', '/admin/api/operator/service-registry', edited),
        'config_patch': ('PATCH', '/admin/api/config/changes', {'items': [{'section': 'registry', 'value': edited}]}),
        'config_import': ('POST', '/admin/api/config/import', {'document': edited}),
        'service_patch': ('PATCH', '/admin/api/operator/services/smell-box', {'connection': connection}),
        'service_add': ('POST', '/admin/api/operator/services', {'name': 'remote-new', 'base_url': 'https://engine.example', 'protocol': 'd0-smell', 'cost_class': 'free-local', 'enabled': False, 'connection': connection}),
    }
    method, path, body = routes[writer]
    response = admin.request(method, path, headers=_write_headers(admin), json=body)
    assert response.status_code == 403, response.text
    assert client.get('/admin/api/operator/service-registry').json()['service_registry'] == before
