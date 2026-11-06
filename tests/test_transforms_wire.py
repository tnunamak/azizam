"""Pins wire-visible behavior of gateway/transforms.py that the mutation sweep found unchecked."""

from __future__ import annotations

import json

import pytest

from gateway import transforms as t
from gateway.transforms import (
    ClampTransform, MessageInjectTransform, ParamMapTransform, ReasoningEffortTransform,
    StreamingThinkSplitTransform, TransformContext, TransformRequestError,
    apply_thinking_suppression, normalize_chat_completion_payload, split_reasoning_visible_text,
)

CTX = TransformContext()


def test_a_path_skips_wildcards_and_empty_segments_and_a_request_error_defaults_to_invalid_value():
    assert t._get_path({"a": {"b": 1}}, "a[*].b") == 1
    assert t._get_path({"a": {"b": 1}}, "a..b") == 1
    assert t._get_path({"a": 1}, "") == {"a": 1}
    assert t._get_path({"a": 1}, "a.b") is t._MISSING
    error = TransformRequestError("bad", param="p")
    assert (str(error), error.param, error.code) == ("bad", "p", "invalid_value")
    assert repr(TransformContext({"k": 1})) == "TransformContext({'k': 1})"


def test_a_renamed_param_is_removed_from_its_source_even_when_the_destination_is_kept():
    keep = ParamMapTransform({"kind": "param-map", "from": "a", "to": "b", "only_if_destination_absent": True})
    assert keep.on_request({"a": 1, "b": 2}, CTX) == {"b": 2}
    default = ParamMapTransform({"kind": "param-map", "from": "a", "to": "b"})
    assert default.on_request({"a": 1, "b": 2}, CTX) == {"b": 1}


def test_message_inject_defaults_are_a_system_message_with_empty_text_prepended():
    body = MessageInjectTransform({"kind": "message-inject"}).on_request({"messages": [{"role": "user", "content": "hi"}]}, CTX)
    assert body["messages"] == [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]


def test_message_inject_append_or_insert_joins_with_a_newline_or_fills_an_empty_content():
    cfg = {"kind": "message-inject", "role": "system", "text": "T", "mode": "append-or-insert"}
    joined = MessageInjectTransform(cfg).on_request({"messages": [{"role": "system", "content": "S"}]}, CTX)
    assert joined["messages"] == [{"role": "system", "content": "S\nT"}]
    empty = MessageInjectTransform(cfg).on_request({"messages": [{"role": "system"}]}, CTX)
    assert empty["messages"] == [{"role": "system", "content": "T"}]
    assert MessageInjectTransform(cfg).on_request({"x": 1}, CTX) == {"x": 1}  # no messages list: untouched
    with pytest.raises(ValueError, match=r"\AUnknown\ message\-inject\ mode:\ 'sideways'\Z"):
        MessageInjectTransform({"kind": "message-inject", "mode": "sideways"}).on_request({"messages": []}, CTX)


@pytest.mark.parametrize("value,expected", [(1, 2), (2, 2), (5, 5), (8, 8), (9, 8)])
def test_clamp_leaves_a_value_on_its_bound_alone_and_moves_the_rest_in(value, expected):
    assert ClampTransform({"kind": "clamp", "param": "x", "min": 2, "max": 8}).on_request({"x": value}, CTX) == {"x": expected}


def test_the_pipelines_use_the_given_context_and_registry_and_inline_specs():
    registry = t.TransformRegistry()
    registry.register("drop-x", {"kind": "drop-params", "drop": ["x"]})
    assert t.run_request_pipeline(["drop-x", {"kind": "default-params", "set": {"y": 1}}], {"x": 0}, registry=registry) == {"y": 1}
    assert t.run_response_pipeline([{"kind": "drop-params", "phase": "response", "drop": ["z"]}], {"z": 1, "k": 2}) == {"k": 2}
    assert registry.names() == ["drop-x"]
    assert t.REGISTRY.resolve  # the module registry exists for the default argument


def test_thinking_suppression_sets_enable_thinking_false_without_overriding_the_caller():
    body: dict = {}
    apply_thinking_suppression(body)
    assert body == {"chat_template_kwargs": {"enable_thinking": False}}
    chosen = {"chat_template_kwargs": {"enable_thinking": True}}
    apply_thinking_suppression(chosen)
    assert chosen == {"chat_template_kwargs": {"enable_thinking": True}}
    assert t.QWEN3_NO_THINK_HINT == "/no_think" and t.QWEN3_ENABLE_THINKING_KEY == "enable_thinking"


