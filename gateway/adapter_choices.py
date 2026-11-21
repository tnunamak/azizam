"""Adapter pick-lists: the files a caller may choose for a workflow, as data the gateway computes.

A workflow binding says it takes adapters (``adapter_architecture`` and ``adapter_lora_folder`` beside a ``loras`` input
that maps through a ``chain``, and optionally a ``text_encoder`` input). A caller never types a file name. The choices are

- ``loras``: the files the engine lists for the chain's loader input, kept when the file's own header names the
  workflow's architecture (``registry/adapter_architectures.yml``) or the owner marked the file for that architecture;
- ``text_encoders``: the encoders the workflow's manifest lists (its ``text_encoder`` enum). The job runs against that
  same contract, so an encoder the manifest does not list cannot run; a new encoder is a manifest edit.

Where choices come from is the one function ``installed`` (the operator's files) plus, for a signed-in person, that
person's own uploads (``UPLOADS``). An upload joins after the same header test, only for the person who owns it, and the
engine learns its file only when a request uses it (``current`` swaps the upload's id for the engine's copy).

Known limit: the engine lists names only, so a header is cached by (service, folder, name) and not by size or mtime; a
file replaced under the same name keeps its old header for ``HEADER_AGE_S``.

The module names no engine, model, node class or workflow: the loader class and its input come from the binding's chain
data, the header keys from the architecture table, and the reads are passed in (``list_files``, ``read_header``).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import yaml

MEMBER_UPLOAD_SUBFOLDER = "azizam-uploads"
LORAS_INPUT = "loras"
ENCODER_INPUT = "text_encoder"
SETTING_KEY = "adapter_marks"
MAX_AGE_S = 300.0          # a list read from the engine is used this long
HEADER_AGE_S = 6 * 3600.0  # a header read is used this long; a file's header does not change while it stays listed
STALE_LIST_S = 10.0        # a requested name missing from a list older than this makes one re-read before it is refused
FAILED_READ_BACKOFF_S = 30.0  # after a failed list read the engine is not asked again for this long
HEADER_READS_AT_ONCE = 8
TABLE_PATH = Path(__file__).resolve().parents[1] / "registry" / "adapter_architectures.yml"

log = logging.getLogger(__name__)


# The owner's marks (``SETTING_KEY``). The gateway sets this at startup to read the stored setting, as it sets the choice
# store; unset, no file is marked.
MARKS: Callable[[], Any] = lambda: None


async def _marks() -> Any:
    """The owner's marks, read off the event loop: the stored setting is a database read."""
    return await asyncio.to_thread(MARKS)



class ChoicesUnavailable(RuntimeError):
    """The engine could not list its files and no earlier list is held, so no choice can be offered."""


class UploadUnavailable(ChoicesUnavailable):
    """A person's own upload is on offer but could not be made ready for the engine (its copy failed)."""


class Uploads(Protocol):
    """A person's own uploaded adapters. The gateway sets ``UPLOADS`` at startup; unset, nobody has any."""

    def offered(self, person_id: str, *, folder: str, architecture: str) -> Sequence[str]:
        """The names that person may choose for a workflow of ``architecture``: their own uploads whose header names it."""

    def labels(self, person_id: str, *, folder: str, architecture: str) -> Mapping[str, str]:
        """The label that person gave each of those uploads; an upload with no label is left out."""

    def engine_names(self, person_id: str, names: Sequence[str], *, folder: str, hold: str | None = None,
                     hold_ttl_s: float = ...) -> Mapping[str, str]:
        """The engine's name for each of those uploads, copied into the engine's folder now and held for ``hold`` until
        ``release(hold)``. ``LookupError`` (its first argument the name) when a name is not one that person is offered or
        the upload is gone; any other error means the copy could not be made."""

    def hold_copies(self, key: str, names: Sequence[Any], *, ttl_s: float = ...) -> None:
        """Hold the copies of these engine names for ``key``."""

    def release(self, key: str | None) -> None:
        """End the holds of ``key``."""

    def is_copy(self, name: str) -> bool:
        """Whether an engine file name is one of the gateway's own copies. A copy is never an operator's file."""


