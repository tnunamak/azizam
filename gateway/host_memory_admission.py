"""Wait for host memory before a prompt that loads model files reaches the graph engine.

The engine stages whole weight files in host RAM within seconds of a prompt. With the host close to its floor that burst
starts a storm of kills. The gateway is the only sender of those prompts, so it waits in front of the send.

What a prompt loads is read from the prompt: each string value that ends with a model-file extension. The size is the
file's size on disk under the configured roots (``registry/host_memory_admission.yml``; the engine directory named by
the engine directory the registry file names adds its models folder and its extra-paths file's base paths). A model whose file is not
found counts for nothing; a prompt with no sized model is admitted without a look at memory. A model is resident when the
gateway sent it within ``resident_ttl_s`` and the engine's models were not freed since; residency the gateway cannot show is
treated as not resident.

A request with a person behind it is admitted before service-key work (``background_auth_kinds``, which also uses the larger margin); within a
class the order is first come, first served. A synchronous request waits ``wait_s`` and then gets the retryable busy answer.
A job waits up to ``job_wait_s`` and reports its wait to the caller through ``Ask.on_wait``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from gateway import client_errors
from gateway.strict_yaml import safe_load_strict

LOG = logging.getLogger("azizam.host_memory")
GIB = 1 << 30
BUSY_REASON = "host_memory_busy"
BUSY_CODE = "resource_unavailable"
WAITING_PHASE = "waiting_for_memory"
_DECLARATION = Path(__file__).resolve().parents[1] / "registry" / "host_memory_admission.yml"
_KEYS = {"schema_version", "margin_gb", "background_margin_gb", "wait_s", "job_wait_s", "poll_s", "report_s", "settle_s",
         "resident_ttl_s", "retry_after_s", "model_extensions", "model_roots", "background_auth_kinds", "engine_dir_env",
         "engine_models_folder", "engine_extra_paths_file"}
ENV_MARGIN, ENV_WAIT, ENV_ROOTS = ("AZIZAM_HOST_MEMORY_MARGIN_GB", "AZIZAM_HOST_MEMORY_WAIT_S", "AZIZAM_HOST_MEMORY_MODEL_ROOTS")


@dataclass(frozen=True)
class HostMemoryPolicy:
    margin_bytes: int = 12 * GIB
    background_margin_bytes: int = 20 * GIB
    wait_s: float = 300.0
    job_wait_s: float = 7200.0
    poll_s: float = 2.0
    report_s: float = 30.0
    settle_s: float = 30.0
    resident_ttl_s: float = 600.0
    retry_after_s: int = 30
    model_extensions: tuple[str, ...] = (".safetensors",)
    model_roots: tuple[str, ...] = ()
    background_auth_kinds: tuple[str, ...] = ()

    def is_background(self, auth_kind: str | None) -> bool:
        return bool(auth_kind) and auth_kind in self.background_auth_kinds


def _engine_roots(directory: str, models_folder: str, extra_file: str) -> list[str]:
    base = Path(directory)
    roots = [str(base / models_folder)] if (base / models_folder).is_dir() else []
    extra = base / extra_file
    try:
        document = safe_load_strict(extra.read_text(encoding="utf-8")) if extra.is_file() else None
    except (OSError, ValueError, Exception):  # noqa: BLE001 - an unreadable file adds no roots
        document = None
    for section in (document.values() if isinstance(document, Mapping) else ()):
        path = section.get("base_path") if isinstance(section, Mapping) else None
        if isinstance(path, str) and path.strip() and Path(path).is_dir():
            roots.append(str(Path(path)))
    return roots


def load_policy(environ: Mapping[str, str] | None = None) -> HostMemoryPolicy:
    env = os.environ if environ is None else environ
    document = safe_load_strict(_DECLARATION.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != _KEYS or document["schema_version"] != 1:
        raise ValueError("host_memory_admission.yml: every key must be known and schema_version must be 1")
    margin_gb = float(env.get(ENV_MARGIN) or document["margin_gb"])
    roots = [r for r in (env.get(ENV_ROOTS) or "").split(os.pathsep) if r] or [str(r) for r in document["model_roots"]]
    engine_dir = env.get(str(document["engine_dir_env"]))
    if engine_dir:
        roots += [r for r in _engine_roots(engine_dir, str(document["engine_models_folder"]),
                                           str(document["engine_extra_paths_file"])) if r not in roots]
    return HostMemoryPolicy(
        margin_bytes=int(margin_gb * GIB), background_margin_bytes=int(float(document["background_margin_gb"]) * GIB),
        wait_s=float(env.get(ENV_WAIT) or document["wait_s"]), job_wait_s=float(document["job_wait_s"]),
        poll_s=float(document["poll_s"]), report_s=float(document["report_s"]), settle_s=float(document["settle_s"]),
        resident_ttl_s=float(document["resident_ttl_s"]), retry_after_s=int(document["retry_after_s"]),
        model_extensions=tuple(str(e).lower() for e in document["model_extensions"]), model_roots=tuple(roots),
        background_auth_kinds=tuple(str(k) for k in document["background_auth_kinds"]))


def read_mem_available() -> int | None:
    """MemAvailable in bytes, or None when the host does not say."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def models_of(graph: Mapping[str, Any], extensions: Iterable[str]) -> list[str]:
    """The model-file names a prompt's nodes name, each once, in node order."""
    suffixes = tuple(extensions)
    found: dict[str, None] = {}
    for node in graph.values() if isinstance(graph, Mapping) else ():
        inputs = node.get("inputs") if isinstance(node, Mapping) else None
        for value in (inputs.values() if isinstance(inputs, Mapping) else ()):
            if isinstance(value, str) and value.lower().endswith(suffixes):
                found.setdefault(value, None)
    return list(found)


