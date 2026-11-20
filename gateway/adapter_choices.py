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

The module names no engine, model, node class or workflow: the loader class and its input come from the binding's chain
data, the header keys from the architecture table, and the reads are passed in (``list_files``, ``read_header``).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import yaml

LORAS_INPUT = "loras"
ENCODER_INPUT = "text_encoder"
SETTING_KEY = "adapter_marks"
MAX_AGE_S = 300.0          # a list read from the engine is used this long
HEADER_AGE_S = 6 * 3600.0  # a header read is used this long; a file's header does not change while it stays listed
HEADER_READS_AT_ONCE = 8
TABLE_PATH = Path(__file__).resolve().parents[1] / "registry" / "adapter_architectures.yml"

log = logging.getLogger(__name__)


# The owner's marks (``SETTING_KEY``). The gateway sets this at startup to read the stored setting, as it sets the choice
# store; unset, no file is marked.
MARKS: Callable[[], Any] = lambda: None


# Whether a listing may read the engine's files. The gateway sets this at startup, as it does the choice store: a
# process that never started serving (a test, a tool) lists no files and never reaches an engine from a listing.
LISTING_READS_ENGINE = False


class ChoicesUnavailable(RuntimeError):
    """The engine could not list its files and no earlier list is held, so no choice can be offered."""


class UploadUnavailable(ChoicesUnavailable):
    """A person's own upload is on offer but could not be made ready for the engine (its copy failed)."""


class Uploads(Protocol):
    """A person's own uploaded adapters. The gateway sets ``UPLOADS`` at startup; unset, nobody has any."""

    def offered(self, person_id: str, *, folder: str, architecture: str) -> Sequence[str]:
        """The names that person may choose for a workflow of ``architecture``: their own uploads whose header names it."""

    def engine_names(self, person_id: str, names: Sequence[str], *, folder: str) -> Mapping[str, str]:
        """The engine's name for each of those uploads, copied into the engine's folder now. ``LookupError`` when a name
        is not one that person is offered; any other error means the copy could not be made."""

    def is_copy(self, name: str) -> bool:
        """Whether an engine file name is one of the gateway's own copies. A copy is never an operator's file."""


UPLOADS: Uploads | None = None


@dataclass(frozen=True)
class AdapterChoices:
    loras: tuple[str, ...]
    text_encoders: tuple[str, ...]
    uploads: tuple[str, ...] = ()     # the part of ``loras`` that is the asking person's own uploads


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


def compatible(installed: Sequence[str], headers: Mapping[str, Any], architecture: str,
               table: Mapping[str, Mapping[str, Sequence[str]]], marked: Sequence[str]) -> list[str]:
    """The installed files that suit ``architecture``, in installed order: the owner's marks, and files whose header holds
    a value the table lists for it. A file with no readable header is offered only if marked."""
    wanted = table.get(architecture, {})
    chosen = set(marked)

    def names_it(header: Any) -> bool:
        return isinstance(header, Mapping) and any(
            isinstance(header.get(key), str) and header[key] in values for key, values in wanted.items())

    return [name for name in installed if name in chosen or names_it(headers.get(name))]


def overlay(contract: Mapping[str, Any], decl: Declaration, choices: AdapterChoices) -> dict[str, Any]:
    """A copy of the contract whose adapter inputs list their current choices. With no compatible file the list takes no
    items. The stored contract is never changed: choices are read live, so they belong to a request or a listing."""
    shown = copy.deepcopy(dict(contract))
    inputs = shown["inputs"]
    loras = inputs[LORAS_INPUT]
    if choices.loras:
        loras["items"]["properties"]["name"]["enum"] = list(choices.loras)
    else:
        loras["max_items"] = 0
    if decl.encoder_input is not None:
        inputs[decl.encoder_input]["enum"] = list(choices.text_encoders)
    return shown


# ------------------------------------------------------------------------------------------------ the operator's files

_LISTS: dict[tuple[str, str, str, str], tuple[float, list[str]]] = {}
_HEADERS: dict[tuple[str, str, str], tuple[float, Mapping[str, Any] | None]] = {}


def reset() -> None:
    _LISTS.clear()
    _HEADERS.clear()


def default_list_files(origin: str, node_type: str, input_name: str) -> list[str]:
    from gateway.engine_protocol.choice_store import choices_from_node_info
    from gateway.engine_protocol.node_info import fetch_fresh

    return choices_from_node_info(fetch_fresh(origin, node_type), input_name)


def default_read_header(origin: str, folder: str, name: str) -> Mapping[str, Any] | None:
    from gateway.engine_protocol.node_info import fetch_header

    return fetch_header(origin, folder, name)


