"""The one-time conversion of a workflow document from schema version 1 to 2 (CODE_TO_DATA_S3, D3).

This module is a tool, not a reader. The loader refuses schema 1 (``schema_version_unsupported``) and never converts: the
bundled files were rewritten with this converter (S3.5a), and the stored owner records are rewritten by the migration
(S3.5b). A package author or an owner runs it through ``scripts/workflow_contract.py convert``. It may name the keys of the
old shape (``trusted``, ``seed``, ``transport``), which the permanent contract module (``workflow_contract``) must not.

``convert_1_to_2`` returns a new document and the list of edits it made. A capability backfill reads its versioned rules;
it never writes files. It does not decide whether the result is valid; ``check_contract`` does. A document it cannot improve passes through unchanged, so the check shows what is wrong.
``v1_policy_facts`` and ``v1_provenance`` read what a schema 1 document said beside its contract, for the one who writes the
policy file or the stored record's facts; the converter itself drops those keys.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway.manifests import MEDIA_INPUT_TYPES, ContractError
from gateway.workflow_contract import Report, check_contract

# What the routes passed to the media resolver before inputs declared their own accepted types.
DEFAULT_ASSET_ACCEPTS = ["image/*"]
# What the old mesh-only picture type accepted.
OLD_PICTURE_ACCEPTS = ["image/png", "image/jpeg", "image/webp"]
SEED_GENERATOR = {"generator": "random_integer", "minimum": 0, "maximum": 4294967295}
BACKFILL_NOTE = {"capability": "intent-prefix rule, schema 1 to 2"}
BACKFILL_KEY = "azizam.dev/backfill"
POLICY_KEYS = ("exposure", "trust_tier", "resource_tier", "tags")
SCHEMA1_CAPABILITY_PREFIXES_PATH = Path(__file__).resolve().parent.parent / "registry" / "schema1_capability_prefixes.yml"


def v1_policy_facts(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The policy facts a schema 1 document states beside its contract: only those it states, as it states them. A document
    that states none gives ``{}``; nothing is filled in (a record with no facts needs configuration)."""
    found: dict[str, Any] = {}
    for key in POLICY_KEYS:
        value = manifest.get(key)
        if key == "tags":
            if isinstance(value, list):
                found[key] = list(dict.fromkeys(item for item in value if isinstance(item, str) and item))
        elif isinstance(value, str) and value:
            found[key] = value
    return found