UPLOADS: Uploads | None = None
HOLD_TTL_S = 1800.0        # a copy chosen for a request is held this long when the request cannot say it has ended


def hold_run(key: str, supplied: Mapping[str, Any]) -> None:
    """Hold the gateway's copies a run's adapter inputs name, for ``key`` (the job id), until ``release(key)``. The run
    core calls it when a job is made and again when a recovered job starts."""
    items = supplied.get(LORAS_INPUT) if isinstance(supplied, Mapping) else None
    if UPLOADS is not None and isinstance(items, list):
        UPLOADS.hold_copies(key, [item.get("name") for item in items if isinstance(item, Mapping)], ttl_s=RUN_HOLD_TTL_S)


def release(key: str | None) -> None:
    """End a hold taken by ``current`` (``Current.hold``) or ``hold_run``. A key that holds nothing is not an error."""
    if UPLOADS is not None and key is not None:
        UPLOADS.release(key)


RUN_HOLD_TTL_S = 6 * 3600.0   # a run's copies are held until it ends, or this long if it never says


@dataclass(frozen=True)
class AdapterChoices:
    loras: tuple[str, ...]
    text_encoders: tuple[str, ...]
    uploads: tuple[str, ...] = ()     # the part of ``loras`` that is the asking person's own uploads
    labels: Mapping[str, str] = field(default_factory=dict)   # the labels that person gave those uploads, by name


@dataclass(frozen=True)
class Declaration:
    architecture: str
    folder: str
    node_type: str          # the class the chain adds: the engine lists its files under this class
    listing_input: str      # the input of that class that takes a file name
    encoder_input: str | None
    default_encoders: tuple[str, ...]


@lru_cache(maxsize=1)
def architecture_table() -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    raw = yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8")) or {}
    return {arch: {key: tuple(values) for key, values in keys.items()} for arch, keys in raw.items()}


def declaration(contract: Mapping[str, Any], binding_key: str) -> Declaration | None:
    """What a binding declares about adapters, or None when it takes none: a binding needs an architecture, a folder and a
    ``loras`` input that maps through a chain whose fields place a ``name``."""
    binding = (contract.get("bindings") or {}).get(binding_key)
    if not isinstance(binding, Mapping):
        return None
    architecture, folder = binding.get("adapter_architecture"), binding.get("adapter_lora_folder")
    mappings = (binding.get("map") or {}).get(LORAS_INPUT)
    chains = [m["chain"] for m in mappings if isinstance(m, Mapping) and isinstance(m.get("chain"), Mapping)] \
        if isinstance(mappings, list) else []
    if not (isinstance(architecture, str) and isinstance(folder, str) and chains):
        return None
    chain = chains[0]
    listing_input = (chain.get("fields") or {}).get("name")
    if not (isinstance(listing_input, str) and isinstance(chain.get("node_type"), str)):
        return None
    encoder = (contract.get("inputs") or {}).get(ENCODER_INPUT)
    has_encoder = isinstance(encoder, Mapping) and ENCODER_INPUT in (binding.get("map") or {})
    defaults = tuple(encoder.get("enum") or ()) if has_encoder else ()
    return Declaration(architecture, folder, chain["node_type"], listing_input, ENCODER_INPUT if has_encoder else None, defaults)


def marked(setting: Any, architecture: str, kind: str) -> list[str]:
    """The files the owner marked for an architecture, by kind (``loras``). A setting that is not shaped as expected
    marks nothing."""
    marks = setting.get("marks") if isinstance(setting, Mapping) else None
    entry = marks.get(architecture) if isinstance(marks, Mapping) else None
    names = entry.get(kind) if isinstance(entry, Mapping) else None
    return [name for name in names if isinstance(name, str)] if isinstance(names, list) else []


def _plain(value: str) -> str:
    """A header value as compared: case folded, with every character that is not a letter or digit dropped, so "Krea-2"
    and " KREA2 " name the same base as "krea2"."""
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def compatible(installed: Sequence[str], headers: Mapping[str, Any], architecture: str,
               table: Mapping[str, Mapping[str, Sequence[str]]], marked: Sequence[str]) -> list[str]:
    """The installed files that suit ``architecture``, in installed order: the owner's marks, and files whose header holds
    a value the table lists for it. A file with no readable header is offered only if marked."""
    wanted = {key: {_plain(value) for value in values} for key, values in table.get(architecture, {}).items()}
    chosen = set(marked)

    def names_it(header: Any) -> bool:
        return isinstance(header, Mapping) and any(
            isinstance(header.get(key), str) and _plain(header[key]) in values for key, values in wanted.items())

    return [name for name in installed if name in chosen or names_it(headers.get(name))]


