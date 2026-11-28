"""D-1: data attachment is the sole opt-in to request reasoning translation."""
import proxy
from gateway.alias_policy import ResolvedRequestTarget


def test_unattached_local_chat_preserves_reasoning_effort():
    body = {"model": "base", "messages": [], "reasoning_effort": "low"}
    target = ResolvedRequestTarget("base", "local", "base", (), ())
    error, prepared, _ = proxy._prepare_resolved_openai_request(
        body, request_path="/v1/chat/completions", target=target,
        execution_service_id="local",
        runtime_config={"backends": {"local": {"transforms": []}},
                        "reasoning": {"default_effort": "high"}},
    )
    assert error is None
    assert prepared == body


def test_logical_policy_does_not_supply_execution_service_attachments():
    body = {"model": "base", "messages": [], "reasoning_effort": "low"}
    target = ResolvedRequestTarget("base", "logical", "base", (), ())
    error, prepared, _ = proxy._prepare_resolved_openai_request(
        body, request_path="/v1/chat/completions", target=target,
        runtime_config={"backends": {"logical": {"transforms": [
            {"kind": "drop-params", "drop": ["messages"]}, "reasoning-effort",
        ]}}},
    )
    assert error is None
    assert prepared == body