def v1_provenance(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What a schema 1 document said of its own review and origin (``trusted``, ``source``). Provenance belongs to the record;
    the one who stores the converted document decides it from where the record came from, and this is only what was said."""
    return {"trusted": bool(manifest.get("trusted", False)), "source": manifest.get("source")}


@dataclass(frozen=True)
class ConversionNote:
    """One edit: where (``path``, in the new document unless the operation is ``remove``), what (``operation``) and why."""

    path: tuple[str | int, ...]
    operation: str
    reason: str


def convert_1_to_2(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[ConversionNote]]:
    """The schema-2 form of a schema-1 document, and the edits made. Any other document is returned as a copy, unedited."""
    out = copy.deepcopy(dict(doc))
    notes: list[ConversionNote] = []
    if out.get("schema_version") != 1:
        return out, notes

    def note(path: tuple[str | int, ...], operation: str, reason: str) -> None:
        notes.append(ConversionNote(path, operation, reason))

    out["schema_version"] = 2
    note(("schema_version",), "set", "the document dialect is 2")
    for key in ("trusted", "source"):
        if key in out:
            del out[key]
            note((key,), "remove", "authority belongs to the registry record, not the document")
    _backfill_capability(out, note)

    inputs = out.get("inputs")
    if isinstance(inputs, dict):
        for name, spec in inputs.items():
            if isinstance(spec, dict):
                _convert_input(("inputs", name), spec, note)
    outputs = out.get("outputs")
    if isinstance(outputs, dict):
        for name, spec in outputs.items():
            if isinstance(spec, dict) and spec.pop("modality", None) is not None:
                note(("outputs", name, "modality"), "remove", "the media type says what an output is")
    if isinstance(out.get("bindings"), dict):
        out["bindings"], flattened = _convert_bindings(out, note)
        _generate_seeds(out, flattened, note)
    return out, notes


def _load_capability_prefixes() -> tuple[list[tuple[str, str]], str]:
    """Read and validate the complete ordered backfill table, without caching it."""
    path = SCHEMA1_CAPABILITY_PREFIXES_PATH

    def invalid(reason: str) -> ValueError:
        return ValueError(f"{path}: {reason}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise invalid(f"cannot read capability prefixes: {exc}") from exc
    if not isinstance(document, dict):
        raise invalid("capability prefixes must be a mapping")
    unknown = set(document) - {"schema_version", "prefixes", "default"}
    if unknown:
        raise invalid(f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise invalid("schema_version must be 1")
    default = document.get("default")
    if not isinstance(default, str) or not default.strip() or default != default.strip():
        raise invalid("default must be a nonempty string without surrounding spaces")
    if any(control in default for control in "\t\r\n"):
        raise invalid("default must not contain tab, CR or LF")
    rows = document.get("prefixes")
    if not isinstance(rows, list):
        raise invalid("prefixes must be an ordered list")
    prefixes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise invalid(f"prefixes[{index}] must be a mapping")
        unknown = set(row) - {"prefix", "capability"}
        if unknown:
            raise invalid(f"prefixes[{index}] has unknown keys: {', '.join(sorted(map(str, unknown)))}")
        prefix, capability = row.get("prefix"), row.get("capability")
        if not isinstance(prefix, str) or not prefix.strip() or prefix != prefix.strip():
            raise invalid(f"prefixes[{index}].prefix must be a nonempty string without surrounding spaces")
        if any(control in prefix for control in "\t\r\n"):
            raise invalid(f"prefixes[{index}].prefix must not contain tab, CR or LF")
        if prefix in seen:
            raise invalid(f"prefixes[{index}] has duplicate prefix: {prefix!r}")
        if not isinstance(capability, str) or not capability.strip() or capability != capability.strip():
            raise invalid(f"prefixes[{index}].capability must be a nonempty string without surrounding spaces")
        if any(control in capability for control in "\t\r\n"):
            raise invalid(f"prefixes[{index}].capability must not contain tab, CR or LF")
        seen.add(prefix)
        prefixes.append((prefix, capability))
    return prefixes, default


def _infer_capability(intent: str) -> str:
    """The catalog's backfill rule, used only by this one-time conversion."""
    prefixes, default = _load_capability_prefixes()
    for prefix, capability in prefixes:
        if intent.startswith(prefix):
            return capability
    return default


def _insert_after(mapping: dict[str, Any], after: str, key: str, value: Any) -> None:
    """Put ``key`` right after ``after`` (at the end when ``after`` is absent), so a converted file reads in a fixed order."""
    items = list(mapping.items())
    mapping.clear()
    placed = False
    for name, held in items:
        mapping[name] = held
        if name == after:
            mapping[key] = value
            placed = True
    if not placed:
        mapping[key] = value


def _backfill_capability(out: dict[str, Any], note: Any) -> None:
    intent = out.get("intent")
    if "capability" in out or not isinstance(intent, str):
        return
    _insert_after(out, "intent", "capability", _infer_capability(intent))
    annotations = out.get("annotations")
    if annotations is None:
        _insert_after(out, "capability", "annotations", {BACKFILL_KEY: dict(BACKFILL_NOTE)})
    elif isinstance(annotations, dict):
        annotations[BACKFILL_KEY] = dict(BACKFILL_NOTE)
    note(("capability",), "backfill", "inferred once from the intent prefix, as the catalog did")


def _convert_input(at: tuple[str, ...], spec: dict[str, Any], note: Any) -> None:
    if spec.pop("modality", None) is not None:
        note(at + ("modality",), "remove", "an asset input lists the media types it accepts")
    kind = spec.get("type")
    if isinstance(kind, str) and kind in MEDIA_INPUT_TYPES - {"asset"}:  # the mesh-only picture type of schema 1 (a list is no type)
        spec["type"] = "asset"
        _insert_after(spec, "type", "accepts", list(OLD_PICTURE_ACCEPTS))
        note(at + ("type",), "set", "the picture type is an asset that accepts the types the old check accepted")
    elif spec.get("type") == "asset" and "accepts" not in spec:
        declared = spec.pop("media_type", None)
        _insert_after(spec, "type", "accepts", [declared] if isinstance(declared, str) else list(DEFAULT_ASSET_ACCEPTS))
        note(at + ("accepts",), "add", "taken from the declared media type" if declared else "the type the routes passed before")
    if spec.get("type") == "object" and "properties" not in spec and "opaque" not in spec:
        spec["opaque"] = True
        note(at + ("opaque",), "add", "an object with no declared properties is opaque")


CONFLICT = "conflict"


def _flat_kind(block: Any) -> str | None:
    """The key a binding with a transport block would flatten to, or None when it has no transport kind."""
    transport = block.get("transport") if isinstance(block, dict) else None
    if isinstance(transport, dict) and isinstance(transport.get("kind"), str):
        return transport["kind"]
    return None


def conversion_errors(notes: list[ConversionNote]) -> list[ContractError]:
    """The edits the converter refused to make. The checker cannot see them (a binding is opaque to it), so they are errors."""
    return [ContractError(note.path, "conversion_conflict", note.reason) for note in notes if note.operation == CONFLICT]


def convert_and_check(doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[ConversionNote], Report]:
    """Convert a document, check the result, and add the conversion conflicts to the report."""
    converted, notes = convert_1_to_2(doc)
    # The conflicts join the raw errors before the cap, so one cap and one "more errors" marker apply.
    report = check_contract(converted, extra_errors=conversion_errors(notes))
    return converted, notes, report


def _convert_bindings(out: dict[str, Any], note: Any) -> tuple[dict[str, Any], set[str]]:
    """The new bindings, and the keys of those that had a transport block (a form POST to a service)."""
    bindings: dict[str, Any] = {}
    flattened: set[str] = set()
    kinds = {key: _flat_kind(block) for key, block in out["bindings"].items()}
    # The target is found before the occupied keys are excluded. Bindings that would flatten to one key, or onto a key
    # another binding already holds, would lose one of them: all of those stay as written, and the notes say so.
    clashing = {
        key for key, kind in kinds.items() if kind is not None and (
            list(kinds.values()).count(kind) > 1 or (kind in out["bindings"] and kind != key))}
    clashing |= {kind for key, kind in kinds.items() if key in clashing and kind in out["bindings"]}
    for key, block in out["bindings"].items():
        kind = kinds[key]
        if key in clashing:
            note(("bindings", key), CONFLICT, f"another binding also uses {kind or key}; both stay unconverted")
        elif kind is not None and kind not in out["bindings"]:
            transport = block["transport"]
            block = {name: value for name, value in block.items() if name != "transport"}
            block["service"] = key
            block.update({name: value for name, value in transport.items() if name != "kind"})
            note(("bindings", kind), "rename", "the key names the adapter; the engine is a service reference")
            key = kind
            flattened.add(key)
        bindings[key] = block
    requires = out.pop("requires", None)
    if requires is not None:
        if len(bindings) == 1 and isinstance(next(iter(bindings.values())), dict):
            (target,) = bindings
            bindings[target]["requires"] = requires
            note(("bindings", target, "requires"), "move", "requirements belong to the binding that needs them")
        else:
            out["requires"] = requires  # no one binding to take it; the check shows it
    return bindings, flattened


def _generate_seeds(out: dict[str, Any], flattened: set[str], note: Any) -> None:
    """An integer input called ``seed`` that a binding other than a form POST fills gets the generator the routes drew with."""
    inputs = out.get("inputs")
    if not isinstance(inputs, dict):
        return
    maps = [block.get("map") for key, block in out["bindings"].items() if isinstance(block, dict) and key not in flattened]
    if not any(isinstance(mapped, dict) and "seed" in mapped for mapped in maps):
        return
    spec = inputs.get("seed")
    if spec is None:
        inputs["seed"] = spec = {"type": "integer", "minimum": 0, "settable": False}
        note(("inputs", "seed"), "add", "a map targets this input but the document did not declare it; only the route fills it")
    if isinstance(spec, dict) and spec.get("type") == "integer" and "generate" not in spec and "default" not in spec:
        spec["generate"] = dict(SEED_GENERATOR)
        note(("inputs", "seed", "generate"), "add", "the routes drew this value when the caller gave none")
