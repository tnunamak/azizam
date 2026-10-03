from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.decisions_api import (
    DecisionDispatchResult,
    DecisionModel,
    DecisionsApiDeps,
    DecisionsError,
    register_decision_routes,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeDeps:
    def __init__(self) -> None:
        self.receipts = []
        self.calls = []
        self.allowed = True
        self.reply = self._default_reply

    def authenticate(self, request, _path):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": {"message": "Invalid API key."}}, status_code=401)
        return SimpleNamespace(app_profile="family")

    def profile_for(self, auth):
        return SimpleNamespace(id=auth.app_profile, allowed=self.allowed)

    def models_for(self, _auth, _profile):
        return [
            DecisionModel(
                id="decide",
                root="decider-2b-v11",
                backend="decider-cpu",
                engine="cpu",
                capabilities={"intent": "decision.systemone", "latency_class": "cpu-seconds"},
            ),
            DecisionModel(
                id="decide-quality",
                root="decider-4b-v2.1",
                backend="decider-quality",
                engine="cpu",
                available=False,
                reason="not_started",
            ),
        ]

    async def dispatch(self, _auth, profile, body, request_id):
        self.calls.append((profile, body, request_id))
        if not profile.allowed:
            from gateway.decisions_api import DecisionsError

            raise DecisionsError(403, "model_not_allowed", "This profile cannot use that decision model.", param="model")
        return self.reply(body)

    def record_receipt(self, auth, receipt):
        self.receipts.append((auth, receipt))

    def _default_reply(self, body):
        answers = {}
        for question_id, question in body["questions"].items():
            if question["type"] == "noul":
                answers[question_id] = {"type": "noul", "noul": 0.95}
            elif question["type"] == "choice":
                options = list(question["criteria"])
                answers[question_id] = {
                    "type": "choice",
                    "choice": options[0],
                    "probabilities": {options[0]: 0.8, options[1]: 0.2},
                    "confidence": 0.6,
                }
            else:
                answers[question_id] = {
                    "type": "score",
                    "score": 1.0,
                    "legend": {str(i): value for i, value in enumerate(question["criteria"])},
                    "probabilities": {str(i): 1.0 if i == 1 else 0.0 for i, _ in enumerate(question["criteria"])},
                    "confidence": 0.9,
                }
        return DecisionDispatchResult(
            body={"model": "decider-2b-v11", "answers": answers, "usage": {"input_tokens": 12, "output_tokens": 0}},
            backend="decider-cpu",
            resolved_model="decider-2b-v11",
            engine="cpu",
        )


@pytest.fixture()
def env():
    fakes = FakeDeps()
    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=fakes.authenticate,
            profile_for=fakes.profile_for,
            models_for=fakes.models_for,
            dispatch=fakes.dispatch,
            record_receipt=fakes.record_receipt,
        ),
    )
    return SimpleNamespace(client=TestClient(app), fakes=fakes)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


H = {"authorization": "Bearer good"}


class TestClientTransport:
    def __init__(self, client, httpx2_module):
        self.client = client
        self.httpx2 = httpx2_module

    def handle_request(self, request):
        path = request.url.raw_path.decode()
        content = request.read()
        response = self.client.request(
            request.method,
            path,
            content=content,
            headers=dict(request.headers),
        )
        return self.httpx2.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    def close(self):
        return None


def test_typesafe_models_lists_decision_models_with_capabilities(env):
    resp = env.client.get("/typesafe/v1/models", headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert [m["name"] for m in body["models"]] == ["decide", "decide-quality"]
    assert body["models"][0]["capabilities"]["engine"] == "cpu"
    assert body["models"][0]["capabilities"]["cost_class"] == "free-local"


def test_systemone_accepts_typesafe_noul_fixture_and_records_receipt(env):
    fixture = _fixture("typesafe_systemone_noul.json")
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=fixture["response"],
        backend="decider-cpu",
        resolved_model="jev-1.13.0",
        engine="cpu",
    )
    resp = env.client.post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json() == fixture["response"]
    assert resp.headers["x-gateway-backend"] == "decider-cpu"
    assert resp.headers["x-gateway-remote"] == "0"
    assert resp.headers["x-gateway-cost-usd"] == "0.0"
    assert resp.headers["x-gateway-engine"] == "cpu"
    assert resp.headers["x-decision-engine"] == "cpu"
    auth, receipt = env.fakes.receipts[0]
    assert auth.app_profile == "family"
    assert receipt.protocol == "typesafe.systemone"
    assert receipt.intent == "decision.systemone"
    assert receipt.requested_model == "jev-latest"
    assert receipt.resolved_model == "jev-1.13.0"
    assert receipt.question_count == 1
    assert receipt.cost_usd == 0.0
    assert receipt.engine == "cpu"


