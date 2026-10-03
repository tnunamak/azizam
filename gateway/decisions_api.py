"""TypeSafe System One and reserved OpenAI Decisions facade routes.

The gateway owns authentication, profile policy, routing and receipts; the
decision backend owns prompt/readout/model knowledge. This module only validates
the TypeSafe wire shape, calls an injected backend dispatcher, and returns the
backend's TypeSafe-shaped body.
"""

from __future__ import annotations

import inspect
import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway.receipts import new_request_id

JSON = dict[str, Any]
State = str | list[Any] | dict[str, Any]
ModelsResult = Sequence["DecisionModel"] | Sequence[Mapping[str, Any]]


MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
DEFAULT_LIMITS = {
    "max_options": MAX_CHOICE_OPTIONS,
    "max_levels": MAX_SCORE_LEVELS,
    "max_state_tokens": 32768,
}


class DecisionsError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        param: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.headers = dict(headers or {})


class BackendShapeError(Exception):
    pass


@dataclass(frozen=True)
class DecisionModel:
    """One model shown through ``/typesafe/v1/models``."""

    id: str
    object: str = "model"
    owned_by: str = "vivid-fish"
    root: str | None = None
    parent: str | None = None
    backend: str | None = None
    backend_kind: str = "local"
    cost_class: str = "free-local"
    remote: bool = False
    available: bool = True
    reason: str | None = None
    engine: str | None = None
    limits: Mapping[str, Any] = field(default_factory=lambda: DEFAULT_LIMITS)
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def view(self) -> JSON:
        out: JSON = {
            "name": self.id,
            "description": str(self.capabilities.get("description") or self.root or self.id),
            "release_date": str(self.capabilities.get("release_date") or "2026-10-03"),
        }
        meta = {
            "backend": self.backend,
            "backend_kind": self.backend_kind,
            "cost_class": self.cost_class,
            "remote": self.remote,
            "available": self.available,
            "reason": self.reason,
            "engine": self.engine,
            "limits": dict(self.limits),
            **dict(self.capabilities),
        }
        out["capabilities"] = {k: v for k, v in meta.items() if v is not None}
        return out


@dataclass(frozen=True)
class DecisionDispatchResult:
    """Backend result. Body must already be TypeSafe-shaped."""

    body: Mapping[str, Any]
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    backend: str | None = None
    resolved_model: str | None = None
    remote: bool = False
    cost_usd: float | str | None = 0.0
    engine: str | None = None
    fallback_reasons: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class DecisionReceipt:
    protocol: str
    intent: str
    request_id: str
    path: str
    requested_model: str | None
    resolved_model: str | None
    backend: str | None
    question_count: int
    latency_ms: float
    status_code: int
    remote: bool
    cost_usd: float | str | None
    engine: str | None
    fallback_reasons: Sequence[str] = field(default_factory=tuple)
    error_code: str | None = None
    error_param: str | None = None
    usage: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DecisionsApiDeps:
    authenticate: Callable[[Request, str], Any]
    profile_for: Callable[[Any], Any]
    models_for: Callable[[Any, Any], Awaitable[ModelsResult] | ModelsResult]
    dispatch: Callable[[Any, Any, Mapping[str, Any], str], Awaitable[DecisionDispatchResult] | DecisionDispatchResult]
    record_receipt: Callable[[Any, DecisionReceipt], Awaitable[None] | None] | None = None
    openai_decisions: Callable[[Request, Any, Any], Awaitable[Response] | Response] | None = None


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: JSON = {"message": message, "type": "invalid_request_error", "code": code}
    if param is not None:
        error["param"] = param
    return JSONResponse({"error": error}, status_code=status, headers=dict(headers or {}))


def _ensure_json_object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DecisionsError(422, "invalid_request", "Request body must be a JSON object.")
    return value


async def _record_receipt(
    deps: "DecisionsApiDeps",
    auth: Any,
    receipt: DecisionReceipt,
) -> None:
    if deps.record_receipt is None:
        return
    try:
        await _call_maybe_async(deps.record_receipt, auth, receipt)
    except Exception:
        return


