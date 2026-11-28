"""A forget sent to POST /sdapi/v1/txt2img reaches the core image route as a forget, and nothing is kept for it.

The adapter's inner call to the core image route is the request that request-history capture records (its path is under
/v1/), so the caller's forget has to be in that call's body. The outer /sdapi/v1/ request is not a captured route, so a
generation is recorded once. The core route is a stub here that authenticates as the real one does, and records the body
it is sent. Capture, prompt memory and the history table are the real code.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import proxy
from gateway import admin_store, media_jobs, prompt_memory, receipt_store, retention
from tests.support.gateway import gateway_client, use_admin_db
from tests.test_request_history_capture import _kept_rows, _mint_member_key

MODEL = "krea2-turbo"
PROMPT = "a sentence the member asked the image route"
FORGET = {retention.NAMESPACE: {"mode": "forget"}}
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


@pytest.fixture
def sdapi(monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", path=db)["id"]
    _mint_member_key(db, person_id)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    received: list[dict] = []

    async def core_image(request):
        auth = proxy.authenticate_request(request, "/v1/images/generations")
        if isinstance(auth, Response):
            return auth
        received.append(await request.json())
        return JSONResponse({"data": [{"b64_json": PNG_B64}]})

    route = Route("/v1/images/generations", core_image, methods=["POST"])
    monkeypatch.setattr(proxy.app.router, "routes", [route, *proxy.app.router.routes])
    return SimpleNamespace(db=db, media=media, person_id=person_id, received=received)


def _post(path: str, body: dict) -> Response:
    async def call():
        async with gateway_client() as client:
            return await client.post(path, json=body)
    return asyncio.run(call())


def _txt2img(extra: dict) -> Response:
    return _post("/sdapi/v1/txt2img", {"model": MODEL, "prompt": PROMPT, **extra})


def test_a_forget_through_sdapi_reaches_the_core_as_a_forget_and_keeps_nothing(sdapi):
    response = _txt2img({"extensions": FORGET})

    assert response.status_code == 200, response.text
    assert sdapi.received[0]["extensions"][retention.NAMESPACE] == {"mode": "forget"}
    assert "x-azizam-ignored-params" not in response.headers
    assert _kept_rows(sdapi.media) == []
    decision = prompt_memory.decide_for_person(sdapi.person_id, sdapi.received[0], admin_path=sdapi.db)
    assert (decision.keep, decision.reason) == (False, "request_forget")


def test_without_forget_the_generation_is_kept_once_as_before(sdapi):
    response = _txt2img({})

    assert response.status_code == 200, response.text
    assert retention.NAMESPACE not in sdapi.received[0].get("extensions", {})
    assert len(_kept_rows(sdapi.media)) == 1


@pytest.mark.parametrize("control", [{"mode": "keep"}, {"mode": "maybe"}, {"mode": "forget", "extra": 1}, "forget"])
def test_an_invalid_forget_gets_the_core_400_and_the_core_route_is_not_called(sdapi, control):
    body = {"model": MODEL, "prompt": PROMPT, "extensions": {retention.NAMESPACE: control}}
    core = _post("/v1/images/generations", body)
    response = _post("/sdapi/v1/txt2img", body)

    assert core.status_code == 400, core.text
    assert response.status_code == 400, response.text
    assert response.json() == core.json()
    assert sdapi.received == []


def test_other_extension_namespaces_are_not_passed_to_the_core(sdapi):
    response = _txt2img({"extensions": {"com.azizam.workflow": {"steps": 99}}})

    assert response.status_code == 200, response.text
    assert set(sdapi.received[0]["extensions"]) == {"com.azizam.workflow"}
    assert 99 not in sdapi.received[0]["extensions"]["com.azizam.workflow"].values()
    assert "extensions" in response.headers["x-azizam-ignored-params"].split(",")


@pytest.mark.parametrize("extensions", [[], "forget", 7])
def test_extensions_that_are_not_an_object_get_the_core_400_and_the_core_route_is_not_called(sdapi, extensions):
    body = {"model": MODEL, "prompt": PROMPT, "extensions": extensions}
    core = _post("/v1/images/generations", body)
    response = _post("/sdapi/v1/txt2img", body)

    assert core.status_code == 400, core.text
    assert response.status_code == 400, response.text
    assert response.json() == core.json()
    assert sdapi.received == []