@pytest.mark.parametrize("effort,budget", [("none", 0), ("minimal", 2048), ("low", 4096), ("medium", 6144), ("high", 8192), ("xhigh", -1)])
def test_the_default_effort_budgets(effort, budget):
    body = {"reasoning_effort": effort}
    assert ReasoningEffortTransform().apply(body) is None
    expected = {"thinking_budget_tokens": budget}
    if budget == 0:
        expected["chat_template_kwargs"] = {"enable_thinking": False}
    assert body == expected


def test_effort_validation_errors_are_worded_exactly():
    transform = ReasoningEffortTransform({"effort_budgets": {"low": True, "medium": "soon", "high": 7}})
    assert transform.apply({"reasoning_effort": "turbo"}) == (
        "Invalid value for 'reasoning_effort': 'turbo'. "
        "Expected one of: ['high', 'low', 'medium', 'minimal', 'none', 'xhigh'].")
    assert transform.apply({"reasoning_effort": 3}).startswith("Invalid value for 'reasoning_effort': 3.")
    assert transform.apply({"reasoning_effort": "low"}) == "No reasoning budget configured for effort 'low'."
    assert transform.apply({"reasoning_effort": "none"}) == "No reasoning budget configured for effort 'none'."
    assert transform.apply({"reasoning_effort": "medium"}) == "Invalid reasoning budget configured for effort 'medium'."
    body = {"reasoning_effort": "high"}
    assert transform.apply(body) is None and body == {"thinking_budget_tokens": 7}
    nothing = {"reasoning_effort": None}
    assert ReasoningEffortTransform().apply(nothing) is None and nothing == {}


def test_a_concrete_budget_is_kept_and_a_bad_one_is_reported():
    transform = ReasoningEffortTransform({"default_effort": "high"})
    for good in (-1, 0, 100):
        body = {"thinking_budget_tokens": good}
        assert transform.apply(body) is None and body == {"thinking_budget_tokens": good}
    for bad in (-2, True, "5", 1.5):
        assert transform.apply({"thinking_budget_tokens": bad}) == (
            f"Invalid value for 'thinking_budget_tokens': {bad!r}. Expected an integer >= -1.")
    plain = {}
    assert transform.apply(plain) is None and plain == {"thinking_budget_tokens": 8192}
    assert ReasoningEffortTransform().apply({}) is None
    assert ReasoningEffortTransform({"default_effort": 5}).apply({}) is None


def test_the_final_chat_reasoning_step_runs_only_on_chat_completions_and_raises_a_request_error():
    step = t.FinalChatReasoningEffortTransform()
    other = {"reasoning_effort": "low"}
    assert step.on_request(other, TransformContext({"request_path": "/v1/embeddings"})) is other and other == {"reasoning_effort": "low"}
    chat = {"reasoning_effort": "low"}
    step.on_request(chat, CTX)  # no request_path: chat is the default
    assert chat == {"thinking_budget_tokens": 4096}
    with pytest.raises(TransformRequestError) as raised:
        step.on_request({"reasoning_effort": "bogus"}, CTX)
    assert raised.value.param == "reasoning_effort" and raised.value.code == "invalid_value"
    from_ctx = {"reasoning_effort": "low"}
    step.on_request(from_ctx, TransformContext({"reasoning": {"effort_budgets": {"low": 11}}}))
    assert from_ctx == {"thinking_budget_tokens": 11}
    assert step.request_phase == "final" and t.ReasoningEffortTransform.on_response is not None


def test_a_reasoning_effort_response_passes_through_untouched():
    payload = {"a": 1}
    assert ReasoningEffortTransform().on_response(payload, CTX) is payload
    assert ReasoningEffortTransform().on_request({"reasoning_effort": "low"}, CTX) == {"thinking_budget_tokens": 4096}


# --- reasoning split -----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("<think>why</think>answer", ("why", "answer")),
    ("why</THINK> answer ", ("why", "answer")),
    ("<think>why</think>", ("why", None)),
    ("</think>answer", (None, "answer")),
    ("no tags", (None, "no tags")),
    ("", (None, None)),
    (None, (None, None)),
])
def test_splitting_reasoning_from_visible_text(text, expected):
    assert split_reasoning_visible_text(text) == expected


