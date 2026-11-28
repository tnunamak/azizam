import json

from gateway.backends.llm_execution import _strip_litellm_router_body_overrides


def test_local_role_and_sampler_fields_survive_the_router_filter():
    body = json.dumps({
        "model": "default-fast",
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "dry_multiplier": 0.8,
        "fallbacks": ["openrouter/paid"],
        "router_settings_override": {"num_retries": 3},
    }).encode()
    out = json.loads(_strip_litellm_router_body_overrides(body, trusted_num_retries=None, trusted_max_retries=None))
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
    assert out["top_k"] == 20 and out["min_p"] == 0.0 and out["repetition_penalty"] == 1.0
    assert out["dry_multiplier"] == 0.8
    assert "fallbacks" not in out and "router_settings_override" not in out
