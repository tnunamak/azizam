"""What an engine's node list says it holds, kept for the availability lists.

A workflow declares the node classes and model files it needs (``requires`` in its binding). Whether the engine has them
is read from the engine's node list, the same list the owner's engine check reads (``engine_check.node_list_inventory``).
That list is large, so a listing never fetches it. A loop that runs beside the gateway reads each engine's list now and
then and keeps the names; a listing reads what is kept.

Three rules keep this safe:

- **A listing never waits for an engine.** ``fresh`` and ``fresh_all`` read memory only.
- **Fail open.** A name set that was never read, is older than ``MAX_AGE_S`` or could not be read is "not known":
  ``fresh`` answers ``None`` and the availability of the workflow is what it was before. Only a fresh set that lacks a
  required name can mark a workflow.
- **No engine is named here.** Where the list is, and which engines have one, is data: the engine definitions
  (``registry/engine_definitions.json``) name a list probe of kind ``node`` for a protocol, and the caller passes the
  address of each engine that speaks it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

REFRESH_EVERY_S = 300.0   # the loop reads each engine's list this often
MAX_AGE_S = 900.0         # a kept list older than this is not known (three missed reads)
FIRST_READ_AFTER_S = 5.0  # the first read, after start-up
READ_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 3.0
DEFAULT_CAP_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Source:
    """Where one engine's node list is read: its full URL and the most bytes read from it."""

    url: str
    cap_bytes: int = DEFAULT_CAP_BYTES


@dataclass(frozen=True)
class NodeInventory:
    """The node classes and combo names one read of an engine's node list held, and when it was read (epoch seconds)."""

    node_classes: frozenset[str]
    model_names: tuple[str, ...]
    fetched_at: float
    url: str = ""
    _verdicts: dict[str, tuple[Any, ...]] = field(default_factory=dict, compare=False, repr=False)

    def missing(self, requires: Mapping[str, Any]) -> tuple[Any, ...]:
        """The ``RequirementIssue`` rows this list shows for a ``requires`` block (``workflow_requirements.judge_by_names``).

        A required hash is left out of the judgement (a list of names cannot check one), so a row here is a name the list
        does not offer. Kept per block for the life of this read, so a listing that judges the same workflow many times
        judges it once. Raises ``ValueError`` for a block that cannot be read."""
        from gateway.workflow_requirements import judge_by_names

        # A list of names cannot check a hash: a pinned file counts as present when a file of that name is listed.
        models = [{k: v for k, v in entry.items() if k != "sha256"} if isinstance(entry, Mapping) else entry
                  for entry in requires.get("models") or []]
        block = {"custom_nodes": requires.get("custom_nodes") or [], "models": models}
        key = json.dumps(block, sort_keys=True, default=str)
        held = self._verdicts.get(key)
        if held is None:
            held = self._verdicts[key] = tuple(judge_by_names(block, self.node_classes, self.model_names).missing)
        return held


def from_node_list(body: Any, *, fetched_at: float, url: str = "") -> NodeInventory | None:
    """The inventory a node list holds, or ``None`` when the body is not a node list."""
    from gateway.engine_check import node_list_inventory

    read = node_list_inventory(body)
    if read is None:
        return None
    return NodeInventory(frozenset(read[0]), tuple(read[1]), fetched_at, url)


# ------------------------------------------------------------------------------------------------ the kept lists

_KEPT: dict[str, NodeInventory] = {}


def reset() -> None:
    _KEPT.clear()


def store(service_id: str, inventory: NodeInventory) -> None:
    """Keep ``inventory`` as the engine's current list. The seam a test fills."""
    _KEPT[service_id] = inventory


def fresh(service_id: str, *, now: float | None = None) -> NodeInventory | None:
    """The engine's kept list when it is younger than ``MAX_AGE_S``, else ``None`` (not known). Reads memory only."""
    held = _KEPT.get(service_id)
    now = time.time() if now is None else now
    if held is None or not (0 <= now - held.fetched_at < MAX_AGE_S):
        return None
    return held


def fresh_all(*, now: float | None = None) -> dict[str, NodeInventory]:
    """Every engine's fresh list, by service id. Reads memory only."""
    now = time.time() if now is None else now
    return {service_id: held for service_id in list(_KEPT) if (held := fresh(service_id, now=now)) is not None}


# ------------------------------------------------------------------------------------------------ where a list is read


