from __future__ import annotations

import math

import pytest

from gateway import decision_engine


class FakeScorer:
    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def score_options(self, ctx):
        self.seen.append(ctx)
        return self.rows[ctx.index]


def test_parse_typesafe_systemone_request() -> None:
    req = decision_engine.parse_request(
        {
            "model": "jev-latest",
            "state": {"message": "turn left or right?"},
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": "Pick a route.",
                    "criteria": {"left": "short", "right": "safe"},
                },
                "risk": {
                    "type": "score",
                    "instructions": "Score the risk.",
                    "criteria": ["low", "medium", "high"],
                },
                "allowed": {"type": "noul", "instructions": "Is it allowed?"},
            },
        }
    )

    assert req.model == "jev-latest"
    assert [q.key for q in req.questions] == ["route", "risk", "allowed"]
    assert req.questions[0].options == ("left: short", "right: safe")
    assert req.questions[1].options == ("0: low", "1: medium", "2: high")
    assert req.questions[2].options == ("no", "yes")


@pytest.mark.parametrize(
    "body,param",
    [
        ([], None),
        ({"questions": {}}, "questions"),
        ({"questions": {"q": []}}, "questions.q"),
        ({"questions": {"q": {"type": "maybe", "instructions": "x"}}}, "questions.q.type"),
        ({"questions": {"q": {"type": "choice", "instructions": "", "criteria": {"a": None, "b": None}}}}, "questions.q.instructions"),
        ({"questions": {"q": {"type": "choice", "instructions": "x", "criteria": {"a": None}}}}, "questions.q.criteria"),
        ({"questions": {"q": {"type": "score", "instructions": "x", "criteria": []}}}, "questions.q.criteria"),
    ],
)
def test_bad_request_reports_typesafe_422_details(body, param) -> None:
    with pytest.raises(decision_engine.DecisionInputError) as exc:
        decision_engine.parse_request(body)
    assert exc.value.param == param
    body = decision_engine.error_body(str(exc.value), code=exc.value.code, param=exc.value.param)
    assert body["error"]["type"] == "invalid_request_error"


def test_probability_readout_is_softmax() -> None:
    probs = decision_engine.probabilities_from_logprobs({0: 0.0, 1: math.log(3), 2: -30.0}, ("A", "B", "C"))

    assert probs[1] == pytest.approx(0.75, abs=1e-10)
    assert probs[0] == pytest.approx(0.25, abs=1e-10)
    assert probs[2] < 1e-12
    assert sum(probs) == pytest.approx(1.0)


def test_probability_readout_fails_closed_on_bad_logprobs() -> None:
    with pytest.raises(decision_engine.DecisionReadoutError):
        decision_engine.probabilities_from_logprobs({0: 0.0}, ("A", "B"))
    with pytest.raises(decision_engine.DecisionReadoutError):
        decision_engine.probabilities_from_logprobs({0: 0.0, 1: math.nan}, ("A", "B"))


def test_build_response_for_choice_score_and_noul() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {
                "route": {"type": "choice", "instructions": "Pick.", "criteria": {"left": None, "right": None}},
                "risk": {"type": "score", "instructions": "Risk.", "criteria": ["low", "medium", "high"]},
                "allowed": {"type": "noul", "instructions": "Allowed?"},
            },
        }
    )
    scorer = FakeScorer(
        [
            {0: -2, 1: 0},
            {0: -3, 1: -3},
            {0: -4, 1: 0},
            {0: -3, 1: -3},
            {0: -4, 1: 0},
        ]
    )

    out = decision_engine.build_systemone_response(req, scorer, model="decider-test", engine="cpu", start_time=0)

    assert out["model"] == "decider-test"
    assert out["answers"]["route"]["type"] == "choice"
    assert out["answers"]["route"]["choice"] == "right"
    assert out["answers"]["route"]["confidence"] == pytest.approx(0.7615941559)
    assert out["answers"]["risk"]["type"] == "score"
    assert out["answers"]["risk"]["score"] == pytest.approx(1.0)
    assert out["answers"]["risk"]["legend"] == {"0": "low", "1": "medium", "2": "high"}
    assert out["answers"]["risk"]["probabilities"] == {"0": pytest.approx(0.252268678711), "1": pytest.approx(0.495462642578), "2": pytest.approx(0.252268678711)}
    assert out["answers"]["allowed"]["type"] == "noul"
    assert out["answers"]["allowed"]["noul"] > 0.86
    assert "confidence" not in out["answers"]["allowed"]
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert out["receipt"]["cost"]["usd"] == 0.0
    assert out["receipt"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert out["receipt"]["engine"] == "cpu"
    assert out["receipt"]["placement"] == "cpu"
    assert out["receipt"]["residency"] == "on_demand"
    assert [ctx.index for ctx in scorer.seen] == [0, 1, 2, 3, 4]
    assert scorer.seen[1].options == ("no", "yes")
    assert "Proposed answer: medium" in scorer.seen[2].question


def test_rendered_option_text_collisions_do_not_collapse_probabilities() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {
                "collision": {
                    "type": "choice",
                    "instructions": "Pick.",
                    "criteria": {"a": "b", "a: b": None},
                }
            },
        }
    )
    scorer = FakeScorer([{0: -4, 1: 0}])

    out = decision_engine.build_systemone_response(req, scorer, model="decider-test")

    assert scorer.seen[0].options == ("a: b", "a: b")
    assert out["answers"]["collision"]["choice"] == "a: b"
    assert out["answers"]["collision"]["probabilities"]["a"] < 0.02
    assert out["answers"]["collision"]["probabilities"]["a: b"] > 0.98