def _chat(message=None, delta=None, finish=None, index=0):
    choice = {"index": index}
    if message is not None:
        choice["message"] = message
    if delta is not None:
        choice["delta"] = delta
    if finish is not None:
        choice["finish_reason"] = finish
    return {"choices": [choice]}


def test_a_leaked_close_tag_in_a_message_is_promoted_to_reasoning_content():
    payload = _chat(message={"content": "<think>why</think>answer"})
    assert normalize_chat_completion_payload(payload) is True
    assert payload["choices"][0]["message"] == {"content": "answer", "reasoning_content": "why"}
    keeps = _chat(message={"content": "x</think>answer", "reasoning_content": "mine"})
    assert normalize_chat_completion_payload(keeps) is True
    assert keeps["choices"][0]["message"] == {"content": "answer", "reasoning_content": "mine"}
    only_reasoning_leak = _chat(message={"content": "</think>"})
    assert normalize_chat_completion_payload(only_reasoning_leak) is False


def test_reasoning_content_that_carries_the_visible_suffix_is_split_and_private_reasoning_stays_private():
    payload = _chat(message={"content": "", "reasoning_content": "<think>why</think>answer"})
    assert normalize_chat_completion_payload(payload) is True
    assert payload["choices"][0]["message"] == {"content": "answer", "reasoning_content": "why"}
    private = _chat(message={"reasoning_content": "just thinking"})
    assert normalize_chat_completion_payload(private) is False
    assert private["choices"][0]["message"] == {"reasoning_content": "just thinking"}
    visible = _chat(message={"content": "shown", "reasoning_content": "<think>x</think>y"})
    assert normalize_chat_completion_payload(visible) is False


def test_a_delta_with_a_leaked_close_tag_keeps_only_the_visible_text():
    payload = _chat(delta={"content": "why</think>answer"})
    assert normalize_chat_completion_payload(payload) is True
    assert payload["choices"][0]["delta"] == {"content": "answer"}
    assert normalize_chat_completion_payload(_chat(delta={"reasoning_content": "r"})) is False
    for odd in ({}, {"choices": "x"}, {"choices": ["x", {"delta": 3, "message": 4}]}):
        assert normalize_chat_completion_payload(odd) is False


# --- streaming think split ------------------------------------------------------------------