def test_typesafe_prefixed_systemone_accepts_choice_and_score_fixture(env):
    fixture = _fixture("typesafe_systemone_choice_score.json")
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=fixture["response"],
        backend="decider-cpu",
        resolved_model="jev-1.13.0",
        engine="cpu",
    )
    resp = env.client.post("/typesafe/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json() == fixture["response"]
    assert env.fakes.receipts[0][1].question_count == 2
    assert env.fakes.receipts[0][1].engine == "cpu"


@pytest.mark.parametrize(
    "patch,param,needle",
    [
        ({"model": ""}, "model", "model is required"),
        ({"state": 3}, "state", "state must be a string"),
        ({"questions": {}}, "questions", "questions must be a non-empty object"),
        ({"questions": {"q": {"type": "maybe", "instructions": "x"}}}, "questions.q.type", "question type"),
        ({"questions": {"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}}}, "questions.q.criteria", "2 to 255"),
        (
            {"questions": {"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}},
            "questions.q.criteria",
            "2 to 10",
        ),
    ],
)
def test_validation_errors_are_typesafe_shaped_and_do_not_call_backend(env, patch, param, needle):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body.update(patch)
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["param"] == param
    assert needle in error["message"]
    assert env.fakes.calls == []
    assert env.fakes.receipts[0][1].error_code == error["code"]
    assert env.fakes.receipts[0][1].error_param == param


def test_unknown_top_level_and_question_fields_are_rejected(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["temperature"] = 0
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "temperature"

    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"]["max_tokens"] = 1
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "questions.is_urgent.max_tokens"


def test_authentication_runs_before_models_and_systemone(env):
    assert env.client.get("/typesafe/v1/models").status_code == 401
    assert env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"]).status_code == 401


def test_policy_denial_from_dispatch_is_safe_error(env):
    env.fakes.allowed = False
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "message": "This profile cannot use that decision model.",
        "type": "invalid_request_error",
        "code": "model_not_allowed",
        "param": "model",
    }


def test_decisions_error_headers_are_forwarded(env):
    async def overloaded(_auth, _profile, _body, _request_id):
        raise DecisionsError(529, "overloaded", "Decision engine is starting.", headers={"retry-after": "5"})

    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=env.fakes.authenticate,
            profile_for=env.fakes.profile_for,
            models_for=env.fakes.models_for,
            dispatch=overloaded,
            record_receipt=env.fakes.record_receipt,
        ),
    )
    resp = TestClient(app).post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 529
    assert resp.headers["retry-after"] == "5"
    assert env.fakes.receipts[0][1].error_code == "overloaded"


def test_backend_exception_does_not_leak_details(env):
    def boom(_body):
        raise RuntimeError("secret backend path")

    env.fakes.reply = boom
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"
    assert "secret" not in resp.text
    assert env.fakes.receipts[0][1].error_code == "decision_backend_error"


def test_invalid_backend_response_is_rejected_before_client_response(env):
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body={"model": "decider-test", "answers": {"q": {"noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 0}},
        backend="decider-cpu",
    )
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"
    assert env.fakes.receipts[0][1].error_code == "decision_backend_error"


def test_receipt_callback_failure_does_not_break_success(env):
    fixture = _fixture("typesafe_systemone_noul.json")

    def failing_record(_auth, _receipt):
        raise RuntimeError("receipt db down")

    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=env.fakes.authenticate,
            profile_for=env.fakes.profile_for,
            models_for=env.fakes.models_for,
            dispatch=env.fakes.dispatch,
            record_receipt=failing_record,
        ),
    )
    resp = TestClient(app).post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 200
    assert resp.json()["answers"]["is_urgent"]["type"] == "noul"


def test_free_fallback_reasons_are_reported_in_headers_and_receipts(env):
    fixture = _fixture("typesafe_systemone_noul.json")
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=fixture["response"],
        backend="decider-cpu",
        resolved_model="jev-1.13.0",
        engine="cpu",
        fallback_reasons=("decider-gpu:529",),
    )
    resp = env.client.post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 200
    assert resp.headers["x-gateway-fallback-reasons"] == "decider-gpu:529"
    assert env.fakes.receipts[0][1].fallback_reasons == ("decider-gpu:529",)


def test_gpu_engine_reported_by_backend_is_propagated(env):
    fixture = _fixture("typesafe_systemone_noul.json")
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=fixture["response"],
        backend="decider-gpu",
        resolved_model="jev-1.13.0",
        engine="gpu",
    )
    resp = env.client.post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 200
    assert resp.headers["x-decision-engine"] == "gpu"
    assert env.fakes.receipts[0][1].engine == "gpu"


def test_reserved_openai_decisions_hook_is_501(env):
    resp = env.client.post("/v1/decisions", json={"input": "future schema"}, headers=H)
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "decisions_backend_unavailable"


def test_reserved_openai_decisions_can_call_injected_passthrough(env):
    seen = {}

    async def hook(request, auth, profile):
        seen["auth"] = auth.app_profile
        seen["profile"] = profile.id
        return JSONResponse({"ok": True}, status_code=202)

    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=env.fakes.authenticate,
            profile_for=env.fakes.profile_for,
            models_for=env.fakes.models_for,
            dispatch=env.fakes.dispatch,
            record_receipt=env.fakes.record_receipt,
            openai_decisions=hook,
        ),
    )
    resp = TestClient(app).post("/v1/decisions", json={"input": "future schema"}, headers=H)
    assert resp.status_code == 202
    assert resp.json() == {"ok": True}
    assert seen == {"auth": "family", "profile": "family"}


def test_official_typesafe_sdk_works_offline_against_facade(env):
    typesafe_sdk = pytest.importorskip("typesafe_sdk")
    httpx2 = pytest.importorskip("httpx2")
    transport = TestClientTransport(env.client, httpx2)
    http_client = httpx2.Client(transport=transport, base_url="http://gateway.test/typesafe")

    with typesafe_sdk.TypeSafeClient(api_key="good", base_url="http://gateway.test/typesafe", http_client=http_client) as client:
        models = client.models.list()
        assert [model.name for model in models.models] == ["decide", "decide-quality"]
        result = client.system_one(
            state="Help! My payouts have been failing for 3 days.",
            questions={"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}},
            model="jev-latest",
        )
        assert result.model == "decider-2b-v11"
        assert result.answers["is_urgent"].type == "noul"
        assert 0 <= result.answers["is_urgent"].noul <= 1