@dataclass(frozen=True)
class WaitState:
    needed_bytes: int
    available_bytes: int
    waited_s: float


def wait_event(state: WaitState | None) -> dict[str, Any]:
    """The event payload a waiting job shows its caller: the phase, and the memory it needs and the host has, in GB.
    ``None`` is the end of the wait: the job runs."""
    if state is None:
        return {"phase": "running", "status": "running"}
    return {"phase": WAITING_PHASE, "status": "running", "needed_gb": round(state.needed_bytes / GIB, 1),
            "available_gb": round(state.available_bytes / GIB, 1)}


@dataclass(frozen=True)
class Ask:
    """Who asks and how long it may wait. ``bounded``: a synchronous request, which gets the busy answer at ``wait_s``."""

    auth_kind: str | None = None
    bounded: bool = True
    on_wait: Callable[[WaitState | None], Any] | None = None


class HostMemoryBusy(Exception):
    """The host did not have room in time. The caller may ask again."""

    def __init__(self) -> None:
        super().__init__(BUSY_CODE)
        self.code = BUSY_CODE
        self.category = "capacity"
        self.retryable = True
        self.client_message = client_errors.REASONS[BUSY_REASON]


def busy_response(retry_after_s: int | None = None):
    """The synchronous answer: 503, code ``resource_unavailable``, with Retry-After."""
    seconds = retry_after_s if retry_after_s is not None else load_policy().retry_after_s
    return client_errors.client_error(503, BUSY_REASON, code=BUSY_CODE, error_type="api_error",
                                      headers={"Retry-After": client_errors.retry_after(str(seconds)) or "30"})


@dataclass(eq=False)
class _Waiter:
    background: bool
    seq: int


class HostMemoryAdmission:
    def __init__(self, policy: HostMemoryPolicy, *, read_available: Callable[[], int | None] = read_mem_available,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self._read, self._sleep, self._clock = read_available, sleep, clock
        self._loaded: dict[str, float] = {}
        self._reserved: list[tuple[float, int]] = []
        self._waiting: list[_Waiter] = []
        self._ticket = itertools.count()
        self._sizes: dict[str, int | None] = {}

    # -- what is known -----------------------------------------------------------------------------------------------

    def size_of(self, name: str) -> int | None:
        """Bytes of the model file ``name`` under a root (directly, or in a folder one level down); None when not found."""
        if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
            return None
        if name in self._sizes and self._sizes[name] is not None:
            return self._sizes[name]
        size = None
        for root in self.policy.model_roots:
            try:
                candidates = [Path(root) / name, *(Path(root) / d.name / name for d in os.scandir(root) if d.is_dir())]
                size = next((c.stat().st_size for c in candidates if c.is_file()), None)
            except OSError:
                size = None
            if size is not None:
                break
        self._sizes[name] = size
        return size

    def _resident(self, name: str, now: float) -> bool:
        stamp = self._loaded.get(name)
        return stamp is not None and now - stamp < self.policy.resident_ttl_s

    def forget_all(self) -> None:
        """The engine freed its models: none is resident."""
        self._loaded.clear()

    # -- admission ---------------------------------------------------------------------------------------------------

    def _first(self, waiter: _Waiter) -> bool:
        return min(self._waiting, key=lambda w: (w.background, w.seq)) is waiter

    async def admit(self, graph: Mapping[str, Any], ask: Ask = Ask()) -> None:
        policy = self.policy
        names = models_of(graph, policy.model_extensions)
        now = self._clock()
        fresh = [n for n in names if not self._resident(n, now)]
        sizes = await asyncio.to_thread(lambda: [self.size_of(n) for n in fresh])
        staged = sum(s for s in sizes if s)
        for name, size in zip(fresh, sizes):
            if size is None:
                LOG.info("host memory: no size known for a model of this prompt; it counts for nothing")
        if staged == 0:
            self._loaded.update(dict.fromkeys(names, now))
            return
        background = policy.is_background(ask.auth_kind)
        margin = policy.background_margin_bytes if background else policy.margin_bytes
        limit = policy.wait_s if ask.bounded else policy.job_wait_s
        waiter = _Waiter(background, next(self._ticket))
        self._waiting.append(waiter)
        started, reported = self._clock(), None
        try:
            while True:
                available = await asyncio.to_thread(self._read)
                if available is None:
                    LOG.warning("host memory: MemAvailable is unreadable; the prompt is admitted")
                    break
                now = self._clock()
                self._reserved = [(until, size) for until, size in self._reserved if until > now]
                needed = staged + margin + sum(size for _, size in self._reserved)
                if self._first(waiter) and available >= needed:
                    break
                if now - started >= limit:
                    LOG.warning("host memory: busy; needed %.1f GB, available %.1f GB", needed / GIB, available / GIB)
                    raise HostMemoryBusy()
                if ask.on_wait is not None and (reported is None or now - reported >= policy.report_s):
                    reported = now
                    result = ask.on_wait(WaitState(needed, available, now - started))
                    if asyncio.iscoroutine(result):
                        await result
                await self._sleep(policy.poll_s)
        finally:
            self._waiting.remove(waiter)
        now = self._clock()
        self._reserved.append((now + policy.settle_s, staged))
        self._loaded.update(dict.fromkeys(names, now))
        if ask.on_wait is not None and reported is not None:
            result = ask.on_wait(None)      # the wait is over
            if asyncio.iscoroutine(result):
                await result


_SHARED: HostMemoryAdmission | None = None


def shared() -> HostMemoryAdmission:
    """The process's one admission: the host's memory is one resource, whichever adapter sends the prompt."""
    global _SHARED
    if _SHARED is None:
        _SHARED = HostMemoryAdmission(load_policy())
    return _SHARED
