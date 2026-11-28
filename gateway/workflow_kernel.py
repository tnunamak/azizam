"""The shared pure rules of code-to-data S4 to S8 (docs/design/CODE_TO_DATA_S4_S8.md, section 3.0).

More than one builder must apply each rule here the same way, so each is written once: the canonical name token and the
names a workflow id generates (the store's clash check and the OpenAPI builder), the name-clash rule over a whole
prospective revision, the execution digest (annotations never change what runs) and its byte encoding, the identity of
a run request for idempotent replay, and the closed selector schema (frontend rows and the store's save check). No I/O,
no globals, no domain words.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from gateway.job_envelope import canonical_bytes
from gateway.manifests import ContractError
from gateway.workflow_runtime_types import ID_CLASH, NameOwner

if TYPE_CHECKING:
    from gateway.workflow_runtime_types import RunRequest

_NON_TOKEN = re.compile(r"[^a-z0-9]+")
# Kinds that share one address space: a request can name any of them where it names a target (a model field, an MCP
# target, a route segment). Routes have their own space (method and canonical path).
TARGET_KINDS = frozenset({"workflow", "workflow_alias", "model", "model_alias", "frontend"})
# An alias addresses the entity it names: a workflow and its alias, or a model and its alias, are one owner.
_ENTITY_OF = {"workflow_alias": "workflow", "model_alias": "model"}


def owner_entity(owner: NameOwner) -> tuple[str, str]:
    """The owning entity of a name: (entity kind, id). Two names with one owning entity never clash."""
    return _ENTITY_OF.get(owner.kind, owner.kind), owner.id


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

    Target kinds share one token space; operation, component and route names each have their own. One owning entity
    (``owner_entity``) may hold a token many times: a workflow's id and its alias, or its id and its component name. Two
    different owning entities of one token clash. Because operation and component names derive from the id's token, two
    workflows whose generated names collide already clash in the target space."""
    spaces: dict[tuple[str, str], dict[tuple[str, str], str]] = {}
    for name, owner in names:
        space = "target" if owner.kind in TARGET_KINDS else owner.kind
        token = name if owner.kind == "route" else canonical_token(name)
        holder = owner_entity(owner)
        spaces.setdefault((space, token), {}).setdefault(holder, name)
    problems = []
    for (space, token), holders in sorted(spaces.items()):
        if len(holders) > 1:
            listed = ", ".join(f"{kind} {owner_id} ({name!r})" for (kind, owner_id), name in sorted(holders.items()))
            problems.append(ContractError(("id",), ID_CLASH, f"{space} name {token!r} has more than one owner: {listed}"))
    return tuple(problems)


def _strip_spec(spec: Any) -> Any:
    """One schema node without ``annotations``, recursing only where the grammar nests schema nodes (``properties``
    and ``items``). Values (``default``, ``enum``, ``const``, examples) are data and are never touched, even when they hold
    a key named ``annotations``."""
    if not isinstance(spec, Mapping):
        return spec
    out = {key: value for key, value in spec.items() if key != "annotations"}
    if isinstance(out.get("properties"), Mapping):
        out["properties"] = {name: _strip_spec(child) for name, child in out["properties"].items()}
    if "items" in out:
        out["items"] = _strip_spec(out["items"])
    return out


def _strip_output(spec: Any) -> Any:
    """An output spec without ``annotations``, and its JSON ``schema`` (S3 D5: the input grammar) stripped the same
    way as an input spec."""
    out = _strip_spec(spec)
    if isinstance(out, Mapping) and "schema" in out:
        out = {**out, "schema": _strip_spec(out["schema"])}
    return out


def execution_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """The contract without its annotations: root, input specs (with nested properties and items), output specs and
    their JSON output schemas. S3 says annotations never execute, so they never change the execution digest."""
    out = {key: value for key, value in contract.items() if key != "annotations"}
    if isinstance(out.get("inputs"), Mapping):
        out["inputs"] = {name: _strip_spec(spec) for name, spec in out["inputs"].items()}
    if isinstance(out.get("outputs"), Mapping):
        out["outputs"] = {name: _strip_output(spec) for name, spec in out["outputs"].items()}
    return out


def _thaw(value: Any) -> Any:
    """Read-only views (the snapshot's mappings and tuples) as the plain JSON values they show."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value


def digest(value: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of ``job_envelope.canonical_bytes`` (sorted keys, no spaces, ASCII escapes, no NaN): the bytes
    the definition store already keys blobs by, so a digest computed here names the blob a job pinned. No wrapper: a binding body or a stored record is hashed as
    exactly the mapping the store writes."""
    return "sha256:" + hashlib.sha256(canonical_bytes(_thaw(value))).hexdigest()


