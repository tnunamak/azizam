"""The one-time conversion of a workflow document from schema version 1 to 2 (CODE_TO_DATA_S3, D3), and the report-only
check the loader runs on every bundled document until the files are rewritten (S3.5).

This module is temporary. It may name the keys of the old shape (``trusted``, ``seed``, ``transport``), which the permanent
contract module (``workflow_contract``) must not. When the bundled files and the stored records are v2, it goes.

``convert_1_to_2`` is pure: it returns a new document and the list of edits it made. It does not decide whether the result
is valid; ``check_contract`` does. A document it cannot improve passes through unchanged, so the check shows what is wrong.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from gateway.manifests import MEDIA_INPUT_TYPES, ContractError
from gateway.workflow_contract import CONTRACT_VERSION, Report, check_contract

log = logging.getLogger(__name__)

# What the routes passed to the media resolver before inputs declared their own accepted types.
DEFAULT_ASSET_ACCEPTS = ["image/*"]
# What the old mesh-only picture type accepted.
OLD_PICTURE_ACCEPTS = ["image/png", "image/jpeg", "image/webp"]
SEED_GENERATOR = {"generator": "random_integer", "minimum": 0, "maximum": 4294967295}
BACKFILL_NOTE = {"capability": "intent-prefix rule, schema 1 to 2"}
BACKFILL_KEY = "azizam.dev/backfill"
POLICY_KEYS = ("exposure", "trust_tier", "resource_tier", "tags")


@dataclass(frozen=True)
class PolicyFacts:
    """The record-level facts of a workflow (CODE_TO_DATA_S3, D2): what a reader needs to decide who may run it and at what
    cost, and what a schema 1 document said of its media inputs that the contract no longer says. ``None`` (or an empty
    list) means the record states nothing; each reader fills its own old default until S3.5 removes the defaults. They
    never live in the contract document.

    ``v1_assets`` holds, for each asset input of a schema 1 document (the mesh-only picture type is one), the media type it
    declared (empty when it declared none): the contract only lists what the input accepts, and a default list looks the
    same as a declared one. ``pictures`` names the inputs that were the picture type, which the modality readers did not
    count. Both are empty for a document that was written as schema 2."""

    exposure: str | None = None
    trust_tier: str | None = None
    resource_tier: str | None = None
    tags: tuple[str, ...] = ()
    v1_assets: tuple[tuple[str, str], ...] = ()
    pictures: tuple[str, ...] = ()

    def declared_media(self, name: str) -> str | None:
        """What a schema 1 document declared for this asset input (``""`` for nothing), or ``None`` for an input it did not
        have as an asset."""
        return dict(self.v1_assets).get(name)


def _fact_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _v1_media_facts(manifest: Mapping[str, Any]) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    inputs = manifest.get("inputs")
    if manifest.get("schema_version") != 1 or not isinstance(inputs, Mapping):
        return (), ()
    assets: list[tuple[str, str]] = []
    pictures: list[str] = []
    for name, spec in inputs.items():
        kind = spec.get("type") if isinstance(spec, Mapping) else None
        if kind == "asset":
            assets.append((str(name), str(spec.get("media_type") or "").strip()))
        elif kind in MEDIA_INPUT_TYPES:
            pictures.append(str(name))
            assets.append((str(name), ""))  # the readers showed a picture as an asset that declared nothing
    return tuple(assets), tuple(pictures)


def policy_facts_of(manifest: Mapping[str, Any]) -> PolicyFacts:
    """The facts of a schema 1 document, which holds them beside the contract. This and ``normalize`` are the only readers."""
    raw_tags = manifest.get("tags")
    tags = [item for item in raw_tags if isinstance(item, str) and item] if isinstance(raw_tags, list) else []
    assets, pictures = _v1_media_facts(manifest)
    return PolicyFacts(
        exposure=_fact_text(manifest.get("exposure")), trust_tier=_fact_text(manifest.get("trust_tier")),
        resource_tier=_fact_text(manifest.get("resource_tier")), tags=tuple(dict.fromkeys(tags)),
        v1_assets=assets, pictures=pictures)


def trusted_of(manifest: Mapping[str, Any]) -> bool:
    """Whether a schema 1 document says it is reviewed: provenance, which belongs to the record, not the contract."""
    return bool(manifest.get("trusted", False))


def _carry_requirements(contract: dict[str, Any]) -> None:
    """Requirements the converter left at the top (a document with several bindings, which the contract check reports) go
    into every binding, so no accepted document loses them: a reader gathers them across the bindings without repeats. The
    stored document and the conversion report are not changed."""
    requires = contract.get("requires")
    bindings = contract.get("bindings")
    if requires is None or not isinstance(bindings, dict):
        return
    owners = [block for block in bindings.values() if isinstance(block, dict)]
    if not owners:
        return
    del contract["requires"]
    for block in owners:
        block["requires"] = copy.deepcopy(requires)


def normalize(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], PolicyFacts]:
    """What every reader of a loaded schema 1 document reads: the schema 2 contract (made in memory, nothing is checked or
    refused) and the policy facts that were beside it."""
    contract, _ = convert_1_to_2(manifest)
    for key in POLICY_KEYS:
        contract.pop(key, None)
    _carry_requirements(contract)
    return contract, policy_facts_of(manifest)


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


def _backfill_capability(out: dict[str, Any], note: Any) -> None:
    intent = out.get("intent")
    if "capability" in out or not isinstance(intent, str):
        return
    # The one copy of the rule that infers a capability from an intent. It is deleted with the inference (S3.5).
    from gateway.workflows_catalog import _capability_from_intent

    out["capability"] = _capability_from_intent(intent)
    annotations = out.setdefault("annotations", {})
    if isinstance(annotations, dict):
        annotations[BACKFILL_KEY] = dict(BACKFILL_NOTE)
    note(("capability",), "backfill", "inferred once from the intent prefix, as the catalog did")


def _convert_input(at: tuple[str, ...], spec: dict[str, Any], note: Any) -> None:
    if spec.pop("modality", None) is not None:
        note(at + ("modality",), "remove", "an asset input lists the media types it accepts")
    if spec.get("type") in MEDIA_INPUT_TYPES - {"asset"}:  # the mesh-only picture type of schema 1
        spec["type"] = "asset"
        spec["accepts"] = list(OLD_PICTURE_ACCEPTS)
        note(at + ("type",), "set", "the picture type is an asset that accepts the types the old check accepted")
    elif spec.get("type") == "asset" and "accepts" not in spec:
        declared = spec.pop("media_type", None)
        spec["accepts"] = [declared] if isinstance(declared, str) else list(DEFAULT_ASSET_ACCEPTS)
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


# ------------------------------------------------------------------------------------------------ report-only check

MAX_LOGGED_ERRORS = 10
_REPORTED: set[tuple[str, int]] = set()
_REPORTED_LIMIT = 4096
_REPORTED_LOCK = threading.Lock()


def forget_reported() -> None:
    """Forget which documents were reported (a test wants a fresh report)."""
    with _REPORTED_LOCK:
        _REPORTED.clear()


def _first_report(manifest: Mapping[str, Any]) -> bool:
    """True once per (document digest, contract version) for the process; the later loads skip the work."""
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    key = (digest, CONTRACT_VERSION)
    # Membership, the capacity reset and the insert are one step: two threads must not both be first.
    with _REPORTED_LOCK:
        if key in _REPORTED:
            return False
        if len(_REPORTED) >= _REPORTED_LIMIT:
            _REPORTED.clear()
        _REPORTED.add(key)
        return True


def log_bundled_report(manifest: Mapping[str, Any], source: object = None) -> None:
    """Convert a loaded v1 document, check it as v2 and log the report once. Nothing is refused and nothing is raised,
    not even by a logging handler."""
    try:
        if not _first_report(manifest):
            return
        _, _, report = convert_and_check(manifest)
        name = manifest.get("id") or source
        if report.ok:
            log.debug("workflow %s: no schema 2 contract errors", name)
            return
        log.warning("workflow %s: %d schema 2 contract errors (report only)", name, len(report.errors))
        for error in report.errors[:MAX_LOGGED_ERRORS]:
            log.warning("workflow %s: %s %s: %s", name, error.pointer or "/", error.code, error.message)
    except Exception:  # the report must never stop a load
        try:
            log.exception("workflow contract report failed for %s", source)
        except Exception:  # the handler that failed may be the one that logs this
            pass