def overlay(contract: Mapping[str, Any], decl: Declaration, choices: AdapterChoices) -> dict[str, Any]:
    """A copy of the contract whose adapter inputs list their current choices. With no compatible file the list takes no
    items. The stored contract is never changed: choices are read live, so they belong to a request or a listing."""
    shown = copy.deepcopy(dict(contract))
    inputs = shown["inputs"]
    loras = inputs[LORAS_INPUT]
    if choices.loras:
        name = loras["items"]["properties"]["name"]
        name["enum"] = list(choices.loras)
        named = {key: label for key, label in choices.labels.items() if key in choices.loras}
        if named:
            label_key = lambda label: unicodedata.normalize("NFC", label.strip())
            operator_names = {label_key(key) for key in set(choices.loras) - named.keys()}
            counts: dict[str, int] = {}
            for label in named.values():
                normalized = label_key(label)
                counts[normalized] = counts.get(normalized, 0) + 1
            occupied = operator_names | counts.keys()
            for key, label in named.items():
                if counts[label_key(label)] == 1 and label_key(label) not in operator_names:
                    continue
                for width in range(min(4, len(key)), len(key) + 1):
                    candidate = f"{label} (…{key[-width:]})"
                    if label_key(candidate) not in occupied:
                        break
                else:
                    # Even a member-typed label that matches a whole suffixed ID stays unique.
                    number = 2
                    while label_key(candidate := f"{label} (…{key}, {number})") in occupied:
                        number += 1
                occupied.add(label_key(candidate))
                named[key] = candidate
            name["choice_labels"] = named
    else:
        loras["max_items"] = 0
    if decl.encoder_input is not None:
        inputs[decl.encoder_input]["enum"] = list(choices.text_encoders)
    return shown


# ------------------------------------------------------------------------------------------------ the operator's files