def _frame(content=None, finish=None, index=0, **extra):
    delta = {} if content is None else {"content": content}
    choice = {"index": index, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    return ("data: " + json.dumps({"id": "c1", "object": "chunk", "created": 1, "model": "m", "choices": [choice], **extra})
            + "\n\n").encode()


def _decoded(raw: bytes) -> list:
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data:") and line.strip() != "data: [DONE]":
            out.append(json.loads(line[5:]))
    return out


def _stream(*chunks: bytes) -> tuple[str, str]:
    transform = StreamingThinkSplitTransform()
    visible, reasoning = "", ""
    for chunk in (*chunks, b""):
        for payload in _decoded(transform.process_chunk(chunk) if chunk else transform.finish()):
            for choice in payload["choices"]:
                visible += choice["delta"].get("content") or ""
                reasoning += choice["delta"].get("reasoning_content") or ""
    return visible, reasoning


def test_a_tag_cut_between_frames_is_repaired_and_routed():
    assert _stream(_frame("<think>pl"), _frame("an</thi"), _frame("nk>answer")) == ("answer", "plan")
    assert _stream(_frame("a<th"), _frame("ink>b</think>c")) == ("ac", "b")
    assert _stream(_frame("A</THINK>B")) == ("B", "A")      # a close without an opener: earlier text was reasoning
    assert _stream(_frame("x<think>y")) == ("x", "y")


def test_a_held_partial_tag_is_released_on_the_finish_frame_and_at_done_and_at_close():
    visible, reasoning = _stream(_frame("abc</thi"), _frame("", finish="stop"))
    assert (visible, reasoning) == ("abc</thi", "")
    transform = StreamingThinkSplitTransform()
    first = _decoded(transform.process_chunk(_frame("abc</thi")))
    assert first[0]["choices"][0]["delta"]["content"] == "abc"
    done = transform.process_chunk(b"data: [DONE]\n\n").decode()
    released = _decoded(done.encode())
    assert released == [{"id": "c1", "object": "chunk", "created": 1, "model": "m",
                         "choices": [{"index": 0, "delta": {"content": "</thi"}, "finish_reason": None}]}]
    assert done.endswith("data: [DONE]\n\n")
    closing = StreamingThinkSplitTransform()
    closing.process_chunk(_frame("abc</thi"))
    flushed = closing.finish().decode()
    assert flushed.startswith("data: {") and flushed.endswith("\n\n") and '"content":"</thi"' in flushed
    assert StreamingThinkSplitTransform().finish() == b""
    clean = StreamingThinkSplitTransform()
    clean.process_chunk(_frame("abc"))
    assert clean.finish() == b""


def test_each_choice_is_tracked_separately_and_reasoning_deltas_are_appended():
    transform = StreamingThinkSplitTransform()
    payload = {"id": "c", "choices": [
        {"index": 0, "delta": {"content": "<think>a</think>x", "reasoning_content": "pre-"}},
        {"index": 1, "delta": {"content": "<think>b"}}]}
    out = _decoded(transform.process_chunk(b"data: " + json.dumps(payload).encode() + b"\n\n"))[0]
    assert out["choices"][0]["delta"] == {"content": "x", "reasoning_content": "pre-a"}
    assert out["choices"][1]["delta"] == {"content": "", "reasoning_content": "b"}


def test_a_choice_without_an_index_is_choice_zero():
    transform = StreamingThinkSplitTransform()
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\n\n'
    assert _decoded(transform.process_chunk(raw))[0]["choices"][0]["delta"] == {"content": "b", "reasoning_content": "a"}


def test_lines_that_are_not_data_frames_pass_through_byte_for_byte():
    transform = StreamingThinkSplitTransform()
    for raw in (b": keepalive\n\n", b"event: ping\ndata: not json\n\n", b'data: [1,2]\n\n', b"data:\n\n", b'data: {"choices":[{"delta":{"content":"plain"}}]}\r\n\r\n'):
        assert transform.process_chunk(raw) == raw
    assert transform.process_chunk(b"\xff\xfe\n") == b"\xff\xfe\n"


def test_a_data_line_cut_between_reads_is_carried_and_the_line_ending_style_is_kept():
    transform = StreamingThinkSplitTransform()
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\r\n'
    assert transform.process_chunk(raw[:20]) == b""
    out = transform.process_chunk(raw[20:])
    assert out == b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\r\n'
    assert transform.process_chunk(b"data:") == b""   # a bare prefix is carried too
    assert transform.process_chunk(b' {"choices":[]}\n') == b'data: {"choices":[]}\n'


def test_a_huge_unfinished_line_is_passed_on_instead_of_carried_forever():
    transform = StreamingThinkSplitTransform()
    blob = b"data: " + b"x" * (t._MAX_CARRY_BYTES + 10)
    assert transform.process_chunk(blob) == blob
    assert transform.process_chunk(b"data: ok\n") == b"data: ok\n"


def test_the_partial_tag_length_is_the_longest_proper_prefix_suffix():
    f = StreamingThinkSplitTransform._partial_tag_len
    assert [f(x) for x in ("", "a", "<", "<thi", "<think", "<think>", "</thin", "</think", "x</", "</THI")] == [0, 0, 1, 4, 6, 0, 6, 7, 2, 5]


def test_the_on_response_shims_leave_payloads_alone():
    payload = {"a": 1}
    assert StreamingThinkSplitTransform().on_response(payload, CTX) is payload
    assert StreamingThinkSplitTransform().on_request(payload, CTX) is payload
    assert t.ReasoningFieldNormalizeTransform().on_request(payload, CTX) is payload
    leaked = _chat(message={"content": "w</think>v"})
    assert t.ReasoningFieldNormalizeTransform().on_response(leaked, CTX)["choices"][0]["message"]["content"] == "v"


# --- round 2: exact frames --------------------------------------------------------------------

def test_a_nested_delete_through_a_missing_or_non_mapping_parent_is_a_no_op():
    body = {"x": 1, "a": 5}
    t._del_nested(body, "a.b")
    t._del_nested(body, "missing.b")
    t._del_nested(body, "x")
    assert body == {"a": 5}
    nested = {"a": {"b": 1, "c": 2}}
    t._del_nested(nested, "a.b")
    assert nested == {"a": {"c": 2}}


def test_a_primitive_of_the_other_phase_leaves_a_response_untouched():
    request_only = t.DropParamsTransform({"kind": "drop-params", "drop": ["z"]})
    payload = {"z": 1}
    assert request_only.on_response(payload, CTX) is payload and payload == {"z": 1}
    guarded = t.DropParamsTransform({"kind": "drop-params", "phase": "response", "drop": ["z"],
                                     "when": [{"field": "k", "op": "present"}]})
    assert guarded.on_response({"z": 1}, CTX) == {"z": 1}
    assert guarded.on_response({"z": 1, "k": 0}, CTX) == {"k": 0}


def test_message_inject_adds_only_the_messages_key():
    body = MessageInjectTransform({"kind": "message-inject", "text": "T"}).on_request({"messages": []}, CTX)
    assert body == {"messages": [{"role": "system", "content": "T"}]}
    appended = MessageInjectTransform({"kind": "message-inject", "role": "user", "text": "T", "mode": "append"}).on_request(
        {"messages": [{"role": "system", "content": "S"}], "model": "m"}, CTX)
    assert appended == {"model": "m", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]}


