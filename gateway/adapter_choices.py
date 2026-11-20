"""Adapter pick-lists: the files a caller may choose for a workflow, as data the gateway computes.

A workflow binding says it takes adapters (``adapter_architecture`` and ``adapter_lora_folder`` beside a ``loras`` input
that maps through a ``chain``, and optionally a ``text_encoder`` input). A caller never types a file name. The choices are

- ``loras``: the files the engine lists for the chain's loader input, kept when the file's own header names the
  workflow's architecture (``registry/adapter_architectures.yml``) or the owner marked the file for that architecture;
- ``text_encoders``: the encoder the workflow ships with, then the files the owner marked for that architecture.

Where choices come from is the one function ``installed`` (the operator's files). Another source, such as a member's
uploads, joins by adding its files to the ``entries`` it builds: nothing else here reads where a file came from.

The module names no engine, model, node class or workflow: the loader class and its input come from the binding's chain
data, the header keys from the architecture table, and the reads are passed in (``list_files``, ``read_header``).
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LORAS_INPUT = "loras"
ENCODER_INPUT = "text_encoder"
SETTING_KEY = "adapter_marks"
MAX_AGE_S = 300.0          # a list read from the engine is used this long
HEADER_AGE_S = 6 * 3600.0  # a header read is used this long; a file's header does not change while it stays listed
HEADER_READS_AT_ONCE = 8
TABLE_PATH = Path(__file__).resolve().parents[1] / "registry" / "adapter_architectures.yml"


class ChoicesUnavailable(RuntimeError):
    """The engine could not list its files and no earlier list is held, so no choice can be offered."""


@dataclass(frozen=True)
class AdapterChoices:
    loras: tuple[str, ...]
    text_encoders: tuple[str, ...]


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
    """The files the owner marked for an architecture, by kind (``loras`` or ``text_encoders``). A setting that is not
    shaped as expected marks nothing."""
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
    list_files: Callable[[str, str, str], list[str]] = default_list_files,
    read_header: Callable[[str, str, str], Mapping[str, Any] | None] = default_read_header,
    table: Mapping[str, Mapping[str, Sequence[str]]] | None = None, now: float | None = None,
) -> AdapterChoices:
    """The choices for one binding from the operator's installed files. Reads the engine only when its list is older than
    ``MAX_AGE_S``; a file header is read once. When the engine cannot answer, the last good list stays in use; with none
    held, ``ChoicesUnavailable``. A header that cannot be read keeps its file out of the list, never in it."""
    now = time.time() if now is None else now
    key = (service_id, decl.node_type, decl.listing_input, decl.folder)
    held = _LISTS.get(key)
    if held is None or now - held[0] >= MAX_AGE_S:
        try:
            files = await asyncio.to_thread(list_files, origin, decl.node_type, decl.listing_input)
            _LISTS[key] = held = (now, files)
        except Exception as exc:  # noqa: BLE001 - any failed read keeps the last good list
            if held is None:
                raise ChoicesUnavailable("the engine could not list its adapter files") from exc
    files = held[1]
    headers = await _headers(service_id, origin, decl.folder, files, read_header, now)
    table = architecture_table() if table is None else table
    loras = compatible(files, headers, decl.architecture, table, marked(marks, decl.architecture, "loras"))
    encoders = list(dict.fromkeys([*decl.default_encoders, *marked(marks, decl.architecture, "text_encoders")]))
    return AdapterChoices(tuple(loras), tuple(encoders))


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