def node_list_probe(protocol_id: str, catalog: Any = None) -> Mapping[str, Any] | None:
    """The engine definition's list probe of kind ``node`` for a protocol, or ``None`` when no definition has one."""
    if catalog is None:
        from gateway.engine_definitions import load_engine_catalog

        catalog = load_engine_catalog()
    for definition_id in catalog.ids():
        definition = catalog.definition(definition_id) or {}
        if (definition.get("record") or {}).get("protocol") != protocol_id:
            continue
        for probe in (definition.get("probes") or {}).get("lists") or ():
            if probe.get("offers") == "node" and probe.get("method", "GET") == "GET":
                return probe
    return None


def source_of(address: str, probe: Mapping[str, Any]) -> Source | None:
    """The URL and byte cap to read an engine's node list from, given its configured address and the definition's probe;
    ``None`` when the address or the path cannot be used."""
    from gateway.engine_check import join_url, origin_of

    base = origin_of(address) if probe.get("from") == "origin" else address.rstrip("/")
    path = str(probe.get("path") or "")
    if not base or not path.startswith("/"):
        return None
    return Source(join_url(base, path), int(probe.get("cap_bytes") or DEFAULT_CAP_BYTES))


def fetch_node_list(source: Source) -> Any:
    """Read one engine's node list and parse it. Blocking; raises on any failure (an answer that is not 200, one longer
    than the cap, a body that is not JSON, a send the egress rules refuse)."""
    import httpx

    from gateway import engine_egress

    with engine_egress.client(timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)) as client:
        with client.stream("GET", source.url, extensions=engine_egress.probe()) as response:
            if response.status_code != 200:
                raise ValueError(f"the node list answered HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > source.cap_bytes:
                    raise ValueError("the node list is longer than the cap")
    return json.loads(bytes(body))


def refresh_all(
    sources: Mapping[str, Source], *, fetch: Callable[[Source], Any] = fetch_node_list, now: float | None = None,
) -> dict[str, str | None]:
    """Read each engine's node list once and keep what it holds. Blocking: run it off the event loop.

    ``{service id: None | why not}``. A list that cannot be read keeps the engine's earlier list (which ages out at
    ``MAX_AGE_S``); an engine that is no longer in ``sources``, or that is now read from another URL, loses its list at
    once, so an edited address never keeps the old engine's answer."""
    for service_id in list(_KEPT):
        source = sources.get(service_id)
        if source is None or _KEPT[service_id].url != source.url:
            _KEPT.pop(service_id, None)
    report: dict[str, str | None] = {}
    for service_id, source in sources.items():
        try:
            body = fetch(source)
            inventory = from_node_list(body, fetched_at=time.time() if now is None else now, url=source.url)
            if inventory is None:
                raise ValueError("the answer is not a node list")
        except Exception as exc:  # noqa: BLE001 - any failed read keeps the earlier list, and the listing fails open
            report[service_id] = type(exc).__name__
            log.warning("engine node list for %s could not be read: %s", service_id, type(exc).__name__)
            continue
        _KEPT[service_id] = inventory
        report[service_id] = None
    return report


async def run_loop(
    stop: asyncio.Event, sources: Callable[[], Mapping[str, Source]], *,
    fetch: Callable[[Source], Any] = fetch_node_list, every_s: float = REFRESH_EVERY_S,
    first_after_s: float = FIRST_READ_AFTER_S,
) -> None:
    """Read the engines' node lists until ``stop`` is set: once after ``first_after_s``, then every ``every_s``.
    One read at a time, so a slow engine never has two reads open. ``sources`` is called each round, so an engine added
    or edited is picked up without a restart."""
    delay = first_after_s
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(lambda: refresh_all(sources(), fetch=fetch))
        except Exception as exc:  # noqa: BLE001 - a round that fails is tried again next round
            log.warning("engine node list round failed: %s", type(exc).__name__)
        delay = every_s


def sources_for(
    engines: Iterable[tuple[str, str, str]], *, catalog: Any = None,
) -> dict[str, Source]:
    """``{service id: Source}`` for engines given as ``(service id, protocol id, address)``. An engine whose protocol has
    no node-list probe in the definitions, or whose address cannot be used, is left out (its workflows are judged as
    before)."""
    out: dict[str, Source] = {}
    for service_id, protocol_id, address in engines:
        if not (service_id and protocol_id and address) or service_id in out:
            continue
        probe = node_list_probe(protocol_id, catalog)
        source = source_of(address, probe) if probe else None
        if source is not None:
            out[service_id] = source
    return out