class _Recorder:
    def __init__(self):
        self.seen = []

    def on_request(self, body, ctx):
        self.seen.append(ctx)
        return body

    on_response = on_request


def test_the_pipelines_hand_the_given_context_to_every_step_and_make_one_when_none_is_given():
    registry = t.TransformRegistry()
    recorder = _Recorder()
    registry.register_builtin("rec", recorder)
    mine = TransformContext({"k": 1})
    t.run_request_pipeline(["rec"], {}, mine, registry)
    t.run_response_pipeline(["rec"], {}, mine, registry)
    assert recorder.seen == [mine, mine] and all(c is mine for c in recorder.seen)
    recorder.seen.clear()
    t.run_request_pipeline(["rec"], {}, registry=registry)
    t.run_response_pipeline(["rec"], {}, registry=registry)
    assert len(recorder.seen) == 2 and all(isinstance(c, TransformContext) and c.get("k") is None for c in recorder.seen)


def test_a_final_phase_step_runs_after_the_ordinary_ones_in_attachment_order():
    order = []

    class Step:
        def __init__(self, name, phase):
            self.name, self.request_phase = name, phase

        def on_request(self, body, ctx):
            order.append(self.name)
            return body

    registry = t.TransformRegistry()
    for name, phase in (("a", "final"), ("b", "request"), ("c", "final"), ("d", "request")):
        registry.register_builtin(name, Step(name, phase))
    t.run_request_pipeline(["a", "b", "c", "d"], {}, registry=registry)
    assert order == ["b", "d", "a", "c"]


def test_the_carry_limit_is_one_mebibyte():
    assert t._MAX_CARRY_BYTES == 1048576 and t._THINK_OPEN_TAG == "<think>" and t._THINK_CLOSE_TAG == "</think>"


def test_an_opening_tag_inside_reasoning_is_released_at_once_not_held_back():
    transform = StreamingThinkSplitTransform()
    out = _decoded(transform.process_chunk(_frame("<think>abc<think>")))
    assert out[0]["choices"][0]["delta"] == {"content": "", "reasoning_content": "abc<think>"}


def test_the_flush_frame_is_exactly_the_template_ids_plus_one_choice_per_held_index():
    transform = StreamingThinkSplitTransform()
    first = (b'data: {"id":"c1","object":"chunk","created":1,"model":"m","extra":9,"choices":['
             b'{"index":2,"delta":{"content":"b</thi"}},{"index":0,"delta":{"content":"<think>a</th"}}]}\n\n')
    transform.process_chunk(first)
    assert transform.finish() == (
        b'data: {"id":"c1","object":"chunk","created":1,"model":"m","choices":['
        b'{"index":0,"delta":{"content":"","reasoning_content":"</th"},"finish_reason":null},'
        b'{"index":2,"delta":{"content":"</thi"},"finish_reason":null}]}\n\n')
    assert transform.finish() == b""


def test_a_finish_frame_without_content_releases_the_held_text_in_place():
    transform = StreamingThinkSplitTransform()
    transform.process_chunk(_frame("abc</thi"))
    out = _decoded(transform.process_chunk(_frame(None, finish="stop")))
    assert out[0]["choices"][0] == {"index": 0, "delta": {"content": "</thi"}, "finish_reason": "stop"}
    assert transform.finish() == b""


def test_a_frame_with_no_content_and_nothing_held_is_left_alone():
    transform = StreamingThinkSplitTransform()
    raw = _frame(None)
    assert transform.process_chunk(raw) == raw
    again = _frame(None, finish="stop")
    assert transform.process_chunk(again) == again


