"""C2D2 route oracles, partial: S3 and S6 (expected to fail until built) and the typo half of S7 (already true).

S1 requires the file-input description and Library upload/reference contracts.
S2 requires the description's multi-result fetch grammar.
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
from tests.test_signin_who_can_enter import gateway  # noqa: F401
from tests.test_owner_engine_chain_smell import (
    HOST, MOLECULE, SmellEngine, _add_engine, _app_key, _enabled_engine,
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
    async def __call__(self, scope, receive, send):
        if scope["method"] == "GET" and scope["path"] == "/v1/infer/smell-1/molecule":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"image/png"),
                                    (b"content-length", str(len(PNG)).encode())]})
            await send({"type": "http.response.body", "body": PNG})
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


@pytest.mark.xfail(
    strict=True, raises=CapabilityGap,
    reason="c2d2 S3: declared_operation_job.run stores every single result with kind=file",
)
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
        if asset["kind"] == "file":
            raise CapabilityGap("a fetched PNG must have kind=image, not file")
        assert asset["kind"] == "image", asset


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
