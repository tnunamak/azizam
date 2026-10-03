from __future__ import annotations

import asyncio
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
                selected = min(1, len(question["criteria"]) - 1)
                answers[question_id] = {
                    "type": "score",
                    "score": float(selected),
                    "legend": {str(i): value for i, value in enumerate(question["criteria"])},
                    "probabilities": {str(i): 1.0 if i == selected else 0.0 for i, _ in enumerate(question["criteria"])},
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


def _assert_detail_error(resp, param: str | None = None, needle: str | None = None) -> dict:
    payload = resp.json()
    assert "error" not in payload
    detail = payload["detail"]
    assert isinstance(detail, list) and len(detail) == 1
    item = detail[0]
    assert isinstance(item["loc"], list)
    assert isinstance(item["msg"], str) and item["msg"]
    assert isinstance(item["type"], str) and item["type"]
    if param is not None:
        assert item["loc"] == ["body", *[int(part) if part.isdigit() else part for part in param.split(".")]]
    if needle is not None:
        assert needle in item["msg"]
    return item


class GatewaySdkTransport:
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
    assert receipt.usage == {"input_tokens": 307, "output_tokens": 20}


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


def test_pinned_openapi_documents_nullable_noul_fields_and_detail_validation_envelope():
    schema = _fixture("typesafe_openapi.json")
    schemas = schema["components"]["schemas"]

    noul_question = schemas["NoulQuestion"]
    assert {variant.get("type") for variant in noul_question["properties"]["instructions"]["anyOf"]} >= {"string", "object", "array", "null"}
    assert {variant.get("type") for variant in noul_question["properties"]["criteria"]["anyOf"]} >= {"null"}

    noul_criteria = schemas["NoulCriteria"]
    assert {variant.get("type") for variant in noul_criteria["properties"]["true"]["anyOf"]} >= {"string", "object", "array", "null"}
    assert {variant.get("type") for variant in noul_criteria["properties"]["false"]["anyOf"]} >= {"string", "object", "array", "null"}

    validation_error = schemas["HTTPValidationError"]
    assert validation_error["required"] == ["detail"] if "required" in validation_error else True
    assert validation_error["properties"]["detail"]["type"] == "array"
    assert schemas["ValidationError"]["required"] == ["loc", "msg", "type"]


def test_typesafe_noul_accepts_null_instructions_with_meaningful_nullable_criteria(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"] = {
        "type": "noul",
        "instructions": None,
        "criteria": {"true": "The message is unsolicited advertising.", "false": None},
    }

    resp = env.client.post("/v1/systemone", json=body, headers=H)

    assert resp.status_code == 200, resp.text
    assert env.fakes.calls[0][1]["questions"]["is_urgent"]["instructions"] is None
    assert env.fakes.calls[0][1]["questions"]["is_urgent"]["criteria"]["false"] is None


def test_systemone_request_validation_errors_use_typesafe_detail_envelope(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    del body["state"]

    resp = env.client.post("/v1/systemone", json=body, headers=H)

    assert resp.status_code == 422
    payload = resp.json()
    assert "error" not in payload
    assert payload == {
        "detail": [
            {
                "loc": ["body", "state"],
                "msg": "Field required",
                "type": "missing",
            }
        ]
    }
    detail_schema = _fixture("typesafe_openapi.json")["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"]
    assert detail_schema["type"] == "array"
    for item in payload["detail"]:
        assert isinstance(item["loc"], list)
        assert all(isinstance(part, (str, int)) for part in item["loc"])
        assert isinstance(item["msg"], str) and item["msg"]
        assert isinstance(item["type"], str) and item["type"]


@pytest.mark.parametrize(
    "patch,param,needle",
    [
        ({"model": ""}, "model", "Field required"),
        ({"state": 3}, "state", "state must be a string"),
        ({"questions": {}}, "questions", "questions must be a non-empty object"),
        ({"questions": {"q": {"type": "maybe", "instructions": "x"}}}, "questions.q.type", "question type"),
        ({"questions": {"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}}}, "questions.q.criteria", "2 to 255"),
    ],
)


def test_validation_errors_are_typesafe_shaped_and_do_not_call_backend(env, patch, param, needle):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body.update(patch)
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param=param, needle=needle)
    assert env.fakes.calls == []
    assert env.fakes.receipts[0][1].error_param == param


def test_unknown_top_level_and_question_fields_are_rejected(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["temperature"] = 0
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param="temperature")

    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"]["max_tokens"] = 1
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param="questions.is_urgent.max_tokens")


def test_openapi_nullable_instruction_rules_cover_choice_and_score_questions():
    schemas = _fixture("typesafe_openapi.json")["components"]["schemas"]

    choice_question = schemas["ChoiceQuestion"]
    assert choice_question["required"] == ["criteria", "type"]
    assert {variant.get("type") for variant in choice_question["properties"]["instructions"]["anyOf"]} >= {"string", "object", "array", "null"}
    choice_description = choice_question["properties"]["criteria"]["additionalProperties"]
    assert {variant.get("type") for variant in choice_description["anyOf"]} >= {"string", "object", "array", "null"}

    score_question = schemas["ScoreQuestion"]
    assert score_question["required"] == ["criteria", "type"]
    assert {variant.get("type") for variant in score_question["properties"]["instructions"]["anyOf"]} >= {"string", "object", "array", "null"}
    assert score_question["properties"]["criteria"]["minItems"] == 1


def test_choice_accepts_missing_or_null_instructions_when_criteria_are_descriptive(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"] = {
        "department": {
            "type": "choice",
            "criteria": {"billing": "Billing issue", "support": "General support issue"},
        },
        "tone": {
            "type": "choice",
            "instructions": None,
            "criteria": {"angry": "Upset or hostile", "calm": "Neutral or polite"},
        },
    }

    resp = env.client.post("/v1/systemone", json=body, headers=H)

    assert resp.status_code == 200, resp.text
    sent = env.fakes.calls[0][1]["questions"]
    assert "instructions" not in sent["department"]
    assert sent["tone"]["instructions"] is None



def test_choice_accepts_null_instructions_with_null_descriptions_because_option_names_are_meaningful(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"] = {
        "department": {
            "type": "choice",
            "instructions": None,
            "criteria": {"billing": None, "support": None},
        }
    }

    resp = env.client.post("/v1/systemone", json=body, headers=H)

    assert resp.status_code == 200, resp.text
    sent = env.fakes.calls[0][1]["questions"]["department"]
    assert sent["instructions"] is None
    assert sent["criteria"] == {"billing": None, "support": None}


def test_score_accepts_null_instructions_with_descriptive_single_level_criteria(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"] = {
        "severity": {
            "type": "score",
            "instructions": None,
            "criteria": ["Needs immediate attention"],
        }
    }

    resp = env.client.post("/v1/systemone", json=body, headers=H)

    assert resp.status_code == 200, resp.text
    sent = env.fakes.calls[0][1]["questions"]["severity"]
    assert sent["instructions"] is None
    assert sent["criteria"] == ["Needs immediate attention"]


def test_validation_handles_non_string_type_and_criteria_only_noul(env):
    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"]["type"] = ["noul"]
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param="questions.is_urgent.type")

    body = _fixture("typesafe_systemone_noul.json")["request"]
    del body["questions"]["is_urgent"]["instructions"]
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 200

    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"]["instructions"] = 7
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param="questions.is_urgent.instructions")

    body = _fixture("typesafe_systemone_noul.json")["request"]
    body["questions"]["is_urgent"]["criteria"]["true"] = 1
    resp = env.client.post("/v1/systemone", json=body, headers=H)
    assert resp.status_code == 422
    _assert_detail_error(resp, param="questions.is_urgent.criteria.true")


def test_malformed_json_returns_422_and_records_receipt(env):
    resp = env.client.post(
        "/v1/systemone",
        content=b'{"state":',
        headers={**H, "content-type": "application/json"},
    )
    assert resp.status_code == 422
    _assert_detail_error(resp, needle="valid JSON")
    assert resp.headers["x-gateway-request-id"].startswith("req_")
    assert env.fakes.calls == []
    assert env.fakes.receipts[0][1].status_code == 422
    assert env.fakes.receipts[0][1].error_code == "invalid_json"

def test_authentication_runs_before_models_and_systemone(env):
    assert env.client.get("/typesafe/v1/models").status_code == 401
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"])
    assert resp.status_code == 401
    assert resp.headers["x-gateway-request-id"].startswith("req_")


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


def test_systemone_dispatch_is_cancelled_when_client_disconnects_after_body():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    continued_after_disconnect = asyncio.Event()

    async def dispatch(_auth, _profile, _body, _request_id):
        started.set()
        try:
            await asyncio.sleep(0.2)
            continued_after_disconnect.set()
            return DecisionDispatchResult(
                body={
                    "model": "decider-2b-v11",
                    "answers": {"is_urgent": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
                backend="decider-cpu",
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise

    fakes = FakeDeps()
    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=fakes.authenticate,
            profile_for=fakes.profile_for,
            models_for=fakes.models_for,
            dispatch=dispatch,
            record_receipt=fakes.record_receipt,
        ),
    )
    body = json.dumps(_fixture("typesafe_systemone_noul.json")["request"]).encode("utf-8")
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    async def run_app():
        scope = {
            "type": "http",
            "asgi": {"spec_version": "2.3", "version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/systemone",
            "raw_path": b"/v1/systemone",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer good"), (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("gateway.test", 80),
            "root_path": "",
        }
        await app(scope, receive, send)

    asyncio.run(asyncio.wait_for(run_app(), timeout=2))

    assert cancelled.is_set()
    assert not continued_after_disconnect.is_set()
    assert fakes.receipts[0][1].status_code == 499
    assert fakes.receipts[0][1].error_code == "client_disconnected"
    assert sent == []


def test_backend_exception_does_not_leak_details(env):
    def boom(_body):
        raise RuntimeError("secret backend path")

    env.fakes.reply = boom
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"
    assert "secret" not in resp.text
    assert env.fakes.receipts[0][1].error_code == "decision_backend_error"


@pytest.mark.parametrize("status,headers", [(422, {}), (429, {"retry-after": "3"}), (529, {"retry-after": "5"})])
def test_backend_error_statuses_are_relayed_without_response_validation(env, status, headers):
    body = {"error": {"type": "invalid_request_error", "message": "backend says no", "code": "backend_code"}}
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=body,
        status_code=status,
        headers=headers,
        backend="decider-cpu",
    )
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == status
    if status == 422:
        _assert_detail_error(resp, needle="backend says no")
    else:
        assert resp.json() == body
    assert resp.headers["x-gateway-request-id"].startswith("req_")
    for key, value in headers.items():
        assert resp.headers[key] == value
    assert env.fakes.receipts[0][1].status_code == status



def test_backend_422_detail_is_preserved(env):
    body = {"detail": [{"loc": ["body", "state"], "msg": "backend context too long", "type": "context_length_exceeded"}]}
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body=body,
        status_code=422,
        backend="decider-cpu",
    )

    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)

    assert resp.status_code == 422
    assert resp.json() == body
    assert env.fakes.receipts[0][1].status_code == 422



