"""Two differently shaped fake adapters for the checkpoint and restore steps, over one shared fake engine state.

``SyncAdapter`` answers each effect when it finishes, after an optional delay, and can finish after its caller timed out.
``DeferredAdapter`` answers every checkpoint, restore and reset with ``unknown`` and reveals the real answer only through
``query``. Both run from the same script (``rig.script("restore", "no_change")``), so one scenario checks both shapes.

Scripted outcomes for checkpoint, restore and reset: ``success`` (default), ``no_change``, ``partial``, ``unknown``
(the effect did not happen and the reply is lost), plus checkpoint-only ``half_written`` (fewer bytes on disk than
reported) and ``no_file`` (success reported, nothing written). ``delay`` makes a sync effect finish that many seconds later.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from gateway.transition_ops import EffectResult, Evidence, StepIntent
from gateway.transition_snapshots import PrivateSnapshotDir, SnapshotBudget, SqliteSnapshotStore

PAYLOAD = b"cache-bytes-" * 8


@dataclass
class Engine:
    """The state both adapters share."""

    files: PrivateSnapshotDir
    offering: Optional[str] = "model-a"
    resident: bool = True
    ready: bool = True
    """Readiness of the resident offering; the load step of the executor needs it (design D4 row 5)."""
    compat: dict = field(default_factory=lambda: {"artifact": "a1", "tokenizer": "t1", "engine": "e1", "cache_format": "c1", "defaults": "d1", "placement": "gpu0"})
    observations: dict = field(default_factory=dict)
    restored: Optional[str] = None
    dirty: bool = False
    calls: list = field(default_factory=list)
    effects: dict = field(default_factory=dict)
    scripts: dict = field(default_factory=dict)
    delays: dict = field(default_factory=dict)
    on_release: Optional[object] = None

    def script(self, name: str, *outcomes: str, delay: float = 0.0) -> None:
        self.scripts[name] = list(outcomes)
        self.delays[name] = delay

    def next_outcome(self, name: str) -> str:
        queue = self.scripts.get(name)
        return queue.pop(0) if queue else "success"


def _handle(intent: StepIntent) -> str:
    return intent.effect_key.replace(":", "-") + ".snap"


def _apply(engine: Engine, name: str, domain: str, intent: StepIntent, outcome: str, handle: Optional[str]) -> EffectResult:
    """What the effect does to the engine and the files, and what the engine would answer."""
    if name == "checkpoint":
        if outcome in ("success", "half_written", "no_file"):
            path = engine.files.path_for(domain, _handle(intent))
            if outcome != "no_file":
                path.write_bytes(PAYLOAD if outcome == "success" else PAYLOAD[: len(PAYLOAD) // 2])
                path.chmod(0o600)
            return EffectResult(outcome="success", handle=_handle(intent), bytes=len(PAYLOAD), evidence_ref="saved")
        return EffectResult(outcome=outcome, evidence_ref=f"checkpoint:{outcome}")  # type: ignore[arg-type]
    if name == "restore":
        if outcome == "success":
            engine.restored = handle
            return EffectResult(outcome="success", evidence_ref="restored")
        if outcome == "partial":
            engine.dirty = True
        return EffectResult(outcome=outcome, evidence_ref=f"restore:{outcome}")  # type: ignore[arg-type]
    if outcome == "success":  # reset
        engine.dirty, engine.restored = False, None
        return EffectResult(outcome="success", evidence_ref="reset")
    return EffectResult(outcome=outcome, evidence_ref=f"reset:{outcome}")  # type: ignore[arg-type]


class _Base:
    supports_checkpoint = True

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def idle(self, domain: str) -> Evidence:
        return Evidence(value=True, observed_at=time.time())

    async def resident(self, domain: str) -> Evidence:
        self.engine.calls.append("resident")
        value = self.engine.observations.get("resident", self.engine.resident)
        return Evidence(value=value, observed_at=time.time(),
                        detail={"offering": self.engine.offering, "ready": value is True and self.engine.ready})

    async def observe(self, domain: str, name: str) -> Evidence:
        self.engine.calls.append(f"observe:{name}")
        if name == "clean":
            value = not self.engine.dirty and self.engine.observations.get("clean", True)
        else:
            value = self.engine.observations.get(name, True)
        stale = self.engine.observations.get(f"{name}:stale", False)
        return Evidence(value=value, observed_at=time.time() - (100 if stale else 0))

    async def compatibility(self, domain: str):
        self.engine.calls.append("compatibility")
        return dict(self.engine.compat)

    async def release(self, domain: str, intent: StepIntent) -> EffectResult:
        self.engine.calls.append("release")
        self.engine.resident, self.engine.offering = False, None
        return EffectResult(outcome="success", attested=True)

    async def load(self, domain: str, target: str, intent: StepIntent) -> EffectResult:
        self.engine.calls.append("load")
        self.engine.resident, self.engine.offering = True, target
        return EffectResult(outcome="success")

    async def query(self, domain: str, intent: StepIntent) -> EffectResult:
        self.engine.calls.append("query")
        return self.engine.effects.get(intent.effect_key, EffectResult(outcome="unknown", evidence_ref="query:unknown"))


class SyncAdapter(_Base):
    """Answers when the effect finishes. With a delay, the effect finishes later even if the caller gave up."""

    async def _effect(self, name: str, domain: str, intent: StepIntent, handle: Optional[str] = None) -> EffectResult:
        self.engine.calls.append(name)
        outcome = self.engine.next_outcome(name)
        delay = self.engine.delays.get(name, 0.0)
        if outcome == "unknown":
            if delay:
                await asyncio.sleep(delay)
            return EffectResult(outcome="unknown", evidence_ref="reply_lost")

        def finish() -> EffectResult:
            result = _apply(self.engine, name, domain, intent, outcome, handle)
            self.engine.effects[intent.effect_key] = result
            return result

        if not delay:
            return finish()
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def complete() -> None:
            result = finish()  # the engine finishes even when its caller gave up
            if not future.done():
                future.set_result(result)

        asyncio.get_running_loop().call_later(delay, complete)
        return await future

    async def checkpoint(self, domain, intent):
        return await self._effect("checkpoint", domain, intent)

    async def restore(self, domain, handle, intent):
        return await self._effect("restore", domain, intent, handle)

    async def reset(self, domain, intent):
        return await self._effect("reset", domain, intent)


class DeferredAdapter(_Base):
    """Reports ``unknown`` for every effect; the answer is available through ``query``."""

    async def _effect(self, name: str, domain: str, intent: StepIntent, handle: Optional[str] = None) -> EffectResult:
        self.engine.calls.append(name)
        outcome = self.engine.next_outcome(name)
        if outcome != "unknown":
            self.engine.effects[intent.effect_key] = _apply(self.engine, name, domain, intent, outcome, handle)
        return EffectResult(outcome="unknown", evidence_ref="accepted")

    async def checkpoint(self, domain, intent):
        return await self._effect("checkpoint", domain, intent)

    async def restore(self, domain, handle, intent):
        return await self._effect("restore", domain, intent, handle)

    async def reset(self, domain, intent):
        return await self._effect("reset", domain, intent)


class Rig:
    def __init__(self, tmp_path: Path, shape: str, *, max_bytes: int = 10_000, max_count: int = 4) -> None:
        self.shape = shape
        self.files = PrivateSnapshotDir(tmp_path / "snapshots")
        self.db = tmp_path / "transition.db"
        self.budget = SnapshotBudget(max_bytes=max_bytes, max_count=max_count)
        self.snapshots = SqliteSnapshotStore(self.db, self.files, self.budget)
        self.engine = Engine(files=self.files)
        self.adapter = SyncAdapter(self.engine) if shape == "sync" else DeferredAdapter(self.engine)

    def reopen(self, **kwargs) -> SqliteSnapshotStore:
        """A restart: a new store over the same database and the same files."""
        self.snapshots = SqliteSnapshotStore(self.db, self.files, self.budget, **kwargs)
        return self.snapshots

    def policy(self, *, required: bool = False, checkpoint: Optional[Mapping] = None, restore: Optional[Mapping] = None,
               extra_profile: Optional[Mapping] = None, deadlines: Optional[Mapping] = None, query: Optional[bool] = None) -> dict:
        """A reviewed profile that declares checkpoint and restore with attested ``no_change`` failures and a reset."""
        deferred = self.shape == "deferred" if query is None else query
        profile = {
            "checkpoint": {"failure_semantics": "no_change", "query": deferred, **(checkpoint or {})},
            "restore": {"failure_semantics": "no_change", "query": deferred,
                        "reset": {"failure_semantics": "unknown", "query": deferred}, **(restore or {})},
            "placement_keys": ["placement"],
            "fresh_within_s": 5,
            **(extra_profile or {}),
        }
        return {"profile": profile, "checkpoint_policy": "required" if required else "best_effort",
                "deadlines": {"checkpoint": 1, "restore": 1, "reset": 1, "query": 1, **(deadlines or {})}}
