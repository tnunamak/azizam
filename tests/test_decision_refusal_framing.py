"""A gate or admission refusal carried through the decisions route keeps an honest Content-Length.

The refusal is built as a Response (its Content-Length counts its own body), then re-serialized by the decisions
route from ``json.loads(body)``. The copied length described other bytes and made a real HTTP client fail.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import proxy
from gateway.decisions_api import DecisionDispatchResult, DecisionModel, DecisionsApiDeps, register_decision_routes
from gateway.transition_gate import TransitionUnavailable

BODY = json.loads((Path(__file__).parent / "fixtures" / "typesafe_systemone_noul.json").read_text())["request"]
H = {"authorization": "Bearer good"}


def _refusals():
    """The same Responses the proxy turns into dispatch results (admit refusal and the egress refusal)."""
    transitioning = proxy._transition_unavailable_response(
        TransitionUnavailable("gpu1", reason="gate_closed", retry_after_s=7))
    unsupported = proxy._transition_unavailable_response(
        TransitionUnavailable("gpu1", reason="path_not_leased"))
    return {"engine_transitioning": transitioning, "transition_unsupported": unsupported}


def _app(refusal) -> FastAPI:
    app = FastAPI()
    result = DecisionDispatchResult(
        body=json.loads(refusal.body), status_code=refusal.status_code,
        headers=dict(refusal.headers), backend="decider",
    )

    async def dispatch(_auth, _profile, _body, _request_id):
        return result

    register_decision_routes(app, DecisionsApiDeps(
        authenticate=lambda request, _path: SimpleNamespace(app_profile="family")
        if request.headers.get("authorization") == "Bearer good" else JSONResponse({}, status_code=401),
        profile_for=lambda auth: SimpleNamespace(id=auth.app_profile, allowed=True),
        models_for=lambda _a, _p: [DecisionModel(id="decide", root="decider", backend="decider", engine="cpu",
                                                 capabilities={"intent": "decision.systemone"})],
        dispatch=dispatch,
        record_receipt=lambda _auth, _receipt: None,
    ))
    return app


@pytest.mark.parametrize("code", ["engine_transitioning", "transition_unsupported"])
def test_a_carried_refusal_declares_the_length_of_the_body_it_sends(code) -> None:
    refusal = _refusals()[code]
    assert refusal.headers["content-length"] == str(len(refusal.body)), "the fixture refusal is well-formed"

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=_app(refusal))
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            return await client.post("/v1/systemone", json=BODY, headers=H)

    response = asyncio.run(send())
    assert response.status_code == 503
    assert response.headers["retry-after"] == refusal.headers["retry-after"], "Retry-After survives"
    sent = response.content
    assert response.headers["content-length"] == str(len(sent))
    assert len(sent) != int(refusal.headers["content-length"]), "the re-serialized body differs in length from the original"
