"""Offline boundary contracts: one lower-layer home, unchanged facade behavior."""

import ast
import inspect

import pytest

from gateway import decision_engine, decisions_api


def _body(kind, count):
    criteria = ({f"option_{i}": None for i in range(count)}
                if kind == "choice" else [f"level {i}" for i in range(count)])
    return {"model": "decide", "state": "synthetic",
            "questions": {"q": {"type": kind, "instructions": "Pick.",
                                 "criteria": criteria}}}


def test_facade_imports_bounds_from_lower_layer_without_redeclaring():
    tree = ast.parse(inspect.getsource(decisions_api))
    expected = {"MAX_CHOICE_OPTIONS": "MAX_OPTIONS",
                "MIN_SCORE_LEVELS": "MIN_LEVELS",
                "MAX_SCORE_LEVELS": "MAX_LEVELS"}
    imported = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "gateway.decision_engine"
        for alias in node.names
    }
    for alias, source in expected.items():
        assert imported.get(alias) == source
        assert getattr(decisions_api, alias) == getattr(decision_engine, source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        assert not any(isinstance(target, ast.Name) and target.id in expected
                       for target in targets)


def test_published_model_limits_are_the_engine_bounds():
    limits = decisions_api.DecisionModel(id="synthetic").view()["capabilities"]["limits"]
    assert limits["max_options"] == decision_engine.MAX_OPTIONS == 255
    assert limits["max_levels"] == decision_engine.MAX_LEVELS == 10
    assert decisions_api.MIN_SCORE_LEVELS == decision_engine.MIN_LEVELS == 1


@pytest.mark.parametrize("kind,count,api_message,engine_message", [
    ("choice", 256, "choice criteria must contain 2 to 255 options.",
     "questions.q.criteria must be an object containing 2 to 255 options."),
    ("score", 11, "score criteria must contain 1 to 10 levels.",
     "questions.q.criteria must list 1 to 10 score levels."),
    ("score", 0, "score criteria must contain 1 to 10 levels.",
     "questions.q.criteria must list 1 to 10 score levels."),
])
def test_both_paths_reject_out_of_bounds_with_current_messages(
        kind, count, api_message, engine_message):
    body = _body(kind, count)
    with pytest.raises(decisions_api.DecisionsError) as facade:
        decisions_api.validate_systemone_request(body)
    assert str(facade.value) == api_message
    assert facade.value.status == 422
    assert facade.value.code == "invalid_criteria"
    assert facade.value.param == "questions.q.criteria"
    with pytest.raises(decision_engine.DecisionInputError) as engine:
        decision_engine.parse_request(body)
    assert str(engine.value) == engine_message
    assert engine.value.param == "questions.q.criteria"


@pytest.mark.parametrize("kind,count", [("choice", 255), ("score", 10), ("score", 1)])
def test_both_paths_accept_inclusive_bounds(kind, count):
    body = _body(kind, count)
    validated = decisions_api.validate_systemone_request(body)
    assert len(validated["questions"]["q"]["criteria"]) == count
    parsed = decision_engine.parse_request(body)
    assert len(parsed.questions) == 1
    assert len(parsed.questions[0].options) == count
    assert decision_engine.parse_request(validated) == parsed
