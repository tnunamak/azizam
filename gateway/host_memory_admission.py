"""Wait for host memory before a prompt that loads model files reaches the graph engine.

The engine stages whole weight files in host RAM within seconds of a prompt. With the host close to its floor that burst
starts a storm of kills. The gateway is the only sender of those prompts, so it waits in front of the send.

What a prompt loads is read from the prompt: each string value that ends with a model-file extension. The size is the
file's size on disk under the configured roots (``registry/host_memory_admission.yml``; the engine directory named by
the engine directory the registry file names adds its models folder and its extra-paths file's base paths). A model whose file is not
found counts for nothing; a prompt with no sized model is admitted without a look at memory. A model is resident when the
gateway sent it within ``resident_ttl_s`` and the engine's models were not freed since; residency the gateway cannot show is
treated as not resident.

The margin depends on the kind of App that asks (``admin_store.APP_KINDS``: a ``personal`` App has a person behind it; a
``service`` App has none and yields to every personal request). The margin per kind, ``wait_s`` and ``job_wait_s`` are owner
settings (key ``host_memory_admission`` in admin.db, ``validate_setting``); the shipped file is their read-only default and
the saved setting wins. Within a kind the order is first come, first served. A synchronous request waits ``wait_s`` and then gets the retryable busy answer.
A job waits up to ``job_wait_s`` and reports its wait to the caller through ``Ask.on_wait``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from gateway import admin_store, client_errors
from gateway.strict_yaml import safe_load_strict

LOG = logging.getLogger("azizam.host_memory")
GIB = 1 << 30
BUSY_REASON = "host_memory_busy"
BUSY_CODE = "resource_unavailable"
WAITING_PHASE = "waiting_for_memory"
_DECLARATION = Path(__file__).resolve().parents[1] / "registry" / "host_memory_admission.yml"
_KEYS = {"schema_version", "margin_gb", "wait_s", "job_wait_s", "poll_s", "report_s", "settle_s",
         "resident_ttl_s", "retry_after_s", "model_extensions", "model_roots", "engine_dir_env",
         "engine_models_folder", "engine_extra_paths_file"}
SETTING_KEY = "host_memory_admission"
PERSONAL, SERVICE = admin_store.APP_KINDS
ENV_MARGIN, ENV_WAIT, ENV_ROOTS = ("AZIZAM_HOST_MEMORY_MARGIN_GB", "AZIZAM_HOST_MEMORY_WAIT_S", "AZIZAM_HOST_MEMORY_MODEL_ROOTS")


@dataclass(frozen=True)
class HostMemoryPolicy:
    margin_bytes_by_kind: Mapping[str, int] = field(default_factory=lambda: {PERSONAL: 12 * GIB, SERVICE: 20 * GIB})
    wait_s: float = 300.0
    job_wait_s: float = 7200.0
    poll_s: float = 2.0
    report_s: float = 30.0
    settle_s: float = 30.0
    resident_ttl_s: float = 600.0
    retry_after_s: int = 30
    model_extensions: tuple[str, ...] = (".safetensors",)
    model_roots: tuple[str, ...] = ()


def app_kind_of(auth_kind: str | None) -> str:
    """The App kind behind a request's credential kind. The runtime identity carries ``auth_kind``, not the App row's kind;
    ``service_key`` is what an authenticated request of a ``service`` App has (``admin_runtime``), every other kind has a person."""
    return SERVICE if auth_kind == "service_key" else PERSONAL


def app_kind_of_caller(caller: Any) -> str:
    """The App kind behind an authenticated caller. Requests and jobs carry an auth context whose ``identity`` holds the
    credential kind; a caller without one (None, a resumed job whose caller cannot be rebuilt) has a person."""
    return app_kind_of(getattr(getattr(caller, "identity", None), "auth_kind", None))


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
    margins = document["margin_gb"]
    if not isinstance(margins, dict) or set(margins) != set(admin_store.APP_KINDS):
        raise ValueError("host_memory_admission.yml: margin_gb must give one margin per app kind")
    margins = {kind: float(env.get(ENV_MARGIN) or margins[kind]) if kind == PERSONAL else float(margins[kind])
               for kind in admin_store.APP_KINDS}
    roots = [r for r in (env.get(ENV_ROOTS) or "").split(os.pathsep) if r] or [str(r) for r in document["model_roots"]]
    engine_dir = env.get(str(document["engine_dir_env"]))
    if engine_dir:
        roots += [r for r in _engine_roots(engine_dir, str(document["engine_models_folder"]),
                                           str(document["engine_extra_paths_file"])) if r not in roots]
    return HostMemoryPolicy(
        margin_bytes_by_kind={kind: int(gb * GIB) for kind, gb in margins.items()},
        wait_s=float(env.get(ENV_WAIT) or document["wait_s"]), job_wait_s=float(document["job_wait_s"]),
        poll_s=float(document["poll_s"]), report_s=float(document["report_s"]), settle_s=float(document["settle_s"]),
        resident_ttl_s=float(document["resident_ttl_s"]), retry_after_s=int(document["retry_after_s"]),
        model_extensions=tuple(str(e).lower() for e in document["model_extensions"]), model_roots=tuple(roots))


# -- the owner's setting (admin.db) over the shipped defaults ------------------------------------------------------------

class InvalidHostMemorySetting(ValueError):
    """The submitted setting is not a margin per app kind and two waits, or is out of range."""


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def validate_setting(value: Any) -> dict[str, Any]:
    """``{"margin_gb": {"personal": n, "service": n}, "wait_s": n, "job_wait_s": n}`` with non-negative numbers and
    ``wait_s`` not above ``job_wait_s``, or a clear refusal."""
    if not isinstance(value, Mapping) or set(value) != {"margin_gb", "wait_s", "job_wait_s"}:
        raise InvalidHostMemorySetting("Supply margin_gb (one number per app kind), wait_s and job_wait_s.")
    margins = value["margin_gb"]
    if not isinstance(margins, Mapping) or set(margins) != set(admin_store.APP_KINDS):
        raise InvalidHostMemorySetting("margin_gb needs one number for each of: " + ", ".join(admin_store.APP_KINDS) + ".")
    checked: dict[str, Any] = {"margin_gb": {}}
    for kind in admin_store.APP_KINDS:
        if _number(margins[kind]) is None:
            raise InvalidHostMemorySetting(f"margin_gb.{kind} must be a number of GB, zero or more.")
        checked["margin_gb"][kind] = margins[kind]
    for key in ("wait_s", "job_wait_s"):
        if _number(value[key]) is None:
            raise InvalidHostMemorySetting(f"{key} must be a number of seconds, zero or more.")
        checked[key] = value[key]
    if checked["wait_s"] > checked["job_wait_s"]:
        raise InvalidHostMemorySetting("wait_s (requests) cannot be longer than job_wait_s (jobs).")
    return checked


def stored_setting(path: Path) -> dict[str, Any] | None:
    """The owner's saved setting, or None when none is saved. A damaged row counts as none."""
    record = admin_store.get_operator_setting(SETTING_KEY, path=path)
    if record:
        try:
            return validate_setting(record["value"])
        except InvalidHostMemorySetting:
            LOG.warning("host memory: the saved setting is invalid; the shipped defaults apply")
    return None


