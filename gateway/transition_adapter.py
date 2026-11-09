"""The one generic transition adapter: it drives a reviewed profile's declared operations (design D3).

``DeclaredOperationAdapter`` implements ``TransitionAdapter`` for ``release``, ``load``, ``query``, ``idle`` and
``resident`` and, when the profile declares them, ``checkpoint``, ``restore`` and ``reset``, plus ``compatibility``
and the named observations answered by ``observe``. It holds no engine knowledge. The profile
(``gateway/transition_profiles.py``) maps each of those to
an operation name in the engine's own protocol description; ``invoke`` sends that operation through the existing
http-v1 invoke path and returns the reply. What the engine said is read from the reply's ``selected`` values
(the description's ``response.select``), so a new engine's answer shape is data in its description.

Rules:
- ``attested`` comes from the profile, never from the reply or the owner's config.
- Any transport error, a reply that is not a finished answer, or an ``outcome`` the adapter does not know is
  ``unknown``. The executor then asks ``query``; nothing reopens on an unknown outcome.
- A declared ``failed`` outcome is reported as ``partial``: the step did not succeed, so the gate stays closed.
- A checkpoint is the exception: a declared ``failed`` checkpoint (or a ``query`` of one) is reported as
  ``no_change``, the case the profile's checkpoint ``failure_semantics`` speaks about. The checkpoint step trusts it
  only when the profile attests ``no_change``; a declared ``partial`` checkpoint stays ``partial``.
- Evidence is stamped with the adapter's clock, so the executor's freshness window is the time of the read.
- ``checkpoint`` mints its own handle and writes the snapshot only at the path the snapshot store gives it; a
  success is never attested. ``compatibility`` is an empty map when the engine's answer is unknown.
"""

from __future__ import annotations

import os
import stat
import time
import uuid
from pathlib import Path
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