def _receipt(
    *,
    request_id: str,
    path: str,
    started: float,
    requested_model: str | None = None,
    resolved_model: str | None = None,
    backend: str | None = None,
    question_count: int = 0,
    status_code: int,
    remote: bool = False,
    cost_usd: float | str | None = 0.0,
    engine: str | None = None,
    fallback_reasons: Sequence[str] = (),
    error_code: str | None = None,
    error_param: str | None = None,
    usage: Mapping[str, Any] | None = None,
) -> DecisionReceipt:
    return DecisionReceipt(
        protocol="typesafe.systemone",
        intent="decision.systemone",
        request_id=request_id,
        path=path,
        requested_model=requested_model,
        resolved_model=resolved_model,
        backend=backend,
        question_count=question_count,
        latency_ms=(time.perf_counter() - started) * 1000,
        status_code=status_code,
        remote=remote,
        cost_usd=cost_usd,
        engine=engine,
        fallback_reasons=tuple(fallback_reasons),
        error_code=error_code,
        error_param=error_param,
        usage=usage,
    )


def _is_structured_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return True
    if isinstance(value, Mapping):
        return True
    return False


def validate_systemone_request(payload: Any) -> JSON:
    body = _ensure_json_object(payload)
    _reject_unknown_fields(body, {"state", "model", "questions"}, "body")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise DecisionsError(422, "invalid_model", "model is required.", param="model")
    if "state" not in body or not isinstance(body["state"], (str, list, dict)):
        raise DecisionsError(422, "invalid_state", "state must be a string, object, or array.", param="state")
    questions = body.get("questions")
    if not isinstance(questions, Mapping) or not questions:
        raise DecisionsError(422, "invalid_questions", "questions must be a non-empty object.", param="questions")

    normalized_questions: JSON = {}
    for question_id, raw_question in questions.items():
        if not isinstance(question_id, str) or not question_id:
            raise DecisionsError(422, "invalid_question_id", "question ids must be non-empty strings.", param="questions")
        question = _ensure_json_object(raw_question)
        normalized_questions[question_id] = _validate_question(question_id, question)

    return {
        **dict(body),
        "model": model.strip(),
        "questions": normalized_questions,
    }


