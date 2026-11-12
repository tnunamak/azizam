"""The optional run tag: validation, the header gate, and the receipt echo (train 54, L3)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import request_history, run_tag
from gateway.receipts import RouteReceipt
from gateway.request_source import RequestSourceMiddleware, current_run_tag


@pytest.mark.parametrize("tag", ["a", "x" * 64, "batch:7_a.b-c", "0"])
def test_a_good_tag_is_kept_as_is(tag):
    assert run_tag.validate(tag) == tag and run_tag.valid_or_none(tag) == tag


@pytest.mark.parametrize("tag", ["", "x" * 65, "has space", "semi;colon", "naïve", "line\nbreak", None, 7])
def test_a_bad_tag_is_refused_with_the_rule(tag):
    with pytest.raises(run_tag.InvalidTag, match="tag must be 1 to 64"):
        run_tag.validate(tag)
    assert run_tag.valid_or_none(tag) is None


def _app():
    app = FastAPI()
    app.add_middleware(RequestSourceMiddleware)

    @app.get("/probe")
    def probe():
        receipt = RouteReceipt(protocol="test", intent="probe", path="/probe")
        return {"seen": current_run_tag(), "receipt": receipt.to_dict()}

    return TestClient(app)


def test_the_header_reaches_the_receipt_and_an_absent_one_changes_nothing():
    client = _app()
    tagged = client.get("/probe", headers={run_tag.TAG_HEADER: "batch-1"}).json()
    assert tagged["seen"] == "batch-1" and tagged["receipt"]["tag"] == "batch-1"
    plain = client.get("/probe").json()
    assert plain["seen"] is None and "tag" not in plain["receipt"]


def test_a_bad_tag_header_is_refused_with_400_before_the_route_runs():
    response = _app().get("/probe", headers={run_tag.TAG_HEADER: "has space"})
    body = response.json()["error"]
    assert response.status_code == 400 and body["code"] == "invalid_tag" and "tag must be" in body["message"]


def test_the_request_history_summary_stores_the_tag_only_when_given():
    assert request_history.summarize("/v1/x", {"model": "m"}, 200, 1, tag="batch-1")["tag"] == "batch-1"
    assert "tag" not in request_history.summarize("/v1/x", {"model": "m"}, 200, 1)