def effective(policy: HostMemoryPolicy, stored: Mapping[str, Any] | None) -> HostMemoryPolicy:
    """``policy`` (shipped file and environment) with the owner's saved setting over it."""
    if not stored:
        return policy
    return replace(policy, margin_bytes_by_kind={kind: int(float(gb) * GIB) for kind, gb in stored["margin_gb"].items()},
                   wait_s=float(stored["wait_s"]), job_wait_s=float(stored["job_wait_s"]))


def setting_view(policy: HostMemoryPolicy) -> dict[str, Any]:
    """The setting as the owner sees it (GB and seconds) for ``policy``."""
    return {"margin_gb": {kind: round(size / GIB, 3) for kind, size in policy.margin_bytes_by_kind.items()},
            "wait_s": policy.wait_s, "job_wait_s": policy.job_wait_s}


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

    app_kind: str = PERSONAL
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
    background: bool    # a service App's request: it yields to every personal one
    seq: int


class HostMemoryAdmission:
    def __init__(self, policy: HostMemoryPolicy, *, read_available: Callable[[], int | None] = read_mem_available,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                 saved_setting: Callable[[], Mapping[str, Any] | None] | None = None) -> None:
        self.policy = policy
        self._saved = saved_setting     # read on every admission: a save by the owner applies to the next prompt
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
        policy = effective(self.policy, await asyncio.to_thread(self._saved) if self._saved else None)
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
        background = ask.app_kind == SERVICE
        margin = policy.margin_bytes_by_kind[ask.app_kind]
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
        _SHARED = HostMemoryAdmission(load_policy(), saved_setting=lambda: stored_setting(admin_store.DB_PATH))
    return _SHARED