def test_a_done_marker_with_nothing_held_adds_no_frame():
    transform = StreamingThinkSplitTransform()
    transform.process_chunk(_frame("plain"))
    assert transform.process_chunk(b"data: [DONE]\n\n") == b"data: [DONE]\n\n"


def test_crlf_and_lf_line_endings_and_blank_lines_survive_a_rewrite():
    transform = StreamingThinkSplitTransform()
    crlf = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\r\n\r\n'
    assert transform.process_chunk(crlf) == b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\r\n\r\n'
    lf = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\n\n'
    assert transform.process_chunk(lf) == b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\n\n'
    assert transform.process_chunk(b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}') == b""


def test_text_before_a_close_tag_with_no_opener_is_reasoning_in_the_stream():
    assert _stream(_frame("A</think>B")) == ("B", "A")


# --- a close tag before any opener ---------------------------------------------------------------

def _split(text, *, final=True, state=None):
    return StreamingThinkSplitTransform()._split_text(state if state is not None else ["", False], text, final=final)


@pytest.mark.parametrize("text, expected", [
    ("r</think><think>x</think>v", ("v", "rx")),          # a stray close, then a whole block
    ("r</think>v<think>t</think>w", ("vw", "rt")),
    ("</think><think>x</think>v", ("v", "x")),
    ("r</think>x", ("x", "r")),
    ("</think>x", ("x", "")),
    ("a<think>b</think>c</think>d", ("ad", "bc")),        # a stray close after a whole block: "c" was reasoning
    ("<think>a</think><think>b</think>", ("", "ab")),
])
def test_a_close_tag_before_any_opener_never_leaks_into_the_visible_text(text, expected):
    assert _split(text) == expected


def test_a_stray_close_cut_between_frames_is_still_routed():
    # "r" went out before the cut tag could be known to be a close tag; the held tag text is not leaked.
    assert _stream(_frame("r</thi"), _frame("nk><think>x</think>v")) == ("rv", "x")
    assert _stream(_frame("</thi"), _frame("nk><think>x</think>v")) == ("v", "x")


def test_lone_cr_sse_lines_are_rewritten_with_their_terminators():
    transform = StreamingThinkSplitTransform()
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\r\r: keepalive\r'
    expected = b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\r\r: keepalive'
    assert transform.process_chunk(raw) == expected
    assert transform.finish() == b"\r"


@pytest.mark.parametrize("prefix", [b": keepalive", b"event: ping", b"\xff"])
def test_a_crlf_split_between_reads_holds_the_cr_until_the_lf_arrives(prefix):
    transform = StreamingThinkSplitTransform()
    assert transform.process_chunk(prefix + b"\r") == prefix
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}'
    assert transform.process_chunk(b"\n" + raw + b"\r") == b"\r\n"
    expected = b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\r\n\r\n'
    assert transform.process_chunk(b"\n\r\n") == expected
    assert transform.finish() == b""


def test_a_raw_unicode_line_separator_in_json_is_not_an_sse_line_end():
    transform = StreamingThinkSplitTransform()
    raw = 'data: {"choices":[{"delta":{"content":"<think>a\u2028b</think>c"}}]}\n\n'.encode()
    expected = b'data: {"choices":[{"delta":{"content":"c","reasoning_content":"a\\u2028b"}}]}\n\n'
    assert transform.process_chunk(raw) == expected
    assert transform.finish() == b""


def test_mixed_sse_terminators_in_one_chunk_are_preserved_on_rewrite():
    transform = StreamingThinkSplitTransform()
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}'
    rewritten = b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}'
    chunk = b": first\r" + raw + b"\r" + raw + b"\n" + raw + b"\r\n\r\n"
    expected = b": first\r" + rewritten + b"\r" + rewritten + b"\n" + rewritten + b"\r\n\r\n"
    assert transform.process_chunk(chunk) == expected
    assert transform.finish() == b""


def test_a_final_lone_cr_is_flushed_as_a_rewritten_data_line_terminator(monkeypatch):
    transform = StreamingThinkSplitTransform()
    raw = b'data: {"choices":[{"delta":{"content":"<think>a</think>b"}}]}\r'
    assert transform.process_chunk(raw) == b""
    expected = b'data: {"choices":[{"delta":{"content":"b","reasoning_content":"a"}}]}\r'
    assert transform.finish() == expected
    assert transform.finish() == b""

    # Overflow still passes the line through instead of carrying it, while
    # the one ambiguous delimiter byte stays within the carry budget.
    monkeypatch.setattr(t, "_MAX_CARRY_BYTES", 8)
    oversized = StreamingThinkSplitTransform()
    assert oversized.process_chunk(raw) == expected[:-1]
    assert oversized._raw_carry == b"\r"
    assert oversized.finish() == b"\r"


