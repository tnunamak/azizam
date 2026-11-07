"""The one-time conversion of a workflow document from schema version 1 to 2 (CODE_TO_DATA_S3, D3), and the report-only
check the loader runs on every bundled document until the files are rewritten (S3.5).

This module is temporary. It may name the keys of the old shape (``trusted``, ``seed``, ``transport``), which the permanent
contract module (``workflow_contract``) must not. When the bundled files and the stored records are v2, it goes.

``convert_1_to_2`` is pure: it returns a new document and the list of edits it made. It does not decide whether the result
is valid; ``check_contract`` does. A document it cannot improve passes through unchanged, so the check shows what is wrong.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from gateway.manifests import MEDIA_INPUT_TYPES
from gateway.workflow_contract import check_contract

log = logging.getLogger(__name__)

# What the routes passed to the media resolver before inputs declared their own accepted types.
DEFAULT_ASSET_ACCEPTS = ["image/*"]
# What the old mesh-only picture type accepted.
OLD_PICTURE_ACCEPTS = ["image/png", "image/jpeg", "image/webp"]
SEED_GENERATOR = {"generator": "random_integer", "minimum": 0, "maximum": 4294967295}
BACKFILL_NOTE = {"capability": "intent-prefix rule, schema 1 to 2"}
BACKFILL_KEY = "azizam.dev/backfill"


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


def _convert_bindings(out: dict[str, Any], note: Any) -> tuple[dict[str, Any], set[str]]:
    """The new bindings, and the keys of those that had a transport block (a form POST to a service)."""
    bindings: dict[str, Any] = {}
    flattened: set[str] = set()
    for key, block in out["bindings"].items():
        transport = block.get("transport") if isinstance(block, dict) else None
        if isinstance(transport, dict) and isinstance(transport.get("kind"), str) and transport["kind"] not in out["bindings"]:
            block = {name: value for name, value in block.items() if name != "transport"}
            kind = transport["kind"]
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


def log_bundled_report(manifest: Mapping[str, Any], source: object = None) -> None:
    """Convert a loaded v1 document, check it as v2 and log the report. Nothing is refused and nothing is raised."""
    try:
        converted, _notes = convert_1_to_2(manifest)
        report = check_contract(converted)
    except Exception:  # the report must never stop a load
        log.exception("workflow contract report failed for %s", source or manifest.get("id"))
        return
    name = manifest.get("id") or source
    if report.ok:
        log.debug("workflow %s: no schema 2 contract errors", name)
        return
    log.warning("workflow %s: %d schema 2 contract errors (report only)", name, len(report.errors))
    for error in report.errors[:MAX_LOGGED_ERRORS]:
        log.warning("workflow %s: %s %s: %s", name, error.pointer or "/", error.code, error.message)
