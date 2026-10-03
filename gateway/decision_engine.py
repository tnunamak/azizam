"""Local decision engine contract and option-scoring readout.

This module is pure gateway-adjacent code: it validates the TypeSafe
``/v1/systemone`` body, defines the backend-independent scoring interface, and
builds TypeSafe-shaped answers from one logprob readout per question. The
llama.cpp prompt construction lives in ``engines/decision_engine_server.py`` so
gateway core code does not learn model-specific prompt details.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

DEFAULT_MODEL = "decide"
DEFAULT_LOCAL_MODEL_ID = "decider-2b-v11"
MAX_OPTIONS = 255
MAX_LEVELS = 10
LETTER_TOKEN_IDS = tuple(ord("A") + i for i in range(26))


class DecisionInputError(ValueError):
    """The request body is not a valid TypeSafe systemone request."""

    def __init__(self, message: str, *, code: str = "invalid_request", param: str | None = None):
        super().__init__(message)
        self.code = code
        self.param = param


class DecisionReadoutError(ValueError):
    """The engine readout is incomplete or not numeric."""


@dataclass(frozen=True)
class DecisionQuestion:
    key: str
    kind: str
    instructions: Any
    options: tuple[str, ...]
    names: tuple[Any, ...]
    legend: tuple[str, ...] | None = None
    isolated: bool = True


@dataclass(frozen=True)
class DecisionRequest:
    model: str
    state: Any
    questions: tuple[DecisionQuestion, ...]


@dataclass(frozen=True)
class ScoreContext:
    state: Any
    question: str
    options: tuple[str, ...]
    kind: str
    index: int


class OptionScorer(Protocol):
    """One-pass scorer used by the engine wrapper and fake tests."""

    def score_options(self, ctx: ScoreContext) -> Mapping[Any, float]:
        """Return log probabilities keyed by stable option index."""


def parse_request(body: Any) -> DecisionRequest:
    if not isinstance(body, Mapping):
        raise DecisionInputError("Request body must be a JSON object.")
    model = body.get("model") or DEFAULT_MODEL
    if not isinstance(model, str):
        raise DecisionInputError("model must be a string.", param="model")

    questions = body.get("questions")
    if not isinstance(questions, Mapping) or not questions:
        raise DecisionInputError("questions must be a non-empty object.", param="questions")

    parsed: list[DecisionQuestion] = []
    for key, raw in questions.items():
        if not isinstance(key, str) or not key:
            raise DecisionInputError("question keys must be non-empty strings.", param="questions")
        if not isinstance(raw, Mapping):
            raise DecisionInputError(f"questions.{key} must be an object.", param=f"questions.{key}")
        kind = raw.get("type")
        if kind not in {"choice", "score", "noul"}:
            raise DecisionInputError(
                f"questions.{key}.type must be choice, score, or noul.",
                param=f"questions.{key}.type",
            )
        instructions = raw.get("instructions")
        criteria = raw.get("criteria")
        if instructions in (None, "") and not (kind == "noul" and isinstance(criteria, Mapping) and criteria):
            raise DecisionInputError(
                f"questions.{key}.instructions must be non-empty unless noul criteria describe true or false.",
                param=f"questions.{key}.instructions",
            )
        options, names, legend = _question_options(kind, raw, key)
        parsed.append(
            DecisionQuestion(
                key=key,
                kind=kind,
                instructions=instructions,
                options=options,
                names=names,
                legend=legend,
                isolated=bool(raw.get("isolated", True)),
            )
        )
    return DecisionRequest(model=model, state=body.get("state"), questions=tuple(parsed))


def _text(value: Any) -> str:
    import json

    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _question_text(question: DecisionQuestion) -> str:
    if question.kind == "noul" and question.instructions in (None, ""):
        return "Which answer fits the context?"
    return _text(question.instructions)


def _question_options(kind: str, raw: Mapping[str, Any], key: str) -> tuple[tuple[str, ...], tuple[Any, ...], tuple[str, ...] | None]:
    criteria = raw.get("criteria")
    if kind == "noul":
        if criteria is not None and not isinstance(criteria, Mapping):
            raise DecisionInputError(
                f"questions.{key}.criteria must be an object when present.",
                param=f"questions.{key}.criteria",
            )
        criteria_map = criteria if isinstance(criteria, Mapping) else {}
        false_desc = criteria_map.get("false", criteria_map.get(False))
        true_desc = criteria_map.get("true", criteria_map.get(True))
        options = (
            "no" if false_desc in (None, "") else f"no: {_text(false_desc)}",
            "yes" if true_desc in (None, "") else f"yes: {_text(true_desc)}",
        )
        return options, (False, True), None
    if kind == "score":
        if isinstance(criteria, Mapping):
            criteria = [criteria[k] for k in sorted(criteria, key=float)]
        if not isinstance(criteria, list) or not 2 <= len(criteria) <= MAX_LEVELS:
            raise DecisionInputError(
                f"questions.{key}.criteria must list 2 to {MAX_LEVELS} score levels.",
                param=f"questions.{key}.criteria",
            )
        legend = tuple(_text(value) for value in criteria)
        return tuple(f"{index}: {value}" for index, value in enumerate(legend)), tuple(range(len(legend))), legend
    if not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= MAX_OPTIONS:
        raise DecisionInputError(
            f"questions.{key}.criteria must be an object containing 2 to {MAX_OPTIONS} options.",
            param=f"questions.{key}.criteria",
        )
    names = tuple(str(option) for option in criteria.keys())
    options = tuple(
        name if criteria[name] in (None, "") else f"{name}: {_text(criteria[name])}"
        for name in names
    )
    if len(set(names)) != len(names):
        raise DecisionInputError(
            f"questions.{key}.criteria contains duplicate options.",
            param=f"questions.{key}.criteria",
        )
    return options, names, None


def _planned_rows(req: DecisionRequest) -> tuple[list[tuple[DecisionQuestion, tuple[str, ...]]], list[tuple[DecisionQuestion, int, int, str]]]:
    rows: list[tuple[DecisionQuestion, tuple[str, ...]]] = []
    index: list[tuple[DecisionQuestion, int, int, str]] = []
    for question in req.questions:
        if question.kind == "score" and question.isolated:
            start = len(rows)
            for level in question.legend or ():
                rows.append((question, ("no", "yes")))
            index.append((question, start, len(question.legend or ()), "isolated_score"))
        else:
            index.append((question, len(rows), 1, "list"))
            rows.append((question, question.options))
    return rows, index


def _row_question(question: DecisionQuestion, row_offset: int | None = None) -> str:
    base = _question_text(question)
    if row_offset is not None and question.legend is not None:
        level = _strip_level_number(question.legend[row_offset])
        return f"{base}\nProposed answer: {level}\nDoes the proposed answer fit?"
    return base


def _strip_level_number(text: str) -> str:
    import re

    return re.sub(r"^\s*-?\d+\s*:\s*", "", text)


def _combine_isolated(p_yes: list[float]) -> list[float]:
    total = sum(p_yes)
    if total <= 0 or not math.isfinite(total):
        raise DecisionReadoutError("isolated score rows produced invalid fit mass")
    return [value / total for value in p_yes]


def build_systemone_response(
    req: DecisionRequest,
    scorer: OptionScorer,
    *,
    model: str = DEFAULT_LOCAL_MODEL_ID,
    engine: str = "cpu",
    placement: str = "cpu",
    residency: str = "on_demand",
    start_time: float | None = None,
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter() if start_time is None else start_time
    reset_usage = getattr(scorer, "reset_usage", None)
    if callable(reset_usage):
        reset_usage()
    rows, index = _planned_rows(req)
    contexts: list[ScoreContext] = []
    for row_index, (question, options) in enumerate(rows):
        row_offset = None
        if question.kind == "score" and question.isolated:
            prior_for_question = sum(1 for q, _ in rows[:row_index] if q is question)
            row_offset = prior_for_question
        contexts.append(
            ScoreContext(
                state=req.state,
                question=_row_question(question, row_offset),
                options=options,
                kind=question.kind,
                index=row_index,
            )
        )
    prepare_request = getattr(scorer, "prepare_request", None)
    if callable(prepare_request):
        prepare_request(contexts)
    flat: list[dict[str, float]] = []
    for context, (_question, options) in zip(contexts, rows, strict=True):
        flat.append(probabilities_from_logprobs(scorer.score_options(context), options))
    answers: dict[str, Any] = {}
    for question, start, count, kind in index:
        if kind == "isolated_score":
            distribution = _combine_isolated([flat[start + offset][1] for offset in range(count)])
            answers[question.key] = _score_answer(question, distribution)
        else:
            answers[question.key] = _answer_for_question(question, flat[start])

    scorer_usage = getattr(scorer, "usage", None)
    usage_body = scorer_usage() if callable(scorer_usage) else usage
    usage_dict = dict(usage_body or {"input_tokens": 0, "output_tokens": 0})
    latency_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "model": model,
        "answers": answers,
        "usage": usage_dict,
        "receipt": {
            "latency_ms": latency_ms,
            "model": model,
            "question_count": len(req.questions),
            "remote": False,
            "cost": {"usd": 0.0, "source": "free-local", "cost_class": "free-local"},
            "usage": usage_dict,
            "engine": engine,
            "placement": placement,
            "residency": residency,
        },
    }


def probabilities_from_logprobs(logprobs: Mapping[Any, float], options: tuple[str, ...]) -> list[float]:
    if not options:
        raise ValueError("options must not be empty")
    values = []
    for index, option in enumerate(options):
        if index not in logprobs:
            raise DecisionReadoutError(f"missing logprob for option {option!r}")
        raw = logprobs[index]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or math.isnan(float(raw)):
            raise DecisionReadoutError(f"invalid logprob for option {option!r}")
        values.append(float(raw))
    peak = max(values)
    weights = [math.exp(value - peak) for value in values]
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        raise DecisionReadoutError("logprobs produced an invalid probability total")
    return [weight / total for weight in weights]


def _clip01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _choice_confidence(probabilities: list[float]) -> float:
    n = len(probabilities)
    if n <= 1:
        return 1.0
    pmax = max(probabilities)
    return _clip01((n * pmax - 1) / (n - 1))


def _score_confidence(probabilities: list[float]) -> float:
    n = len(probabilities)
    if n <= 1:
        return 1.0
    best = max(range(n), key=probabilities.__getitem__)
    spread = sum(value * abs(index - best) for index, value in enumerate(probabilities))
    uniform = sum(abs(index - (n - 1) / 2) for index in range(n)) / n
    return _clip01(1.0 - spread / uniform)


def _answer_for_question(question: DecisionQuestion, probabilities: list[float]) -> dict[str, Any]:
    best = max(range(len(probabilities)), key=probabilities.__getitem__)
    if question.kind == "noul":
        return {
            "type": "noul",
            "noul": probabilities[1],
        }
    if question.kind == "score":
        return _score_answer(question, probabilities)
    return {
        "type": "choice",
        "choice": question.names[best],
        "probabilities": {str(name): value for name, value in zip(question.names, probabilities)},
        "confidence": _choice_confidence(probabilities),
    }


def _score_answer(question: DecisionQuestion, probabilities: list[float]) -> dict[str, Any]:
    expected = sum(index * value for index, value in enumerate(probabilities))
    legend = question.legend or tuple(str(name) for name in question.names)
    return {
        "type": "score",
        "score": expected,
        "legend": {str(index): value for index, value in enumerate(legend)},
        "probabilities": {str(index): value for index, value in enumerate(probabilities)},
        "confidence": _score_confidence(probabilities),
    }


def error_body(
    message: str,
    *,
    code: str,
    param: str | None = None,
    kind: str = "invalid_request_error",
) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": kind, "code": code}
    if param:
        error["param"] = param
    return {"error": error}
