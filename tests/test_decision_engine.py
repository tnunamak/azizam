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
        ({"questions": {"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}}, "questions.q.criteria"),
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