def _validate_question(question_id: str, question: Mapping[str, Any]) -> JSON:
    _reject_unknown_fields(question, {"type", "instructions", "criteria"}, f"questions.{question_id}")
    qtype = question.get("type")
    param = f"questions.{question_id}"
    if not isinstance(qtype, str) or qtype not in {"noul", "choice", "score"}:
        raise DecisionsError(422, "invalid_question_type", "question type must be noul, choice, or score.", param=f"{param}.type")

    if qtype == "noul":
        criteria = question.get("criteria")
        if criteria is not None:
            if not isinstance(criteria, Mapping):
                raise DecisionsError(422, "invalid_criteria", "noul criteria must be an object.", param=f"{param}.criteria")
            for key in criteria:
                if key not in {"true", "false"}:
                    raise DecisionsError(422, "invalid_criteria", "noul criteria may only contain true and false.", param=f"{param}.criteria")
                if not _is_structured_text(criteria[key]):
                    raise DecisionsError(422, "invalid_criteria", "noul criteria values must be strings, objects, or arrays.", param=f"{param}.criteria.{key}")
        if "instructions" in question and not _is_structured_text(question.get("instructions")):
            raise DecisionsError(422, "invalid_instructions", "instructions must be a string, object, or array when present.", param=f"{param}.instructions")
        if "instructions" not in question and not criteria:
            raise DecisionsError(422, "invalid_instructions", "instructions or non-empty criteria is required.", param=f"{param}.instructions")
        return dict(question)

    if not _is_structured_text(question.get("instructions")):
        raise DecisionsError(422, "invalid_instructions", "instructions is required.", param=f"{param}.instructions")

    if qtype == "choice":
        criteria = question.get("criteria")
        if not isinstance(criteria, Mapping):
            raise DecisionsError(422, "invalid_criteria", "choice criteria must be an object.", param=f"{param}.criteria")
        count = len(criteria)
        if count < 2 or count > MAX_CHOICE_OPTIONS:
            raise DecisionsError(
                422,
                "invalid_criteria",
                f"choice criteria must contain 2 to {MAX_CHOICE_OPTIONS} options.",
                param=f"{param}.criteria",
            )
        for option, description in criteria.items():
            if not isinstance(option, str) or not option:
                raise DecisionsError(422, "invalid_criteria", "choice option names must be non-empty strings.", param=f"{param}.criteria")
            if description is not None and not _is_structured_text(description):
                raise DecisionsError(422, "invalid_criteria", "choice descriptions must be string, object, array, or null.", param=f"{param}.criteria.{option}")
        return dict(question)

    criteria = question.get("criteria")
    if not isinstance(criteria, list):
        raise DecisionsError(422, "invalid_criteria", "score criteria must be an array.", param=f"{param}.criteria")
    if len(criteria) < MIN_SCORE_LEVELS or len(criteria) > MAX_SCORE_LEVELS:
        raise DecisionsError(
            422,
            "invalid_criteria",
            f"score criteria must contain {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels.",
            param=f"{param}.criteria",
        )
    for index, description in enumerate(criteria):
        if not _is_structured_text(description):
            raise DecisionsError(422, "invalid_criteria", "score level descriptions must be strings, objects, or arrays.", param=f"{param}.criteria.{index}")
    return dict(question)


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], param: str) -> None:
    for key in value:
        if key not in allowed:
            field = key if param == "body" else f"{param}.{key}"
            raise DecisionsError(422, "unsupported_field", f"{field} is not supported by this facade.", param=field)


def _model_view(model: DecisionModel | Mapping[str, Any]) -> JSON:
    if isinstance(model, DecisionModel):
        return model.view()
    out = dict(model)
    out.setdefault("object", "model")
    return out


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_maybe_async(func: Callable[..., Any], *args: Any) -> Any:
    if inspect.iscoroutinefunction(func):
        return await func(*args)
    return await _maybe_await(await asyncio.to_thread(func, *args))


def validate_systemone_response(body: Mapping[str, Any], request_body: Mapping[str, Any]) -> None:
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise BackendShapeError("response.model must be a non-empty string")
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        raise BackendShapeError("response.usage must be an object")
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise BackendShapeError(f"response.usage.{key} must be a non-negative integer")
    answers = body.get("answers")
    if not isinstance(answers, Mapping):
        raise BackendShapeError("response.answers must be an object")
    questions = request_body.get("questions")
    if not isinstance(questions, Mapping):
        raise BackendShapeError("request questions missing")
    if set(answers) != set(questions):
        raise BackendShapeError("response.answers keys must match request questions")
    for answer_id, answer in answers.items():
        if not isinstance(answer_id, str) or not isinstance(answer, Mapping):
            raise BackendShapeError("response.answers must map ids to answer objects")
        question = questions[answer_id]
        atype = answer.get("type")
        if atype != question.get("type"):
            raise BackendShapeError("answer.type must match question.type")
        if atype == "noul":
            _expect_probability(answer, "noul")
        elif atype == "choice":
            if not isinstance(answer.get("choice"), str):
                raise BackendShapeError("choice answer requires a string choice")
            criteria = question.get("criteria")
            if not isinstance(criteria, Mapping):
                raise BackendShapeError("choice request criteria missing")
            if answer["choice"] not in criteria:
                raise BackendShapeError("choice answer choice must be one of request criteria")
            _expect_probability(answer, "confidence")
            _expect_probabilities(answer, set(criteria), normalize_keys=False)
        elif atype == "score":
            criteria = question.get("criteria")
            if not isinstance(criteria, list):
                raise BackendShapeError("score request criteria missing")
            score = _expect_number(answer, "score")
            if not 0 <= score <= len(criteria) - 1:
                raise BackendShapeError("score answer must be within rubric range")
            _expect_probability(answer, "confidence")
            legend = answer.get("legend")
            if not isinstance(legend, Mapping):
                raise BackendShapeError("score answer requires legend")
            expected = {str(i) for i in range(len(criteria))}
            if {_score_key(k) for k in legend} != expected:
                raise BackendShapeError("score legend keys must match rubric indexes")
            _expect_probabilities(answer, expected, normalize_keys=True)
        else:
            raise BackendShapeError("answer.type must be noul, choice, or score")