def test_the_sse_line_splitter_returns_each_terminated_line_and_no_empty_tail():
    assert t._split_sse_lines("") == []
    assert t._split_sse_lines("a\nb\r\nc\r") == [("a", "\n"), ("b", "\r\n"), ("c", "\r")]
    assert t._split_sse_lines("a\n\n") == [("a", "\n"), ("", "\n")]
    assert t._split_sse_lines("a\nb") == [("a", "\n"), ("b", "")]
    assert t._split_sse_lines("a\r", final=False) == [("a\r", "")]
    assert t._split_sse_lines("a\r\n", final=False) == [("a", "\r\n")]


@pytest.mark.parametrize("chunk", [b": data: x\n", b": data: x\r\n", b"xdata: x\r", b""])
def test_a_complete_line_that_only_contains_data_is_passed_on_at_once(chunk):
    transform = StreamingThinkSplitTransform()
    expected = chunk[:-1] if chunk.endswith(b"\r") else chunk
    assert transform.process_chunk(chunk) == expected
    assert transform._raw_carry == (b"\r" if chunk.endswith(b"\r") else b"")

# --- invalid primitive configuration -------------------------------------------------------

_INVALID_PRIMITIVE_CONFIGS = [
    pytest.param(t.ValueMapTransform, {}, "value-map config 'param' is required", id="value-map-missing-param"),
    pytest.param(t.ValueMapTransform, {"param": []}, "value-map config 'param' must be a string", id="value-map-param-type"),
    pytest.param(t.ValueMapTransform, {"param": "x", "map": ["x"]}, "value-map config 'map' must be a mapping or null", id="value-map-map-type"),
    pytest.param(t.ValueMapTransform, {"param": "x", "map": ""}, "value-map config 'map' must be a mapping or null", id="value-map-falsey-map"),
    pytest.param(t.DropParamsTransform, {"drop": "xy"}, "drop-params config 'drop' must be a list of strings or null", id="drop-params-string"),
    pytest.param(t.DropParamsTransform, {"drop": [1]}, "drop-params config 'drop' must be a list of strings or null", id="drop-params-entry"),
    pytest.param(t.DropParamsTransform, {"drop": {}}, "drop-params config 'drop' must be a list of strings or null", id="drop-params-falsey-map"),
    pytest.param(t.DefaultParamsTransform, {"set": ["x"]}, "default-params config 'set' must be a mapping or null", id="default-params-set-type"),
    pytest.param(t.DefaultParamsTransform, {"set": {1: True}}, "default-params config 'set' must have string paths", id="default-params-path-type"),
    pytest.param(t.DefaultParamsTransform, {"set": ""}, "default-params config 'set' must be a mapping or null", id="default-params-falsey-set"),
    pytest.param(t.ParamMapTransform, {"to": "y"}, "param-map config 'from' is required", id="param-map-missing-from"),
    pytest.param(t.ParamMapTransform, {"from": "x"}, "param-map config 'to' is required", id="param-map-missing-to"),
    pytest.param(t.ParamMapTransform, {"from": [], "to": "y"}, "param-map config 'from' must be a string", id="param-map-from-type"),
    pytest.param(t.ParamMapTransform, {"from": "x", "to": None}, "param-map config 'to' must be a string", id="param-map-to-type"),
    pytest.param(t.MessageInjectTransform, {"role": []}, "message-inject config 'role' must be a string", id="message-inject-role-type"),
    pytest.param(t.MessageInjectTransform, {"text": 1}, "message-inject config 'text' must be a string", id="message-inject-text-type"),
    pytest.param(t.MessageInjectTransform, {"mode": None}, "message-inject config 'mode' must be a string", id="message-inject-mode-type"),
    pytest.param(t.ClampTransform, {}, "clamp config 'param' is required", id="clamp-missing-param"),
    pytest.param(t.ClampTransform, {"param": []}, "clamp config 'param' must be a string", id="clamp-param-type"),
    pytest.param(t.ClampTransform, {"param": "x", "min": "0"}, "clamp config 'min' must be a number or null", id="clamp-min-type"),
    pytest.param(t.ClampTransform, {"param": "x", "max": []}, "clamp config 'max' must be a number or null", id="clamp-max-type"),
]
_PRIMITIVE_CLASSES = [
    t.ValueMapTransform, t.DropParamsTransform, t.DefaultParamsTransform,
    t.ParamMapTransform, t.MessageInjectTransform, t.ClampTransform,
]


