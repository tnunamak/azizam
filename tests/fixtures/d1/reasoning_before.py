from typing import Any
QWEN3_ENABLE_THINKING_KEY = "enable_thinking"

def apply_thinking_suppression(body: dict[str, Any]) -> None:
    """Suppress reasoning/thinking on *body* in-place via the standard mechanism.

    Sets ``chat_template_kwargs.enable_thinking = False`` (via setdefault). This
    is the ecosystem-standard reasoning toggle (vLLM auto-maps reasoning_effort
    to it; the model's own chat template interprets it) — engine-agnostic DATA,
    not a prompt-string hack.

    The legacy ``/no_think`` PROMPT-STRING injection was removed: it is
    non-standard AND verified non-functional on this deployment (a live
    reasoning_effort=none test showed the enable_thinking kwarg suppresses
    thinking while the /no_think tag alone did not). See
    docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md D-1.
    """
    kwargs = body.setdefault("chat_template_kwargs", {})
    if isinstance(kwargs, dict):
        kwargs.setdefault(QWEN3_ENABLE_THINKING_KEY, False)

class ReasoningEffortTransform:
    """Translate qualitative effort into the configured backend budget.

    Registered as "reasoning-effort" in REGISTRY.

    Handles request phase only (validated pop of reasoning_effort + body mutation).
    Returns a string error message if reasoning_effort is invalid (the caller
    must turn this into a 400 Response — the 400 plumbing stays in proxy.py).

    Contract:
      1. reasoning_effort absent → no-op.
      2. Pop reasoning_effort. If None → no-op (treat as absent after pop).
      3. If not str or not in _REASONING_EFFORT_VALUES → return error string.
      4. Translate the configured effort to ``thinking_budget_tokens``.
      5. For zero budget, also set ``enable_thinking=False``.
    """

    # The valid set — matches _REASONING_EFFORT_VALUES in proxy.py:5235
    _VALID: frozenset[str] = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
    # Efforts that suppress thinking
    _DEFAULT_BUDGETS: dict[str, int | str] = {
        "none": 0,
        "minimal": 2048,
        "low": 4096,
        "medium": 6144,
        "high": 8192,
        "xhigh": "unlimited",
    }

    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        policy = policy or {}
        raw_budgets = policy.get("effort_budgets")
        budgets = raw_budgets if isinstance(raw_budgets, dict) else self._DEFAULT_BUDGETS
        self._budgets: dict[str, int | str] = dict(budgets)
        self._default_effort = policy.get("default_effort")

    def apply(self, body: dict[str, Any]) -> str | None:
        """Mutate body in-place.  Return an error string or None."""
        if "reasoning_effort" not in body:
            # Internal routing can pass through the gateway more than once
            # (gateway -> LiteLLM -> gateway -> engine).  A concrete budget is
            # the output of this transform, so preserve it instead of applying
            # the default policy a second time.
            if "thinking_budget_tokens" in body:
                existing_budget = body["thinking_budget_tokens"]
                if (
                    not isinstance(existing_budget, int)
                    or isinstance(existing_budget, bool)
                    or existing_budget < -1
                ):
                    return (
                        "Invalid value for 'thinking_budget_tokens': "
                        f"{existing_budget!r}. Expected an integer >= -1."
                    )
                return None
            if isinstance(self._default_effort, str):
                body["reasoning_effort"] = self._default_effort
            else:
                return None
        effort = body.pop("reasoning_effort")
        if effort is None:
            return None
        if not isinstance(effort, str) or effort not in self._VALID:
            return (
                f"Invalid value for 'reasoning_effort': {effort!r}. "
                f"Expected one of: {sorted(self._VALID)}."
            )
        budget = self._budgets.get(effort)
        if not isinstance(budget, (int, str)) or isinstance(budget, bool):
            return f"No reasoning budget configured for effort {effort!r}."
        if isinstance(budget, str) and budget != "unlimited":
            return f"Invalid reasoning budget configured for effort {effort!r}."
        body["thinking_budget_tokens"] = -1 if budget == "unlimited" else budget
        if budget == 0:
            apply_thinking_suppression(body)
        return None

    # Transform protocol shim — lets this live in the registry alongside
    # primitives even though it is not a _Primitive subclass.
    def on_request(self, body: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        # Error return is handled by the dispatch site; here we just mutate.
        self.apply(body)
        return body

    def on_response(self, payload: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        return payload