def _snapshot_bytes(value: Any) -> Optional[int]:
    """A byte count from a reply: a non-negative int, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _file_bytes(path: Path) -> Optional[int]:
    """The size of the file at ``path``, when it is a regular file."""
    try:
        facts = os.lstat(path)
    except OSError:
        return None
    return facts.st_size if stat.S_ISREG(facts.st_mode) else None


class DeclaredOperationAdapter:
    def __init__(self, profile: Profile, invoke: Invoke, *, clock: Callable[[], float] = time.time,
                 snapshot_path: Optional[Callable[[str, str], Path]] = None) -> None:
        self._profile = profile
        self._invoke = invoke
        self._clock = clock
        self._snapshot_path = snapshot_path
        self.supports_checkpoint = ("checkpoint" in profile.operations and "restore" in profile.operations
                                    and snapshot_path is not None)

    async def _reply_selected(self, domain: str, name: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """The ``selected`` values of one invoke of a protocol operation name, or ``{}`` when the reply is not a
        finished 2xx answer."""
        reply = await self._invoke(domain, name, body)
        status = getattr(reply, "status", 0)
        selected = getattr(reply, "selected", None)
        if not isinstance(status, int) or not 200 <= status < 300 or not isinstance(selected, Mapping):
            return {}
        return selected

    async def _selected(self, domain: str, key: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        name = self._profile.operations.get(key)
        if name is None:
            return {}
        return await self._reply_selected(domain, name, body)

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

    async def _effect(self, domain: str, key: str, body: Mapping[str, Any], *, attests: bool,
                      failed_as: str = "partial") -> EffectResult:
        try:
            selected = await self._selected(domain, key, body)
        except Exception:  # noqa: BLE001 - the engine may or may not have acted
            return EffectResult(outcome="unknown", evidence_ref="invoke_error")
        declared = str(selected.get("outcome") or "")
        outcome = failed_as if declared == "failed" else _OUTCOMES.get(declared, "unknown")
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
        return await self._effect(domain, "query", self._body(intent, step=intent.step), attests=intent.step == "release",
                                  failed_as="no_change" if intent.step == "checkpoint" else "partial")

    def _path(self, domain: str, handle: str) -> Optional[Path]:
        """The snapshot store's path for ``handle``, or None when the store refuses it."""
        assert self._snapshot_path is not None
        try:
            return self._snapshot_path(domain, handle)
        except Exception:  # noqa: BLE001 - a refused or unreadable snapshot directory
            return None

    async def checkpoint(self, domain: str, intent: StepIntent) -> EffectResult:
        """Mint the handle, send it with the path, and read the byte count from the reply or the file."""
        if "checkpoint" not in self._profile.operations or self._snapshot_path is None:
            return EffectResult(outcome="unknown", evidence_ref="checkpoint_unsupported")
        handle = f"ckpt-{uuid.uuid4().hex}"
        path = self._path(domain, handle)
        if path is None:  # nothing was sent, so the engine is unchanged
            return EffectResult(outcome="no_change", evidence_ref="snapshot_path_error")
        try:
            selected = await self._selected(domain, "checkpoint",
                                            self._body(intent, handle=handle, path=str(path)))
        except Exception:  # noqa: BLE001 - the engine may or may not have acted
            return EffectResult(outcome="unknown", evidence_ref="invoke_error", handle=handle)
        declared = str(selected.get("outcome") or "")
        outcome = "no_change" if declared == "failed" else _OUTCOMES.get(declared, "unknown")
        incarnation = _text(selected.get("incarnation"))
        if outcome != "success":
            return EffectResult(outcome=outcome,  # type: ignore[arg-type]
                                evidence_ref=_text(selected.get("evidence_ref")), handle=handle,
                                incarnation=incarnation)
        size = _snapshot_bytes(selected.get("bytes"))
        if size is None:
            size = _file_bytes(path)
        if size is None:
            return EffectResult(outcome="unknown", evidence_ref="checkpoint_no_size", handle=handle,
                                incarnation=incarnation)
        return EffectResult(outcome="success", evidence_ref=_text(selected.get("evidence_ref")), handle=handle,
                            bytes=size, incarnation=incarnation)

    async def restore(self, domain: str, handle: str, intent: StepIntent) -> EffectResult:
        if "restore" not in self._profile.operations or self._snapshot_path is None:
            return EffectResult(outcome="unknown", evidence_ref="restore_unsupported")
        path = self._path(domain, handle)
        if path is None:  # nothing was sent: the target stays as loaded, so it serves cold
            return EffectResult(outcome="no_change", evidence_ref="snapshot_path_error")
        return await self._effect(domain, "restore", self._body(intent, handle=handle, path=str(path)), attests=False)

    async def reset(self, domain: str, intent: StepIntent) -> EffectResult:
        if "reset" not in self._profile.operations:
            return EffectResult(outcome="unknown", evidence_ref="reset_unsupported")
        return await self._effect(domain, "reset", self._body(intent), attests=False)

    async def compatibility(self, domain: str) -> Mapping[str, str]:
        """The compatibility token as strings. An empty map means unknown, and unknown means cold."""
        if "compatibility" not in self._profile.operations:
            return {}
        try:
            selected = await self._selected(domain, "compatibility", {})
        except Exception:  # noqa: BLE001 - an unknown token means cold
            return {}
        found: dict[str, str] = {}
        for key, value in selected.items():
            if isinstance(value, bool):
                found[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                found[str(key)] = str(value)
        return found

    async def observe(self, domain: str, name: str) -> Evidence:
        """Answer one named observation from the profile. An unknown name observes nothing."""
        operation = self._profile.observations.get(name)
        if operation is None:
            return Evidence(value=None, observed_at=self._clock())
        try:
            selected = await self._reply_selected(domain, operation, {})
        except Exception:  # noqa: BLE001 - a read that failed observed nothing
            return Evidence(value=None, observed_at=self._clock())
        detail: dict[str, object] = {}
        offering = _text(selected.get("offering"))
        if offering is not None:
            detail["offering"] = offering
        ready = _flag(selected.get("ready"))
        if ready is not None:
            detail["ready"] = ready
        return Evidence(value=_flag(selected.get("value")), observed_at=self._clock(),
                        incarnation=_text(selected.get("incarnation")), detail=detail)