def _assert_invalid_primitive_config(factory, config, message):
    # Constructor-time errors must identify the primitive and offending field.
    # A later KeyError/TypeError/AttributeError is deliberately not accepted.
    with pytest.raises(ValueError) as raised:
        factory(config)
    assert str(raised.value) == message


@pytest.mark.parametrize("factory,config,message", _INVALID_PRIMITIVE_CONFIGS)
def test_invalid_primitive_config_is_rejected_clearly(factory, config, message):
    _assert_invalid_primitive_config(factory, config, message)


@pytest.mark.parametrize("factory", _PRIMITIVE_CLASSES)
@pytest.mark.parametrize("config", [None, [], "bad", 1])
def test_each_primitive_rejects_a_non_mapping_config(factory, config):
    with pytest.raises(ValueError, match="config must be a mapping"):
        factory(config)


@pytest.mark.parametrize("factory,config,message", _INVALID_PRIMITIVE_CONFIGS)
def test_invalid_config_cases_detect_removed_validation(monkeypatch, factory, config, message):
    # Negative control: forgetting the constructor's validation call used to
    # either accept the config or raise an incidental KeyError. Run exactly the
    # same contract assertion against that regression, then restore validation.
    original = t._validate_primitive_config
    with monkeypatch.context() as broken:
        broken.setattr(t, "_validate_primitive_config", lambda cfg, kind: None)
        with pytest.raises((pytest.fail.Exception, AssertionError, KeyError, TypeError, AttributeError)):
            _assert_invalid_primitive_config(factory, config, message)
    assert t._validate_primitive_config is original
    _assert_invalid_primitive_config(factory, config, message)


@pytest.mark.parametrize("kind,config,body,expected", [
    ("value-map", {"param": "x"}, {"x": "keep"}, {"x": "keep"}),
    ("value-map", {"param": "x", "map": None}, {"x": "keep"}, {"x": "keep"}),
    ("drop-params", {}, {"x": 1}, {"x": 1}),
    ("drop-params", {"drop": None}, {"x": 1}, {"x": 1}),
    ("default-params", {}, {"x": 1}, {"x": 1}),
    ("default-params", {"set": None}, {"x": 1}, {"x": 1}),
    ("param-map", {"from": "x", "to": "y"}, {"x": 1}, {"y": 1}),
    ("message-inject", {}, {"messages": []}, {"messages": [{"role": "system", "content": ""}]}),
    ("clamp", {"param": "x"}, {"x": 10}, {"x": 10}),
    ("clamp", {"param": "x", "min": None, "max": None}, {"x": 10}, {"x": 10}),
])
def test_missing_optional_primitive_keys_keep_their_defaults(kind, config, body, expected):
    assert t.run_request_pipeline([{"kind": kind, **config}], body) == expected


@pytest.mark.parametrize("kind,config,message", [
    ("value-map", {"param": "x", "map": []}, "value-map config 'map' must be a mapping or null"),
    ("drop-params", {"drop": "x"}, "drop-params config 'drop' must be a list of strings or null"),
    ("default-params", {"set": []}, "default-params config 'set' must be a mapping or null"),
    ("param-map", {"from": "x"}, "param-map config 'to' is required"),
    ("message-inject", {"text": 1}, "message-inject config 'text' must be a string"),
    ("clamp", {"param": "x", "max": "2"}, "clamp config 'max' must be a number or null"),
])
def test_invalid_inline_and_registered_configs_fail_before_body_mutation(kind, config, message):
    import copy

    spec = {"kind": kind, **config}
    body = {"x": 3, "messages": [{"role": "system", "content": "original"}]}
    original = copy.deepcopy(body)
    with pytest.raises(ValueError) as raised:
        t.run_request_pipeline([spec], body)
    assert str(raised.value) == message
    assert body == original
    with pytest.raises(ValueError) as raised:
        t.TransformRegistry().register("invalid", spec)
    assert str(raised.value) == message