async def installed(
    decl: Declaration, *, service_id: str, origin: str, marks: Any = None,
    list_files: Callable[[str, str, str], list[str]] | None = None,
    read_header: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
    table: Mapping[str, Mapping[str, Sequence[str]]] | None = None, now: float | None = None,
    person_id: str | None = None, uploads: Uploads | None = None,
) -> AdapterChoices:
    """The choices for one binding: the operator's installed files, then ``person_id``'s own uploads when there is one.
    Reads the engine only when its list is older than ``MAX_AGE_S``; a file header is read once. When the engine cannot
    answer, the last good list stays in use; with none held, ``ChoicesUnavailable``. A header that cannot be read keeps
    its file out of the list, never in it. The operator's list is the same for everyone and is cached; a person's
    uploads are added after it and never enter a cache that another person reads."""
    now = time.time() if now is None else now
    uploads = UPLOADS if uploads is None else uploads
    list_files, read_header = list_files or default_list_files, read_header or default_read_header
    key = (service_id, decl.node_type, decl.listing_input, decl.folder)
    held = _LISTS.get(key)
    if held is None or now - held[0] >= MAX_AGE_S:
        try:
            files = await asyncio.to_thread(list_files, origin, decl.node_type, decl.listing_input)
            _LISTS[key] = held = (now, files)
        except Exception as exc:  # noqa: BLE001 - any failed read keeps the last good list
            if held is None:
                raise ChoicesUnavailable("the engine could not list its adapter files") from exc
    # The gateway's own copies of uploads sit in the engine's folder, so the engine lists them. They are one person's
    # files: they are never offered as the operator's, whatever their header says.
    files = [name for name in held[1] if uploads is None or not uploads.is_copy(name)]
    headers = await _headers(service_id, origin, decl.folder, files, read_header, now)
    table = architecture_table() if table is None else table
    loras = compatible(files, headers, decl.architecture, table, marked(marks, decl.architecture, "loras"))
    own = await _own_uploads(uploads, person_id, decl)
    return AdapterChoices(tuple(loras) + tuple(name for name in own if name not in loras), decl.default_encoders,
                          tuple(own))


async def _own_uploads(uploads: Uploads | None, person_id: str | None, decl: Declaration) -> list[str]:
    if uploads is None or not person_id:
        return []
    try:
        return list(await asyncio.to_thread(uploads.offered, person_id, folder=decl.folder, architecture=decl.architecture))
    except Exception:  # noqa: BLE001 - a failure here costs the person their uploads on this list, never the operator's files
        log.exception("a person's adapter uploads could not be listed")
        return []


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
        async with gate:
            try:
                header = await asyncio.to_thread(read_header, origin, folder, name)
            except Exception:  # noqa: BLE001 - not offered this time, and not remembered as having no header
                return
        _HEADERS[(service_id, folder, name)] = (now, header)
        found[name] = header

    await asyncio.gather(*(read(name) for name in unknown))
    return found


class Current(NamedTuple):
    decl: Declaration
    contract: dict[str, Any]       # the contract with the choices put in
    supplied: Mapping[str, Any]    # the adapter inputs the request supplied, each upload named as the engine knows it


async def current(contract: Mapping[str, Any], binding_key: str, *, service_id: str, origin: str,
                  person_id: str | None = None, supplied: Mapping[str, Any] | None = None) -> Current | None:
    """The contract as a request sees it now: the declaration the binding makes, the contract with the choices put in,
    and the request's adapter inputs. None when the binding takes no adapters. Raises ``ChoicesUnavailable`` when the
    engine cannot list its files.

    A request names a person's upload by its asset id. Here each upload that ``person_id`` is offered and the request
    names is copied into the engine's folder, and the request and the contract's list name it as the engine does. A
    name that is an asset id the person is not offered (another person's, deleted, unknown) is left alone, so the
    contract refuses it exactly as it refuses any name that is not on the list."""
    decl = declaration(contract, binding_key)
    if decl is None:
        return None
    choices = await installed(decl, service_id=service_id, origin=origin, marks=MARKS(), person_id=person_id)
    supplied = {} if supplied is None else supplied
    copies = await _copies(decl, person_id, supplied, choices)
    if copies:
        choices = replace(choices, loras=tuple(copies.get(name, name) for name in choices.loras))
        supplied = {**supplied, LORAS_INPUT: [
            {**item, "name": copies.get(item.get("name"), item.get("name"))} if isinstance(item, Mapping) else item
            for item in supplied[LORAS_INPUT]]}
    return Current(decl, overlay(contract, decl, choices), supplied)


async def _copies(decl: Declaration, person_id: str | None, supplied: Mapping[str, Any], choices: AdapterChoices
                  ) -> dict[str, str]:
    items = supplied.get(LORAS_INPUT)
    if UPLOADS is None or not person_id or not choices.uploads or not isinstance(items, list):
        return {}
    named = [item["name"] for item in items
             if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"] in choices.uploads]
    if not named:
        return {}
    try:
        return dict(await asyncio.to_thread(UPLOADS.engine_names, person_id, list(dict.fromkeys(named)), folder=decl.folder))
    except LookupError:       # deleted since the list was read: the name stays, and the contract refuses it
        return {}
    except Exception as exc:  # noqa: BLE001 - the file could not be copied
        log.exception("an adapter upload could not be copied for the engine")
        raise UploadUnavailable("an uploaded adapter could not be made ready for the engine") from exc


LISTING_WAIT_S = 5.0     # a listing waits this long for the engine, then shows no files for the workflows not yet read


async def offered(records: Sequence[Any], binding_key: str, *, service_id: str, origin: str,
                  person_id: str | None = None) -> dict[str, tuple[str, ...]]:
    """The adapter files each record offers now, by record id, for a listing. A record that takes no adapters is left
    out; so is one whose files could not be read in time (the listing then shows none, and the next one tries again).
    ``person_id``'s own uploads are on the list for that person only."""
    if not LISTING_READS_ENGINE:
        return {}
    async def one(record: Any) -> tuple[str, tuple[str, ...]] | None:
        decl = declaration(record.contract, binding_key)
        if decl is None:
            return None
        try:
            choices = await asyncio.wait_for(
                installed(decl, service_id=service_id, origin=origin, marks=MARKS(), person_id=person_id), LISTING_WAIT_S)
        except (ChoicesUnavailable, asyncio.TimeoutError):
            return None
        return record.id, choices.loras

    return dict(found for found in await asyncio.gather(*(one(record) for record in records)) if found)


def names_adapters(supplied: Mapping[str, Any]) -> bool:
    """Whether a request supplies an adapter input. A request that does not needs no list of files."""
    return LORAS_INPUT in supplied or ENCODER_INPUT in supplied


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
