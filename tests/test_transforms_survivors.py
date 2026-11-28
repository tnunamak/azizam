"""Kill tests for transforms survivors that mut3 called equivalent or unproductive (lane MUT r4).

Each docstring names the mutant it kills (ID prefix gateway/transforms.py). Inputs and expected
values were run on clean code first; the mutant must change one of them.
"""

from __future__ import annotations

import json

import pytest

from gateway import transforms as t
from gateway.transforms import (
    ClampTransform, StreamingThinkSplitTransform as Splitter, TransformContext, TransformRequestError,
)

CTX = TransformContext()


def _split(text, *, final=True, state=None):
    return Splitter()._split_text(state if state is not None else ["", False], text, final=final)


def _frame(*choices, **top):
    return ("data: " + json.dumps({"id": "c", "choices": list(choices), **top}) + "\n\n").encode()


def _payloads(raw: bytes):
    return [json.loads(line[5:]) for line in raw.decode().splitlines()
            if line.startswith("data:") and "[DONE]" not in line]


# --- clamp keeps the caller's number type -------------------------------------------------------

def test_a_value_equal_to_the_lower_bound_is_not_rewritten_as_the_bound():
    """:439 ``value < min`` -> ``<=``: the int 0 would become the float 0.0, a different JSON number."""
    body = ClampTransform({"param": "x", "min": 0.0}).on_request({"x": 0}, CTX)
    assert body == {"x": 0} and type(body["x"]) is int
    assert json.dumps(body) == '{"x": 0}'


def test_a_value_equal_to_the_upper_bound_is_not_rewritten_as_the_bound():
    """:441 ``value > max`` -> ``>=``."""
    body = ClampTransform({"param": "x", "max": 1.0}).on_request({"x": 1}, CTX)
    assert json.dumps(body) == '{"x": 1}'


def test_a_request_error_built_with_a_keyword_message_still_has_that_message():
    """:53 ``super().__init__(message)`` removed: BaseException keeps only positional args."""
    error = TransformRequestError(message="oops", param="p")
    assert str(error) == "oops" and error.args == ("oops",)


# --- a close tag before an opener (stray close) -------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("</think>x", ("x", "")),
    ("r</think>x", ("x", "r")),
    ("a</think>b</think>c", ("c", "ab")),
    ("<think>a</think><think>b</think>", ("", "ab")),
])
def test_text_before_a_close_tag_with_no_opener_is_reasoning_and_the_rest_is_read_on(text, expected):
    """:888 ``idx_close >= 0`` (``>`` and ``1``), :891 ``continue`` -> ``break``, :902 second join separator."""
    assert _split(text) == expected


# --- the frame rewriter -------------------------------------------------------------------------

def test_a_payload_without_a_choices_list_is_reported_unchanged():
    """:908 ``return False`` -> True / None."""
    transform = Splitter()
    assert transform._rewrite_payload({}) is False
    assert transform._rewrite_payload({"choices": "x"}) is False
    raw = b'data: { "choices": null }\n\n'
    assert Splitter().process_chunk(raw) == raw


def _delta(content):
    return {"delta": {"content": content}}


def test_a_choice_that_is_not_an_object_does_not_stop_the_choices_after_it():
    """:911 ``continue`` -> ``break``."""
    payload = {"choices": [None, _delta("<think>a</think>b")]}
    assert Splitter()._rewrite_payload(payload) is True
    assert payload["choices"][1]["delta"] == {"content": "b", "reasoning_content": "a"}


def test_a_choice_without_a_delta_object_does_not_stop_the_choices_after_it():
    """:914 ``continue`` -> ``break``."""
    payload = {"choices": [{}, {"delta": None}, _delta("<think>a</think>b")]}
    assert Splitter()._rewrite_payload(payload) is True
    assert payload["choices"][2]["delta"] == {"content": "b", "reasoning_content": "a"}


def test_a_choice_without_an_index_continues_choice_zero():
    """:916 default index 0 -> 1: a tag cut across an index-less frame and an index-0 frame is not rejoined."""
    transform = Splitter()
    assert _payloads(transform.process_chunk(_frame(_delta("<thi")))) [0]["choices"][0]["delta"]["content"] == ""
    out = _payloads(transform.process_chunk(_frame({"index": 0, **_delta("nk>x</think>y")})))[0]
    assert out["choices"][0]["delta"] == {"content": "y", "reasoning_content": "x"}


def test_a_choice_with_no_text_does_not_stop_the_choices_after_it():
    """:919 ``continue`` -> ``break``."""
    payload = {"choices": [{"index": 0, "delta": {}}, {"index": 1, **_delta("<think>a</think>b")}]}
    assert Splitter()._rewrite_payload(payload) is True
    assert payload["choices"][1]["delta"] == {"content": "b", "reasoning_content": "a"}


def test_closing_the_stream_releases_the_held_text_of_every_choice_that_has_some():
    """:936 ``continue`` -> ``break``: choice 0 has nothing held, choice 1 holds a cut tag."""
    transform = Splitter()
    transform.process_chunk(_frame({"index": 0, **_delta("a")}, {"index": 1, **_delta("<thi")}))
    (frame,) = _payloads(transform.finish())
    assert frame["choices"] == [{"index": 1, "delta": {"content": "<thi"}, "finish_reason": None}]


# --- reading the byte stream --------------------------------------------------------------------

def test_an_unfinished_line_of_exactly_the_carry_limit_is_still_carried():
    """:961 ``len(carry) > limit`` -> ``>=``: the limit itself is allowed, one byte more is passed on."""
    transform = Splitter()
    exactly = b"data:" + b"x" * (t._MAX_CARRY_BYTES - 5)
    assert len(exactly) == t._MAX_CARRY_BYTES
    assert transform.process_chunk(exactly) == b""
    over = Splitter()
    assert over.process_chunk(exactly + b"x") == exactly + b"x"


def test_a_data_line_whose_text_contains_the_data_prefix_is_read_whole():
    """:978 ``split("data:", 1)`` -> ``2``: the mutant unpacks three parts into two names and raises."""
    raw = b'data: {"choices":[{"delta":{"content":"data:"}}]}\n\n'
    assert Splitter().process_chunk(raw) == raw