def execution_digest(contract: Mapping[str, Any]) -> str:
    """The digest of ``execution_contract``. The definition store keys the contract blob by
    ``canonical_bytes(execution_contract(contract))``; builder C writes and reads it with this module's functions."""
    return "sha256:" + hashlib.sha256(canonical_bytes(_thaw(execution_contract(contract)))).hexdigest()


def run_identity(owner_scope: str, maker: str, req: RunRequest) -> str:
    """What an idempotency key is bound to: owner scope, maker, workflow id, binding key, the inputs, and each media
    input's item digests in order, plus the wire options when there are any and the effective prompt-retention decision when one was taken. Never a spool key, a file name, a wait or a frontend: two requests that stage the same
    bytes are one request."""
    media = {name: [ref.digest for ref in refs] for name, refs in sorted(req.media.items())}
    identity: dict[str, Any] = {"owner_scope": owner_scope, "maker": maker, "workflow_id": req.workflow_id,
                                "binding_key": req.binding_key, "inputs": req.inputs, "media": media}
    if req.options:      # wire options are part of the request; an empty set keeps the identity it always had
        identity["options"] = req.options
    if req.prompt_retention:      # the effective keep/forget decision, so a changed decision never replays the other run
        identity["prompt_retention"] = req.prompt_retention
    return digest(identity)


# The closed selector schema. A row selects explicit ids, or every workflow whose contract matches:
#   {"inputs": {<name>: {"type": <input type>} | {"media_type": <type or family/*>}},
#    "outputs": {"kind": "json" | "assets", "media_type": <type or family/*>}}
# ``intent`` (a string) selects the workflows that declare that intent; it may stand with inputs and outputs.
# ``match`` holds at least one of intent, inputs and outputs, each present section holds at least one predicate, and every value
# is checked here, so ``selector_matches`` is total over every selector that ``selector_problems`` accepts.
_INPUT_KEYS = frozenset({"type", "media_type"})
_OUTPUT_KEYS = frozenset({"kind", "media_type"})
INPUT_TYPES = frozenset({"string", "integer", "number", "boolean", "asset", "object", "array"})   # workflow.schema.json
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/([a-z0-9][a-z0-9.+-]*|\*)$")


def _media_type_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_MEDIA_TYPE.match(value))


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
        if not isinstance(match, Mapping) or not match or set(match) - {"intent", "inputs", "outputs"}:
            problems.append(ContractError(("selects", "match"), "selector_match",
                                          "match holds intent, inputs and/or outputs, and is not empty"))
        else:
            if "intent" in match and not (isinstance(match["intent"], str) and match["intent"]):
                problems.append(ContractError(("selects", "match", "intent"), "selector_match", "intent is a non-empty string"))
            if "inputs" in match:
                inputs = match["inputs"]
                if not isinstance(inputs, Mapping) or not inputs or not all(
                        isinstance(name, str) and name and isinstance(v, Mapping) and len(v) == 1
                        and set(v) <= _INPUT_KEYS
                        and (isinstance(v.get("type"), str) and v["type"] in INPUT_TYPES if "type" in v else _media_type_ok(v.get("media_type")))
                        for name, v in inputs.items()):
                    problems.append(ContractError(("selects", "match", "inputs"), "selector_match",
                                                  "inputs names at least one input, each with exactly one of type (an "
                                                  "input type) or media_type (type/subtype or family/*)"))
            if "outputs" in match:
                outputs = match["outputs"]
                if not isinstance(outputs, Mapping) or not outputs or set(outputs) - _OUTPUT_KEYS \
                        or ("kind" in outputs and outputs["kind"] not in ("json", "assets")) \
                        or ("media_type" in outputs and not _media_type_ok(outputs["media_type"])):
                    problems.append(ContractError(("selects", "match", "outputs"), "selector_match",
                                                  "outputs holds kind (json or assets) and/or media_type, at least one"))
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
    """Whether a valid selector (``selector_problems`` is empty) selects this workflow. Inputs must all match (AND);
    outputs match when any one output fits."""
    if "ids" in selector:
        return workflow_id in selector["ids"]
    match = selector["match"]
    if "intent" in match and contract.get("intent") != match["intent"]:
        return False
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
