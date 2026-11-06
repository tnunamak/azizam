"""The one generic transition adapter: it drives a reviewed profile's declared operations (design D3).

``DeclaredOperationAdapter`` implements ``TransitionAdapter`` for ``release``, ``load``, ``query``, ``idle`` and
``resident``. It holds no engine knowledge. The profile (``gateway/transition_profiles.py``) maps each of those to
an operation name in the engine's own protocol description; ``invoke`` sends that operation through the existing
http-v1 invoke path and returns the reply. What the engine said is read from the reply's ``selected`` values
(the description's ``response.select``), so a new engine's answer shape is data in its description.

Rules:
- ``attested`` comes from the profile, never from the reply or the owner's config.
- Any transport error, a reply that is not a finished answer, or an ``outcome`` the adapter does not know is
  ``unknown``. The executor then asks ``query``; nothing reopens on an unknown outcome.
- A declared ``failed`` outcome is reported as ``partial``: the step did not succeed, so the gate stays closed.
- Evidence is stamped with the adapter's clock, so the executor's freshness window is the time of the read.
- ``checkpoint``, ``restore`` and ``reset`` are not driven here (T1.3).
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from gateway.transition_ops import EffectResult, Evidence, StepIntent
from gateway.transition_profiles import Profile

Invoke = Callable[[str, str, Mapping[str, Any]], Awaitable[Any]]
"""``invoke(domain, protocol_op, body)`` -> a reply with ``status`` and ``selected`` (http-v1 ``Reply``). It is bound
to the right engine record by the caller; it raises on a transport error."""

_OUTCOMES = {"success": "success", "no_change": "no_change", "partial": "partial", "failed": "partial",
             "unknown": "unknown"}


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


class DeclaredOperationAdapter:
    supports_checkpoint = False

    def __init__(self, profile: Profile, invoke: Invoke, *, clock: Callable[[], float] = time.time) -> None:
        self._profile = profile
        self._invoke = invoke
        self._clock = clock

    async def _selected(self, domain: str, key: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        name = self._profile.operations.get(key)
        if name is None:
            return {}
        reply = await self._invoke(domain, name, body)
        status = getattr(reply, "status", 0)
        selected = getattr(reply, "selected", None)
        if not isinstance(status, int) or not 200 <= status < 300 or not isinstance(selected, Mapping):
            return {}
        return selected

    async def _evidence(self, domain: str, key: str, flag: str) -> Evidence:
        try:
            selected = await self._selected(domain, key, {})
        except Exception:  # noqa: BLE001 - a read that failed observed nothing
            return Evidence(value=None, observed_at=self._clock())
        detail: dict[str, object] = {}
        offering = _text(selected.get("offering"))
        if offering is not None:
            detail["offering"] = offering
        ready = _flag(selected.get("ready"))
        if ready is not None:
            detail["ready"] = ready
        return Evidence(value=_flag(selected.get(flag)), observed_at=self._clock(),
                        incarnation=_text(selected.get("incarnation")), detail=detail)

    async def idle(self, domain: str) -> Evidence:
        return await self._evidence(domain, "idle", "idle")

    async def resident(self, domain: str) -> Evidence:
        return await self._evidence(domain, "resident", "resident")

    async def _effect(self, domain: str, key: str, body: Mapping[str, Any], *, attests: bool) -> EffectResult:
        try:
            selected = await self._selected(domain, key, body)
        except Exception:  # noqa: BLE001 - the engine may or may not have acted
            return EffectResult(outcome="unknown", evidence_ref="invoke_error")
        outcome = _OUTCOMES.get(str(selected.get("outcome") or ""), "unknown")
        return EffectResult(outcome=outcome,  # type: ignore[arg-type]
                            evidence_ref=_text(selected.get("evidence_ref")),
                            incarnation=_text(selected.get("incarnation")),
                            attested=bool(attests and self._profile.attested and outcome == "success"))

    @staticmethod
    def _body(intent: StepIntent, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"effect_key": intent.effect_key}
        if intent.expected_incarnation is not None:
            body["expected_incarnation"] = intent.expected_incarnation
        body.update(extra)
        return body

    async def release(self, domain: str, intent: StepIntent) -> EffectResult:
        return await self._effect(domain, "release", self._body(intent), attests=True)

    async def load(self, domain: str, target: str, intent: StepIntent) -> EffectResult:
        return await self._effect(domain, "load", self._body(intent, target=target), attests=False)

    async def query(self, domain: str, intent: StepIntent) -> EffectResult:
        return await self._effect(domain, "query", self._body(intent, step=intent.step), attests=intent.step == "release")

    async def checkpoint(self, domain: str, intent: StepIntent) -> EffectResult:
        return EffectResult(outcome="unknown", evidence_ref="checkpoint_unsupported")

    async def restore(self, domain: str, handle: str, intent: StepIntent) -> EffectResult:
        return EffectResult(outcome="unknown", evidence_ref="restore_unsupported")

    async def reset(self, domain: str, intent: StepIntent) -> EffectResult:
        return EffectResult(outcome="unknown", evidence_ref="reset_unsupported")