class PreparingFakeScorer(FakeScorer):
    def __init__(self, rows):
        super().__init__(rows)
        self.prepared = []

    def prepare_request(self, contexts):
        self.prepared.append(tuple(contexts))


def test_prepare_request_receives_planned_contexts_without_changing_math() -> None:
    req = decision_engine.parse_request(
        {
            "state": "shared state",
            "questions": {
                "route": {"type": "choice", "instructions": "Pick.", "criteria": {"left": None, "right": None}},
                "allowed": {"type": "noul", "instructions": "Allowed?"},
            },
        }
    )
    scorer = PreparingFakeScorer([{0: -2, 1: 0}, {0: -4, 1: 0}])

    out = decision_engine.build_systemone_response(req, scorer, model="decider-test")

    assert out["answers"]["route"]["choice"] == "right"
    assert out["answers"]["allowed"]["noul"] > 0.86
    assert len(scorer.prepared) == 1
    assert [ctx.index for ctx in scorer.prepared[0]] == [0, 1]
    assert [ctx.index for ctx in scorer.seen] == [0, 1]
    assert scorer.prepared[0][0].state == "shared state"
    assert scorer.prepared[0][1].options == ("no", "yes")


def test_parse_accepts_criteria_backed_nullable_instructions_for_choice_score_and_noul() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {
                "choice_missing": {"type": "choice", "criteria": {"a": "Alpha", "b": "Beta"}},
                "choice_null": {"type": "choice", "instructions": None, "criteria": {"a": "Alpha", "b": "Beta"}},
                "score_null": {"type": "score", "instructions": None, "criteria": ["only level"]},
                "noul_null": {"type": "noul", "instructions": None, "criteria": {"true": "Spam", "false": None}},
            },
        }
    )

    assert [q.key for q in req.questions] == ["choice_missing", "choice_null", "score_null", "noul_null"]
    assert req.questions[0].options == ("a: Alpha", "b: Beta")
    assert req.questions[2].legend == ("only level",)
    assert req.questions[3].options == ("no", "yes: Spam")


def test_build_response_uses_generic_question_text_for_nullable_choice_and_score_instructions() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {
                "route": {"type": "choice", "instructions": None, "criteria": {"left": "Left", "right": "Right"}},
                "risk": {"type": "score", "criteria": ["only"]},
            },
        }
    )
    scorer = FakeScorer([{0: -2, 1: 0}, {0: -4, 1: 0}])

    out = decision_engine.build_systemone_response(req, scorer, model="decider-test")

    assert out["answers"]["route"]["choice"] == "right"
    assert out["answers"]["risk"]["score"] == pytest.approx(0.0)
    assert scorer.seen[0].question == "Which answer fits the context?"
    assert "Proposed answer: only" in scorer.seen[1].question


class GuardedFakeScorer(FakeScorer):
    def __init__(self):
        super().__init__([])
        self.prepare_calls = 0

    def prepare_request(self, contexts, *, deadline=None, cancel_check=None):
        self.prepare_calls += 1

    def score_options(self, ctx, *, deadline=None, cancel_check=None):
        raise AssertionError("score must not run after expired deadline")


def test_preexpired_deadline_stops_before_prepare_or_score() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {"q": {"type": "noul", "instructions": "Allowed?"}},
        }
    )
    scorer = GuardedFakeScorer()

    with pytest.raises(decision_engine.DecisionReadoutError, match="deadline"):
        decision_engine.build_systemone_response(req, scorer, deadline=0.0)

    assert scorer.prepare_calls == 0
    assert scorer.seen == []


def test_parse_accepts_nullable_choice_instructions_with_all_null_criteria() -> None:
    req = decision_engine.parse_request(
        {
            "state": "state",
            "questions": {
                "q": {"type": "choice", "instructions": None, "criteria": {"left": None, "right": None}},
            },
        }
    )

    assert req.questions[0].options == ("left", "right")


def test_parse_rejects_noul_without_instructions_or_descriptive_criteria() -> None:
    for question in (
        {"type": "noul"},
        {"type": "noul", "instructions": None},
        {"type": "noul", "instructions": None, "criteria": {"false": None, "true": None}},
    ):
        with pytest.raises(decision_engine.DecisionInputError, match="instructions"):
            decision_engine.parse_request({"questions": {"q": question}})


def test_parse_accepts_nullable_noul_instructions_with_one_descriptive_criterion() -> None:
    req = decision_engine.parse_request(
        {
            "questions": {
                "q": {"type": "noul", "instructions": None, "criteria": {"false": None, "true": "Applies"}},
            },
        }
    )

    assert req.questions[0].options == ("no", "yes: Applies")
