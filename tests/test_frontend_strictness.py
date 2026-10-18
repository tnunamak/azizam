"""Frontend strictness and the loss report (contract section 5).

A route row may declare ``carries``: the request fields its translator can carry. A frontend
declares ``strictness``: ``exact`` refuses a field the translator can't carry; ``lenient`` drops it
and records it. A report of dropped or approximated fields goes into the receipt and into the
``x-azizam-loss`` response header. An ``identity`` frontend carries every field, so its report is
empty and it declares no ``carries``.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import proxy
from gateway import frontend_loss, frontend_mount, receipt_store
from gateway.receipts import RouteReceipt
from tests.support.access import plan_for
from tests.test_runtime_route_policy import AUTH_HEADERS, authenticated_profile

CARRIED = ["model", "messages"]
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


class Seen:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.receipts: list[RouteReceipt] = []

    async def handler(self, request: Request):
        self.bodies.append(await request.json())
        self.receipts.append(RouteReceipt(protocol="vendor", intent="chat.completions", path=request.url.path))
        frontend_loss.approximated("temperature", "clamped to 1.0")
        return {"ok": True}


def _mount(seen: Seen, *, strictness: str, translator: str = "vendor-v1", carries=CARRIED, refuse=None) -> FastAPI:
    app = FastAPI()
    translators = frontend_mount.Translators({
        translator: {"chat.completions": frontend_mount.Operation(seen.handler, frozenset({"POST"}))},
    })
    route = {"methods": ["POST"], "canonical": "/vendor/v1/chat", "intent": "chat.completions", "translator": translator}
    if carries is not None:
        route["carries"] = carries
    rows = frontend_mount.rows_from_frontends([
        {"id": "vendor", "enabled": True, "strictness": strictness, "registered_routes": [route]},
    ])
    frontend_mount.mount(app, rows, translators, refuse=refuse)
    return app


def test_an_exact_frontend_refuses_a_field_it_cannot_carry():
    seen = Seen()
    response = TestClient(_mount(seen, strictness="exact")).post("/vendor/v1/chat", json={**BODY, "logit_bias": {"1": 2}})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "frontend_field_unsupported"
    assert "logit_bias" in error["message"]
    assert seen.bodies == []
    assert json.loads(response.headers["x-azizam-loss"]) == {"dropped": ["logit_bias"], "approximated": []}


def test_a_lenient_frontend_drops_the_field_and_reports_it():
    seen = Seen()
    response = TestClient(_mount(seen, strictness="lenient")).post("/vendor/v1/chat", json={**BODY, "logit_bias": {"1": 2}})

    assert response.status_code == 200, response.text
    assert seen.bodies == [BODY]
    report = json.loads(response.headers["x-azizam-loss"])
    assert report == {
        "dropped": ["logit_bias"],
        "approximated": [{"field": "temperature", "note": "clamped to 1.0"}],
    }
    # The receipt the handler opened carries the same report.
    [receipt] = seen.receipts
    assert receipt.metadata["loss"] == {"dropped": ["logit_bias"], "approximated": []}
    assert "logit_bias" in receipt.ignored_inputs


def test_a_request_that_fits_the_translator_has_nothing_to_report():
    seen = Seen()
    response = TestClient(_mount(seen, strictness="exact")).post("/vendor/v1/chat", json=BODY)

    assert response.status_code == 200, response.text
    assert seen.bodies == [BODY]
    assert json.loads(response.headers["x-azizam-loss"])["dropped"] == []
    assert "loss" not in seen.receipts[0].metadata


def test_an_identity_frontend_carries_everything_so_its_report_is_empty():
    with pytest.raises(frontend_mount.FrontendMountError, match="identity"):
        _mount(Seen(), strictness="exact", translator="identity", carries=CARRIED)

    seen = Seen()
    app = _mount(seen, strictness="exact", translator="identity", carries=None)
    response = TestClient(app).post("/vendor/v1/chat", json={**BODY, "logit_bias": {"1": 2}, "anything": 1})

    assert response.status_code == 200, response.text
    assert seen.bodies == [{**BODY, "logit_bias": {"1": 2}, "anything": 1}]
    assert "x-azizam-loss" not in response.headers
    assert frontend_loss.LossReport().empty


def test_strictness_is_exact_or_lenient():
    for value in ("exact", "lenient"):
        [row] = frontend_mount.rows_from_frontends([{
            "id": "v", "enabled": True, "strictness": value,
            "registered_routes": [{"methods": ["POST"], "canonical": "/a", "intent": "chat.completions", "translator": "t"}],
        }])
        assert row.strictness == value
    with pytest.raises(frontend_mount.FrontendMountError, match="strictness"):
        frontend_mount.rows_from_frontends([{
            "id": "v", "enabled": True, "strictness": "sloppy",
            "registered_routes": [{"methods": ["POST"], "canonical": "/a", "intent": "chat.completions", "translator": "t"}],
        }])


def test_a_frontend_that_declares_no_strictness_is_exact():
    [row] = frontend_mount.rows_from_frontends([{
        "id": "v", "enabled": True,
        "registered_routes": [{"methods": ["POST"], "canonical": "/a", "intent": "chat.completions", "translator": "t"}],
    }])
    assert row.strictness == "exact"


def test_the_loss_header_is_bounded_and_ascii():
    report = frontend_loss.LossReport(dropped=[f"field_{n}_é" + "x" * 200 for n in range(100)])
    value = report.header_value()
    assert value.isascii()
    assert len(value) < 4096
    assert json.loads(value)["truncated"] is True


def test_an_exact_refusal_on_the_live_gateway_authenticates_first_and_writes_a_denied_receipt():
    receipt_store.init_db()
    seen = Seen()
    profile = plan_for("strict-live", caps=())
    body = {**BODY, "logit_bias": {"1": 2}}
    # Mount the row on the live app, which holds the gateway's authentication and receipt store.
    translators = frontend_mount.Translators({
        "vendor-v1": {"chat.completions": frontend_mount.Operation(seen.handler, frozenset({"POST"}))},
    })
    rows = frontend_mount.rows_from_frontends([{
        "id": "vendor", "enabled": True, "strictness": "exact",
        "registered_routes": [{"methods": ["POST"], "canonical": "/vendor/v1/chat", "intent": "chat.completions",
                               "translator": "vendor-v1", "carries": CARRIED}],
    }])
    before = {row["request_id"] for row in receipt_store.recent(limit=50, audience="internal")}
    routes = frontend_mount.mount(proxy.app, rows, translators, index=proxy.app.router.routes.index(proxy.WEB_UI_ROUTE),
                                  refuse=proxy._frontend_refusal)
    try:
        client = TestClient(proxy.app)
        anonymous = client.post("/vendor/v1/chat", json=body)
        with authenticated_profile(profile):
            refused = client.post("/vendor/v1/chat", headers=AUTH_HEADERS, json=body)
    finally:
        for route in routes:
            proxy.app.router.routes.remove(route)

    assert anonymous.status_code == 401, anonymous.text
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "frontend_field_unsupported"
    assert seen.bodies == []
    [receipt] = [row for row in receipt_store.recent(limit=50, audience="internal")
                 if row["request_id"] not in before and row.get("path") == "/vendor/v1/chat"]
    assert receipt["status"] == "denied"
    assert receipt["app_id"] == "app_runtime_policy"
    assert "logit_bias" in receipt["rejected_inputs"]