_LISTS: dict[tuple[str, str, str, str], tuple[float, list[str]]] = {}
_HEADERS: dict[tuple[str, str, str], tuple[float, Mapping[str, Any] | None]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_GENERATION = 0
_FLIGHTS: dict[tuple[str, ...], asyncio.Task[Any]] = {}
_FAILED: dict[tuple[str, str, str, str], float] = {}   # when a list read last failed, by the same key as _LISTS


def reset() -> None:
    global _CACHE_GENERATION
    with _CACHE_LOCK:
        _CACHE_GENERATION += 1
        _LISTS.clear()
        _HEADERS.clear()
        _FAILED.clear()
        _FLIGHTS.clear()


def _shared_read(key: tuple[str, ...], read, *, gate: asyncio.Semaphore | None = None) -> asyncio.Task[Any]:
    task = _FLIGHTS.get(key)
    if task is not None:
        return task
    generation = _CACHE_GENERATION

    async def run():
        if gate is None:
            return await asyncio.to_thread(read, generation)
        async with gate:
            return await asyncio.to_thread(read, generation)

    task = asyncio.create_task(run())
    _FLIGHTS[key] = task

    def done(finished):
        if _FLIGHTS.get(key) is finished:
            _FLIGHTS.pop(key)
        if not finished.cancelled():
            finished.exception()  # A timed-out last waiter may no longer observe the error.

    task.add_done_callback(done)
    return task


def default_list_files(origin: str, node_type: str, input_name: str) -> list[str]:
    from gateway.engine_protocol.choice_store import choices_from_node_info
    from gateway.engine_protocol.node_info import fetch_fresh

    return choices_from_node_info(fetch_fresh(origin, node_type), input_name)


def default_read_header(origin: str, folder: str, name: str) -> Mapping[str, Any] | None:
    from gateway.engine_protocol.node_info import fetch_header

    return fetch_header(origin, folder, name)


def is_member_upload_copy(name: object) -> bool:
    return isinstance(name, str) and name.replace("\\", "/").lower().startswith(MEMBER_UPLOAD_SUBFOLDER + "/")


async def installed(
    decl: Declaration, *, service_id: str, origin: str, marks: Any = None,
    list_files: Callable[[str, str, str], list[str]] | None = None,
    read_header: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
    table: Mapping[str, Mapping[str, Sequence[str]]] | None = None, now: float | None = None,
    wanted: Sequence[str] = (), person_id: str | None = None, uploads: Uploads | None = None,
) -> AdapterChoices:
    """The choices for one binding: the operator's installed files, then ``person_id``'s own uploads when there is one.
    Reads the engine only when its list is older than ``MAX_AGE_S``, or older than ``STALE_LIST_S`` when a ``wanted`` name
    is neither in it nor one of the person's uploads (a file added since); a file header is read once. When the engine
    cannot answer, the last good list stays in use and the engine is left alone for ``FAILED_READ_BACKOFF_S``; with no
    list held, ``ChoicesUnavailable``. A header that cannot be read keeps its file out of the list, never in it. The
    operator's list is the same for everyone and is cached; a person's uploads are added after it and never enter a
    cache that another person reads."""
    now = time.time() if now is None else now
    uploads = UPLOADS if uploads is None else uploads
    own, labels = await _own_uploads(uploads, person_id, decl)
    wanted = [name for name in wanted if name not in own]   # an upload is not in the engine's list, and is not a stale one
    list_files, read_header = list_files or default_list_files, read_header or default_read_header
    key = (service_id, decl.node_type, decl.listing_input, decl.folder)
    held = _LISTS.get(key)
    age = None if held is None else now - held[0]
    due = held is None or age >= MAX_AGE_S or (age >= STALE_LIST_S and any(name not in held[1] for name in wanted))
    failed_at = _FAILED.get(key)
    if due and failed_at is not None and now - failed_at < FAILED_READ_BACKOFF_S:
        if held is None:
            raise ChoicesUnavailable("the engine could not list its adapter files")
    elif due:
        def read_list(generation: int) -> tuple[float, list[str]]:
            try:
                files = list_files(origin, decl.node_type, decl.listing_input)
            except Exception:
                with _CACHE_LOCK:
                    if generation == _CACHE_GENERATION and now >= _FAILED.get(key, float("-inf")):
                        current = _LISTS.get(key)
                        if current is None or current[0] <= now:
                            _FAILED[key] = now
                raise
            with _CACHE_LOCK:
                current = _LISTS.get(key)
                if generation == _CACHE_GENERATION and (current is None or current[0] <= now):
                    _LISTS[key] = (now, files)
                    if _FAILED.get(key, float("-inf")) <= now:
                        _FAILED.pop(key, None)
            return now, files
        try:
            read = await asyncio.shield(_shared_read(("list", *key), read_list))
            held = _LISTS.get(key, read)
        except Exception as exc:  # noqa: BLE001 - any failed read keeps the last good list
            held = _LISTS.get(key, held)
            if held is None:
                raise ChoicesUnavailable("the engine could not list its adapter files") from exc
    # The gateway's own copies of uploads sit in the engine's folder, so the engine lists them. They are one person's
    # files: they are never offered as the operator's, whatever their header says.
    files = [
        name for name in held[1]
        if not is_member_upload_copy(name) and (uploads is None or not uploads.is_copy(name))
    ]
    headers = await _headers(service_id, origin, decl.folder, files, read_header, now)
    table = architecture_table() if table is None else table
    loras = compatible(files, headers, decl.architecture, table, marked(marks, decl.architecture, "loras"))
    return AdapterChoices(tuple(loras) + tuple(name for name in own if name not in loras), decl.default_encoders,
                          tuple(own), labels)


async def _own_uploads(uploads: Uploads | None, person_id: str | None, decl: Declaration
                       ) -> tuple[list[str], dict[str, str]]:
    if uploads is None or not person_id:
        return [], {}

    def read() -> tuple[list[str], dict[str, str]]:
        names = list(uploads.offered(person_id, folder=decl.folder, architecture=decl.architecture))
        labels = uploads.labels(person_id, folder=decl.folder, architecture=decl.architecture)
        return names, {name: label for name, label in labels.items() if name in names and label}

    try:
        return await asyncio.to_thread(read)
    except Exception:  # noqa: BLE001 - a failure here costs the person their uploads on this list, never the operator's files
        log.exception("a person's adapter uploads could not be listed")
        return [], {}


async def _headers(service_id: str, origin: str, folder: str, files: Sequence[str],
                   read_header: Callable[[str, str, str], Mapping[str, Any] | None], now: float) -> dict[str, Any]:
    found: dict[str, Any] = {}
    unknown = []
    for name in files:
        held = _HEADERS.get((service_id, folder, name))
        if held is not None and now - held[0] < HEADER_AGE_S:
            found[name] = held[1]
        else:
            unknown.append(name)
    gate = asyncio.Semaphore(HEADER_READS_AT_ONCE)

    async def read(name: str) -> None:
        key = (service_id, folder, name)

        def fetch(generation: int) -> Mapping[str, Any] | None:
            header = read_header(origin, folder, name)
            with _CACHE_LOCK:
                held = _HEADERS.get(key)
                if generation == _CACHE_GENERATION and (held is None or held[0] <= now):
                    _HEADERS[key] = (now, header)
            return header

        try:
            found[name] = await asyncio.shield(_shared_read(("header", *key), fetch, gate=gate))
        except Exception:  # noqa: BLE001 - not offered this time, and not remembered as having no header
            return

    await asyncio.gather(*(read(name) for name in unknown))
    return found


def wanted_names(supplied: Mapping[str, Any]) -> list[str]:
    """The file names a request picks, for ``installed``'s check that its list is not stale."""
    items = supplied.get(LORAS_INPUT)
    return [item["name"] for item in items if isinstance(item, Mapping) and isinstance(item.get("name"), str)] \
        if isinstance(items, list) else []


class Current(NamedTuple):
    decl: Declaration
    contract: dict[str, Any]       # the contract with the choices put in
    supplied: Mapping[str, Any]    # the adapter inputs the request supplied, each upload named as the engine knows it
    hold: str | None = None        # the key the copies are held under until the caller calls ``release(hold)``


async def current(contract: Mapping[str, Any], binding_key: str, *, service_id: str, origin: str,
                  person_id: str | None = None, supplied: Mapping[str, Any] | None = None,
                  hold_ttl_s: float = HOLD_TTL_S) -> Current | None:
    """The contract as a request sees it now: the declaration the binding makes, the contract with the choices put in,
    and the request's adapter inputs. None when the binding takes no adapters. Raises ``ChoicesUnavailable`` when the
    engine cannot list its files.

    A request names a person's upload by its asset id. Here each upload that ``person_id`` is offered and the request
    names is copied into the engine's folder, and the request and the contract's list name it as the engine does. A
    name that is an asset id the person is not offered (another person's, deleted, unknown) is left alone, so the
    contract refuses it exactly as it refuses any name that is not on the list.

    The copies are held under ``Current.hold`` for ``hold_ttl_s`` or until the caller releases it: the caller holds them
    for the life of the run that names them (``hold_run``), then calls ``release(hold)``."""
    decl = declaration(contract, binding_key)
    if decl is None:
        return None
    supplied = {} if supplied is None else supplied
    choices = await installed(decl, service_id=service_id, origin=origin, marks=await _marks(), person_id=person_id,
                              wanted=wanted_names(supplied))
    hold = f"adapters-{uuid.uuid4().hex}"
    copies, choices = await _copies(decl, person_id, supplied, choices, hold=hold, hold_ttl_s=hold_ttl_s)
    if copies:
        choices = replace(choices, loras=tuple(copies.get(name, name) for name in choices.loras))
        supplied = {**supplied, LORAS_INPUT: [
            {**item, "name": _engine_name(item, copies)} if isinstance(item, Mapping) else item
            for item in supplied[LORAS_INPUT]]}
    return Current(decl, overlay(contract, decl, choices), supplied, hold if copies else None)


def _engine_name(item: Mapping[str, Any], copies: Mapping[str, str]) -> Any:
    name = item.get("name")
    return copies.get(name, name) if isinstance(name, str) else name      # a name that is not text stays, and the contract refuses it


async def _copies(decl: Declaration, person_id: str | None, supplied: Mapping[str, Any], choices: AdapterChoices, *,
                  hold: str, hold_ttl_s: float) -> tuple[dict[str, str], AdapterChoices]:
    """The engine's name for each upload the request names, and the choices without any upload that turned out to be gone
    (deleted since the list was read): such a name is then refused as any name that is not offered."""
    items = supplied.get(LORAS_INPUT)
    if UPLOADS is None or not person_id or not choices.uploads or not isinstance(items, list):
        return {}, choices
    named = list(dict.fromkeys(item["name"] for item in items
                               if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"] in choices.uploads))
    while named:
        try:
            made = dict(await asyncio.to_thread(UPLOADS.engine_names, person_id, list(named), folder=decl.folder,
                                                hold=hold, hold_ttl_s=hold_ttl_s))
            return made, choices
        except LookupError as gone:
            lost = gone.args[0] if gone.args and gone.args[0] in named else named[0]
            named.remove(lost)
            choices = replace(choices, loras=tuple(name for name in choices.loras if name != lost),
                              uploads=tuple(name for name in choices.uploads if name != lost))
        except Exception as exc:  # noqa: BLE001 - the file could not be copied
            log.exception("an adapter upload could not be copied for the engine")
            raise UploadUnavailable("an uploaded adapter could not be made ready for the engine") from exc
    return {}, choices


LISTING_WAIT_S = 5.0     # a listing waits this long for the engine, then shows no files for the workflows not yet read


async def offered(
    records: Sequence[Any], binding_key: str, *, service_id: str, origin: str,
    list_files: Callable[[str, str, str], list[str]] | None = None,
    read_header: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
    person_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The adapter inputs each record offers now, by record id, for a listing: the manifest's ``loras`` (and encoder)
    input with this moment's choices put in by ``overlay``, the same shape a request is checked against. A record that
    takes no adapters is left out; so is one whose files could not be read in time (the listing then shows none, and the
    next one tries again; reads that finish after the wait are kept). The caller passes the readers: with no
    ``list_files`` nothing is read. ``person_id``'s own uploads, and the labels they gave them, are on the list for that
    person only."""
    if list_files is None:
        return {}
    marks = await _marks()      # one read for the listing, not one per workflow

    async def one(record: Any) -> tuple[str, dict[str, Any]] | None:
        decl = declaration(record.contract, binding_key)
        if decl is None:
            return None
        try:
            choices = await asyncio.wait_for(
                installed(decl, service_id=service_id, origin=origin, marks=marks, list_files=list_files,
                          read_header=read_header, person_id=person_id),
                LISTING_WAIT_S)
        except (ChoicesUnavailable, asyncio.TimeoutError):
            return None
        shown = overlay(record.contract, decl, choices)["inputs"]
        return record.id, {name: shown[name] for name in (LORAS_INPUT, decl.encoder_input) if name}

    return dict(found for found in await asyncio.gather(*(one(record) for record in records)) if found)


def needs_file_list(supplied: Mapping[str, Any]) -> bool:
    """Whether a request names adapter files, so its names must be checked against the engine's list. An empty list names
    none, and an encoder is checked against the manifest's own list, so neither reads the engine."""
    value = supplied.get(LORAS_INPUT)
    return value is not None and value != []


# ------------------------------------------------------------------------------------------------------------ receipts


def receipt_facts(decl: Declaration | None, supplied: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a run used, for its receipt: each adapter by file name and weight, and the encoder. None when none was chosen."""
    if decl is None:
        return None
    declared = ((contract.get("inputs") or {}).get(LORAS_INPUT, {}).get("items") or {}).get("properties") or {}
    weight_default = (declared.get("weight") or {}).get("default")
    items = supplied.get(LORAS_INPUT)
    facts: dict[str, Any] = {}
    if isinstance(items, list) and items:
        facts["loras"] = [{"name": item["name"], "weight": item.get("weight", weight_default)} for item in items]
    encoder = supplied.get(ENCODER_INPUT) if decl.encoder_input else None
    if isinstance(encoder, str):
        facts["text_encoder"] = encoder
    return facts or None