def test_backend_422_invalid_detail_falls_back_to_valid_detail(env):
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body={"detail": [{"loc": ["body", True], "msg": "bad", "type": "bad"}]},
        status_code=422,
        backend="decider-cpu",
    )

    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)

    assert resp.status_code == 422
    item = resp.json()["detail"][0]
    assert item["loc"] == ["body"]
    assert item["msg"] == "Validation error"
    assert item["type"] == "model_attributes_type"


def test_invalid_backend_response_is_rejected_before_client_response(env):
    env.fakes.reply = lambda _body: DecisionDispatchResult(
        body={"model": "decider-test", "answers": {"q": {"noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 0}},
        backend="decider-cpu",
    )
    resp = env.client.post("/v1/systemone", json=_fixture("typesafe_systemone_noul.json")["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"
    assert env.fakes.receipts[0][1].error_code == "decision_backend_error"


def test_invalid_backend_numbers_do_not_become_json_500(env):
    fixture = _fixture("typesafe_systemone_noul.json")
    bad = json.loads(json.dumps(fixture["response"]))
    bad["answers"]["is_urgent"]["noul"] = float("nan")
    env.fakes.reply = lambda _body: DecisionDispatchResult(body=bad, backend="decider-cpu")
    resp = env.client.post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["answers"].pop("frustration"),
        lambda body: body["answers"]["department"].update({"type": "score"}),
        lambda body: body["answers"]["department"].update({"choice": "support"}),
        lambda body: body["answers"]["department"].update({"probabilities": {"billing": 0.4, "technical": 0.4, "sales": 0.4}}),
        lambda body: body["answers"]["department"].update({"confidence": 1.2}),
        lambda body: body["answers"]["frustration"].update({"score": 3.1}),
        lambda body: body["answers"]["frustration"].update({"legend": {"0": "Calm", "1": "Frustrated"}}),
        lambda body: body["answers"]["frustration"].update({"probabilities": {"0": 0.5, "1": 0.5}}),
        lambda body: body["usage"].update({"input_tokens": -1}),
    ],
)
def test_backend_response_must_match_request_shape_and_domains(env, mutate):
    fixture = _fixture("typesafe_systemone_choice_score.json")
    bad = json.loads(json.dumps(fixture["response"]))
    mutate(bad)
    env.fakes.reply = lambda _body: DecisionDispatchResult(body=bad, backend="decider-cpu")
    resp = env.client.post("/v1/systemone", json=fixture["request"], headers=H)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "decision_backend_error"


def test_sync_dependency_returning_awaitable_is_awaited(env):
    async def models_result():
        return [DecisionModel(id="decide", root="decider-2b-v11")]

    def models_for(_auth, _profile):
        return models_result()

    app = FastAPI()
    register_decision_routes(
        app,
        DecisionsApiDeps(
            authenticate=env.fakes.authenticate,
            profile_for=env.fakes.profile_for,
            models_for=models_for,
            dispatch=env.fakes.dispatch,
        ),
    )
    resp = TestClient(app).get("/typesafe/v1/models", headers=H)
    assert resp.status_code == 200
    assert resp.json()["models"][0]["name"] == "decide"


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
    transport = GatewaySdkTransport(env.client, httpx2)
    http_client = httpx2.Client(transport=transport, base_url="http://gateway.test/typesafe")

    with typesafe_sdk.TypeSafeClient(api_key="good", base_url="http://gateway.test/typesafe", http_client=http_client) as client:
        models = client.models.list()
        assert [model.name for model in models.models] == ["decide", "decide-quality"]
        result = client.system_one(
            state="Help! My payouts have been failing for 3 days.",
            questions={
                "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"},
                "department": {
                    "type": "choice",
                    "instructions": "Which team should handle this?",
                    "criteria": {"billing": "Billing", "support": "Support"},
                },
                "severity": {
                    "type": "score",
                    "instructions": "Rate severity.",
                    "criteria": ["low", "medium", "high"],
                },
            },
            model="jev-latest",
        )
        assert result.model == "decider-2b-v11"
        assert result.answers["is_urgent"].type == "noul"
        assert 0 <= result.answers["is_urgent"].noul <= 1
        assert result.answers["department"].type == "choice"
        assert result.answers["department"].choice == "billing"
        assert result.answers["severity"].type == "score"
        assert 0 <= result.answers["severity"].score <= 2

        with pytest.raises(Exception) as excinfo:
            client.system_one(state=3, questions={"q": {"type": "noul", "instructions": "x"}}, model="jev-latest")
        assert "422" in str(excinfo.value) or "state" in str(excinfo.value)

        env.fakes.reply = lambda _body: DecisionDispatchResult(
            body={"error": {"type": "server_error", "message": "busy", "code": "overloaded"}},
            status_code=529,
            headers={"retry-after": "5"},
            backend="decider-cpu",
        )
        with pytest.raises(Exception) as excinfo:
            client.system_one(
                state="retry later",
                questions={"q": {"type": "noul", "instructions": "Is retry needed?"}},
                model="jev-latest",
            )
        assert "529" in str(excinfo.value) or "overloaded" in str(excinfo.value)
