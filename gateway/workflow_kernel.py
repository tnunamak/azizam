"""The shared pure rules of code-to-data S4 to S8 (docs/design/CODE_TO_DATA_S4_S8.md, section 3.0).

More than one builder must apply each rule here the same way, so each is written once: the canonical name token and the
names a workflow id generates (the store's clash check and the OpenAPI builder), the name-clash rule over a whole
prospective revision, the execution digest (annotations never change what runs), and the closed selector schema
(frontend rows and the store's save check). No I/O, no globals, no domain words.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from gateway.manifests import ContractError
from gateway.workflow_runtime_types import ID_CLASH, NameOwner

_NON_TOKEN = re.compile(r"[^a-z0-9]+")
# Kinds that share one address space: a request can name any of them where it names a target (a model field, an MCP
# target, a route segment). Routes have their own space (method and canonical path).
TARGET_KINDS = frozenset({"workflow", "workflow_alias", "model", "model_alias", "frontend"})


def canonical_token(name: str) -> str:
    """``Audio.Generate.x-y`` and ``audio_generate x.y`` both give ``audio_generate_x_y``. Two names with one token are
    one name: the save that would make them two different owners is refused."""
    return _NON_TOKEN.sub("_", str(name).strip().lower()).strip("_")


def generated_names(workflow_id: str) -> dict[str, str]:
    """The names the OpenAPI document and the generic route derive from a workflow id. The OpenAPI builder must use
    these and nothing else, so the clash check sees every name it will emit."""
    token = canonical_token(workflow_id)
    return {"operation": f"run_{token}", "component": token}


def route_key(method: str, path: str) -> str:
    """One route address: the method and the path with each segment lowered and its trailing slash dropped."""
    segments = [segment.lower() for segment in str(path).split("/") if segment]
    return f"{method.upper()} /{'/'.join(segments)}"


def name_clashes(names: Iterable[tuple[str, NameOwner]]) -> tuple[ContractError, ...]:
    """Every clash in a whole prospective revision. ``names`` is every (name, owner) pair the revision holds: for a
    workflow its id, its declared aliases and its ``generated_names`` (owner kind ``operation`` and ``component`` with
    the workflow id); model ids and model aliases; frontend ids; route keys (``route_key``, owner kind ``route``).

    Target kinds share one token space; operation, component and route names each have their own. One owner may hold a
    name many times (a workflow's id and its component share a token). Two different owners of one token clash."""
    spaces: dict[tuple[str, str], dict[tuple[str, str], str]] = {}
    for name, owner in names:
        space = "target" if owner.kind in TARGET_KINDS else owner.kind
        token = name if owner.kind == "route" else canonical_token(name)
        holder = (owner.kind, owner.id)
        spaces.setdefault((space, token), {}).setdefault(holder, name)
    problems = []
    for (space, token), holders in sorted(spaces.items()):
        if len(holders) > 1:
            listed = ", ".join(f"{kind} {owner_id} ({name!r})" for (kind, owner_id), name in sorted(holders.items()))
            problems.append(ContractError(("id",), ID_CLASH, f"{space} name {token!r} has more than one owner: {listed}"))
    return tuple(problems)


def _strip_spec(spec: Any) -> Any:
    if not isinstance(spec, Mapping):
        return spec
    out = {key: value for key, value in spec.items() if key != "annotations"}
    if isinstance(out.get("properties"), Mapping):
        out["properties"] = {name: _strip_spec(child) for name, child in out["properties"].items()}
    if "items" in out:
        out["items"] = _strip_spec(out["items"])
    return out


def execution_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """The contract without its annotations (root, input specs and their nested properties and items, output specs):
    S3 says annotations never execute, so they never change the execution digest."""
    out = {key: value for key, value in contract.items() if key != "annotations"}
    for section in ("inputs", "outputs"):
        if isinstance(out.get(section), Mapping):
            out[section] = {name: _strip_spec(spec) for name, spec in out[section].items()}
    return out


def _plain(value: Any) -> Any:
    """Read-only views (the snapshot's mappings) digest as the dicts they show."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not canonical JSON")


def digest(value: Any) -> str:
    """``sha256:<hex>`` of canonical JSON (sorted keys, no spaces, UTF-8)."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_plain).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def execution_digest(contract: Mapping[str, Any]) -> str:
    return digest(execution_contract(contract))


# The closed selector schema. A row selects explicit ids, or every workflow whose contract matches:
#   {"inputs": {<name>: {"type": <json type>} | {"media_type": <type or family/*>}},
#    "outputs": {"kind": "json" | "assets", "media_type": <type or family/*>}}
# Every key is optional; an empty match selects nothing.
_INPUT_KEYS = frozenset({"type", "media_type"})
_OUTPUT_KEYS = frozenset({"kind", "media_type"})


def selector_problems(selector: Mapping[str, Any]) -> tuple[ContractError, ...]:
    problems: list[ContractError] = []
    allowed = {"ids", "match"}
    for key in set(selector) - allowed:
        problems.append(ContractError(("selects", key), "unknown_key", f"a selector holds only ids or match, not {key}"))
    if ("ids" in selector) == ("match" in selector):
        problems.append(ContractError(("selects",), "selector_shape", "a selector holds exactly one of ids or match"))
    ids = selector.get("ids")
    if "ids" in selector and (not isinstance(ids, (list, tuple)) or not ids
                              or not all(isinstance(i, str) and i for i in ids)):
        problems.append(ContractError(("selects", "ids"), "selector_ids", "ids is a non-empty list of workflow ids"))
    match = selector.get("match")
    if "match" in selector:
        if not isinstance(match, Mapping) or not match or set(match) - {"inputs", "outputs"}:
            problems.append(ContractError(("selects", "match"), "selector_match",
                                          "match holds inputs and/or outputs, and is not empty"))
        else:
            inputs = match.get("inputs", {})
            if not isinstance(inputs, Mapping) or not all(
                    isinstance(v, Mapping) and len(v) == 1 and set(v) <= _INPUT_KEYS for v in inputs.values()):
                problems.append(ContractError(("selects", "match", "inputs"), "selector_match",
                                              "each input holds exactly one of type or media_type"))
            outputs = match.get("outputs", {})
            if not isinstance(outputs, Mapping) or set(outputs) - _OUTPUT_KEYS \
                    or outputs.get("kind", "json") not in ("json", "assets"):
                problems.append(ContractError(("selects", "match", "outputs"), "selector_match",
                                              "outputs holds kind (json or assets) and/or media_type"))
    return tuple(problems)


def _media_fits(wanted: str, declared: Any) -> bool:
    """Same family, and the types are equal or one side is the family wildcard (``image/*``)."""
    if not isinstance(declared, str) or "/" not in declared or "/" not in wanted:
        return False
    (want_family, want_sub), (have_family, have_sub) = wanted.split("/", 1), declared.split("/", 1)
    return want_family == have_family and (want_sub == have_sub or "*" in (want_sub, have_sub))


def _input_fits(wanted: str, spec: Mapping[str, Any]) -> bool:
    """An asset input (S3 D4) fits when any type it accepts fits."""
    accepts = spec.get("accepts")
    return isinstance(accepts, (list, tuple)) and any(_media_fits(wanted, declared) for declared in accepts)


def selector_matches(selector: Mapping[str, Any], workflow_id: str, contract: Mapping[str, Any]) -> bool:
    """Whether a valid selector (``selector_problems`` is empty) selects this workflow."""
    if "ids" in selector:
        return workflow_id in selector["ids"]
    match = selector["match"]
    declared_inputs = contract.get("inputs") or {}
    for name, want in (match.get("inputs") or {}).items():
        spec = declared_inputs.get(name)
        if not isinstance(spec, Mapping):
            return False
        if "type" in want and spec.get("type") != want["type"]:
            return False
        if "media_type" in want and not _input_fits(want["media_type"], spec):
            return False
    want_out = match.get("outputs") or {}
    if want_out:
        outputs = [o for o in (contract.get("outputs") or {}).values() if isinstance(o, Mapping)]
        def fits(output: Mapping[str, Any]) -> bool:
            media = output.get("media_type")
            kind = "json" if media == "application/json" else "assets"
            return (("kind" not in want_out or want_out["kind"] == kind)
                    and ("media_type" not in want_out or _media_fits(want_out["media_type"], media)))
        if not any(fits(o) for o in outputs):
            return False
    return True