def _expect_number(value: Mapping[str, Any], key: str) -> float:
    raw = value.get(key)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(float(raw)):
        raise BackendShapeError(f"answer.{key} must be a number")
    return float(raw)


def _expect_probability(value: Mapping[str, Any], key: str) -> float:
    raw = _expect_number(value, key)
    if not 0 <= raw <= 1:
        raise BackendShapeError(f"answer.{key} must be between 0 and 1")
    return raw


def _score_key(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return str(int(value))
    return str(value)


def _expect_probabilities(value: Mapping[str, Any], expected_keys: set[str], *, normalize_keys: bool) -> None:
    probabilities = value.get("probabilities")
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise BackendShapeError("answer.probabilities must be a non-empty object")
    normalized_keys = {_score_key(k) for k in probabilities} if normalize_keys else set(probabilities)
    if normalized_keys != expected_keys:
        raise BackendShapeError("answer.probabilities keys must match request criteria")
    total = 0.0
    for probability in probabilities.values():
        if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not math.isfinite(float(probability)):
            raise BackendShapeError("answer.probabilities values must be finite numbers")
        if not 0 <= float(probability) <= 1:
            raise BackendShapeError("answer.probabilities values must be between 0 and 1")
        total += float(probability)
    tolerance = max(len(probabilities) * 0.0001, 0.0001)
    if abs(total - 1.0) > tolerance:
        raise BackendShapeError("answer.probabilities values must sum to 1")


def _response_headers(request_id: str, result: DecisionDispatchResult) -> dict[str, str]:
    headers = {
        "x-gateway-remote": "1" if result.remote else "0",
        "x-gateway-cost-usd": str(result.cost_usd if result.cost_usd is not None else 0),
    }
    headers.update({str(k): str(v) for k, v in result.headers.items()})
    headers["x-gateway-request-id"] = request_id
    if result.backend:
        headers["x-gateway-backend"] = result.backend
    if result.engine:
        headers["x-gateway-engine"] = result.engine
        headers["x-decision-engine"] = result.engine
    if result.fallback_reasons:
        headers["x-gateway-fallback-reasons"] = ",".join(result.fallback_reasons)
    return headers


def _request_headers(request_id: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = {"x-gateway-request-id": request_id}
    headers.update({str(k): str(v) for k, v in (extra or {}).items()})
    return headers


def register_decision_routes(app: FastAPI, deps: DecisionsApiDeps) -> None:
    def guard(request: Request, path: str) -> tuple[Any, Any] | Response:
        auth = deps.authenticate(request, path)
        if isinstance(auth, Response):
            return auth
        return auth, deps.profile_for(auth)

    @app.get("/typesafe/v1/models")
    async def list_typesafe_models(request: Request) -> Response:
        gate = guard(request, "/typesafe/v1/models")
        if isinstance(gate, Response):
            return error_response(401, "unauthorized", "Missing or invalid API key.")
        auth, profile = gate
        models = await _call_maybe_async(deps.models_for, auth, profile)
        return JSONResponse({"models": [_model_view(m) for m in models]})

    async def systemone(request: Request) -> Response:
        request_id = new_request_id()
        started = time.perf_counter()
        gate = guard(request, request.url.path)
        if isinstance(gate, Response):
            return error_response(
                401,
                "unauthorized",
                "Missing or invalid API key.",
                headers=_request_headers(request_id),
            )
        auth, profile = gate
        try:
            raw = await request.json()
            body = validate_systemone_request(raw)
        except DecisionsError as exc:
            await _record_receipt(
                deps,
                auth,
                _receipt(
                    request_id=request_id,
                    path=request.url.path,
                    started=started,
                    status_code=exc.status,
                    error_code=exc.code,
                    error_param=exc.param,
                ),
            )
            return error_response(
                exc.status,
                exc.code,
                exc.message,
                param=exc.param,
                headers=_request_headers(request_id, exc.headers),
            )
        except Exception:
            await _record_receipt(
                deps,
                auth,
                _receipt(
                    request_id=request_id,
                    path=request.url.path,
                    started=started,
                    status_code=422,
                    error_code="invalid_json",
                ),
            )
            return error_response(
                422,
                "invalid_json",
                "Request body must be valid JSON.",
                headers=_request_headers(request_id),
            )

        try:
            result = await _call_maybe_async(deps.dispatch, auth, profile, body, request_id)
        except DecisionsError as exc:
            await _record_receipt(
                deps,
                auth,
                _receipt(
                    request_id=request_id,
                    path=request.url.path,
                    started=started,
                    requested_model=body.get("model"),
                    question_count=len(body["questions"]),
                    status_code=exc.status,
                    error_code=exc.code,
                    error_param=exc.param,
                ),
            )
            return error_response(
                exc.status,
                exc.code,
                exc.message,
                param=exc.param,
                headers=_request_headers(request_id, exc.headers),
            )
        except Exception:
            await _record_receipt(
                deps,
                auth,
                _receipt(
                    request_id=request_id,
                    path=request.url.path,
                    started=started,
                    requested_model=body.get("model"),
                    question_count=len(body["questions"]),
                    status_code=502,
                    error_code="decision_backend_error",
                ),
            )
            return error_response(
                502,
                "decision_backend_error",
                "The decision backend did not return a usable response.",
                headers=_request_headers(request_id),
            )

        if 200 <= result.status_code < 300:
            try:
                validate_systemone_response(result.body, body)
            except BackendShapeError:
                await _record_receipt(
                    deps,
                    auth,
                    _receipt(
                        request_id=request_id,
                        path=request.url.path,
                        requested_model=body.get("model"),
                        question_count=len(body["questions"]),
                        status_code=502,
                        error_code="decision_backend_error",
                        started=started,
                    ),
                )
                return error_response(
                    502,
                    "decision_backend_error",
                    "The decision backend did not return a usable response.",
                    headers=_request_headers(request_id),
                )

        await _record_receipt(
            deps,
            auth,
            _receipt(
                request_id=request_id,
                path=request.url.path,
                requested_model=body.get("model"),
                resolved_model=result.resolved_model or str(result.body.get("model") or ""),
                backend=result.backend,
                question_count=len(body["questions"]),
                status_code=result.status_code,
                remote=result.remote,
                cost_usd=result.cost_usd,
                engine=result.engine,
                fallback_reasons=tuple(result.fallback_reasons),
                usage=result.body.get("usage") if isinstance(result.body.get("usage"), Mapping) else None,
                started=started,
            ),
        )

        return JSONResponse(dict(result.body), status_code=result.status_code, headers=_response_headers(request_id, result))

    app.add_api_route("/v1/systemone", systemone, methods=["POST"])
    app.add_api_route("/typesafe/v1/systemone", systemone, methods=["POST"])

    @app.post("/v1/decisions")
    async def reserved_openai_decisions(request: Request) -> Response:
        gate = guard(request, "/v1/decisions")
        if isinstance(gate, Response):
            return error_response(401, "unauthorized", "Missing or invalid API key.")
        auth, profile = gate
        if deps.openai_decisions is not None:
            return await _call_maybe_async(deps.openai_decisions, request, auth, profile)
        return error_response(
            501,
            "decisions_backend_unavailable",
            "OpenAI /v1/decisions is reserved, but no configured OpenAI Decisions passthrough is available.",
        )
