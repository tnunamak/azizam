"""Binding descriptors: what a binding block may hold, as data (CODE_TO_DATA_S3, D7).

A binding key names an adapter. The adapter's protocol description closes the binding with three blocks:

- ``binding_fields``: every field the binding may hold, and its kind. The kinds are a small fixed set that this module
  checks with no adapter knowledge (``template``, ``input_map``, ``input_list``, ``target``, ``target_list``,
  ``relative_path``, ``selectors``, ``requirements``, ``service``, ``role``, ``text``, ``enum``, ``boolean``).
- ``roles``: the reviewed implementations a ``role`` field may name, each with the fields only that role reads.
- ``inputs_via``: whether the engine receives inputs only through the input map, or the role's code reads them.

A save is refused for an unknown binding key (``binding_unknown``), an unknown field (``binding_field_unknown``) or a
role the descriptor does not list (``role_unknown``), each with its path. What an engine-side reference points at is
checked by the adapter's reviewed ``check_targets`` hook (``binding_registry``). At run, a role the descriptor lists but
no code registered is ``binding_unavailable`` (``resolve_role`` finds nothing).

The module names no workflow, engine, model, intent or modality.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gateway import binding_registry
from gateway.manifests import ContractErrors, ErrorCollector, PathPart

KINDS = (
    "template", "input_map", "input_list", "target", "target_list", "relative_path", "selectors", "requirements",
    "service", "role", "text", "enum", "boolean",
)
INPUTS_VIA = ("map", "role")
MAX_TEXT = 256
MAX_LIST = 64
PROTOCOL_DIR = Path(__file__).resolve().parent.parent / "registry" / "protocols"

_FIELD_KEYS = frozenset({"kind", "required", "max", "values"})
_BOUNDED_KINDS = frozenset({"text", "input_list", "target_list"})
_MAP_KINDS = frozenset({"input_map", "target", "target_list"})   # what the adapter's check_targets hook reads
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SERVICE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SEGMENT = r"[A-Za-z0-9._~-]+"
_TEMPLATE_REF = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
_RELATIVE_PATH = re.compile(rf"/{_SEGMENT}(?:/{_SEGMENT})*")


class BindingDescriptorError(ValueError):
    """A description's binding blocks are malformed. The message names the problem; there is nothing to load."""


@dataclass(frozen=True)
class BindingField:
    name: str
    kind: str
    required: bool = False
    maximum: int | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class BindingDescriptor:
    """The binding form of one adapter, parsed and checked."""

    binding: str
    adapter_id: str | None
    check_targets: str | None
    fields: Mapping[str, BindingField]
    roles: Mapping[str, Mapping[str, BindingField]] = field(default_factory=dict)
    inputs_via: str = "map"

    @property
    def role_field(self) -> str | None:
        """The name of the field that selects a role, or ``None`` when the adapter has no roles."""
        return next((name for name, spec in self.fields.items() if spec.kind == "role"), None)


def _bad(message: str) -> BindingDescriptorError:
    return BindingDescriptorError(f"binding_fields: {message}")


def _parse_field(name: Any, raw: Any, where: str, *, in_role: bool) -> BindingField:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _bad(f"{where} field name {name!r} is not a plain name")
    if not isinstance(raw, Mapping):
        raise _bad(f"{where} field {name} must be a mapping")
    extra = sorted(str(key) for key in raw if key not in _FIELD_KEYS)
    if extra:
        raise _bad(f"{where} field {name} has unknown keys {extra}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise _bad(f"{where} field {name} kind must be one of {list(KINDS)}")
    if in_role and kind == "role":
        raise _bad(f"{where} field {name} cannot be a role: a role does not name another role")
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise _bad(f"{where} field {name} required must be true or false")
    maximum = raw.get("max")
    if maximum is not None:
        if kind not in _BOUNDED_KINDS:
            raise _bad(f"{where} field {name} of kind {kind} takes no max")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_TEXT:
            raise _bad(f"{where} field {name} max must be a whole number from 1 to {MAX_TEXT}")
    values = raw.get("values")
    if kind == "enum":
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise _bad(f"{where} field {name} of kind enum needs a list of values")
    elif values is not None:
        raise _bad(f"{where} field {name} of kind {kind} takes no values")
    return BindingField(name, kind, required, maximum, tuple(values or ()))


def parse_binding_descriptor(
    *, binding: Any, executor: Any, binding_fields: Any, roles: Any = None, inputs_via: Any = None,
    check_targets: Any = None,
) -> BindingDescriptor:
    """Check the binding blocks of one description and compile them. Raises ``BindingDescriptorError``."""
    if not isinstance(binding, str) or not binding:
        raise _bad("a description that closes a binding must name its binding")
    if not isinstance(executor, str) or not executor.startswith("adapter:"):
        raise _bad("only a reviewed adapter has a binding")
    if not isinstance(binding_fields, Mapping) or not binding_fields:
        raise _bad("must be a non-empty mapping")
    fields = {name: _parse_field(name, raw, "binding", in_role=False) for name, raw in binding_fields.items()}
    role_fields = [name for name, spec in fields.items() if spec.kind == "role"]
    if len(role_fields) > 1:
        raise _bad("at most one field can select a role")
    parsed_roles: dict[str, Mapping[str, BindingField]] = {}
    if roles is not None:
        if not isinstance(roles, Mapping) or not roles:
            raise _bad("roles must be a non-empty mapping")
        if not role_fields:
            raise _bad("roles are listed, but no field is of kind role")
        for role_id, body in roles.items():
            if not isinstance(role_id, str) or not _NAME.fullmatch(role_id):
                raise _bad(f"role id {role_id!r} is not a plain name")
            if not isinstance(body, Mapping):
                raise _bad(f"role {role_id} must be a mapping")
            raw_fields = body.get("fields", {})
            if not isinstance(raw_fields, Mapping):
                raise _bad(f"role {role_id} fields must be a mapping")
            own = {name: _parse_field(name, raw, f"role {role_id}", in_role=True) for name, raw in raw_fields.items()}
            clash = sorted(set(own) & set(fields))
            if clash:
                raise _bad(f"role {role_id} repeats the binding fields {clash}")
            parsed_roles[role_id] = own
    elif role_fields:
        raise _bad("a field of kind role needs roles")
    via = "map" if inputs_via is None else inputs_via
    if via not in INPUTS_VIA:
        raise _bad(f"inputs_via must be one of {list(INPUTS_VIA)}")
    if via == "role" and not parsed_roles:
        raise _bad("inputs_via role needs roles")
    if check_targets is not None and (not isinstance(check_targets, str) or not _NAME.fullmatch(check_targets)):
        raise _bad("check_targets must be the name of a reviewed hook")
    return BindingDescriptor(binding, executor.split(":", 1)[1], check_targets, fields, parsed_roles, via)


def descriptor_from_document(doc: Mapping[str, Any]) -> BindingDescriptor | None:
    """The binding descriptor a description document holds, or ``None`` when it closes no binding."""
    if "binding_fields" not in doc:
        if "roles" in doc or "inputs_via" in doc or "check_targets" in doc:
            raise _bad("roles, inputs_via and check_targets need binding_fields")
        return None
    return parse_binding_descriptor(
        binding=doc.get("binding"), executor=doc.get("executor"), binding_fields=doc["binding_fields"],
        roles=doc.get("roles"), inputs_via=doc.get("inputs_via"), check_targets=doc.get("check_targets"),
    )


def shipped_binding_descriptors(directory: Path | None = None) -> dict[str, BindingDescriptor]:
    """The binding descriptor of every shipped protocol description, by the binding key the description names. Each file
    goes through the same validated reader the registry loader uses (the schema and the strict YAML reader), so a body the
    schema refuses is not read as an empty one. A binding key must be named by exactly one description; a repeat, or a file
    that does not load, raises ``BindingDescriptorError``."""
    from gateway.engine_protocol import description   # the loader imports this module

    found: dict[str, BindingDescriptor] = {}
    for path in sorted((directory or PROTOCOL_DIR).glob("*.yml")):
        try:
            parsed = description.parse_description_text(path.read_text(encoding="utf-8"), source=path.name).binding_descriptor
        except description.DescriptionError as exc:
            raise _bad(f"{path.name} does not load: {exc}") from exc
        if parsed is None:
            continue
        if parsed.binding in found:
            raise _bad(f"binding {parsed.binding} is named by more than one description")
        found[parsed.binding] = parsed
    return found


_SHIPPED: dict[Path, dict[str, BindingDescriptor]] = {}


def cached_shipped_binding_descriptors() -> dict[str, BindingDescriptor]:
    """The shipped descriptors, read once per process."""
    return _cached_shipped()


def _cached_shipped() -> dict[str, BindingDescriptor]:
    if PROTOCOL_DIR not in _SHIPPED:
        _SHIPPED[PROTOCOL_DIR] = shipped_binding_descriptors()
    return _SHIPPED[PROTOCOL_DIR]


# ------------------------------------------------------------------------------------------------------- save-time

def check_bindings(
    manifest: Mapping[str, Any], descriptors: Mapping[str, BindingDescriptor] | None = None, *,
    templates: Mapping[str, Mapping[str, Any]] | None = None, services: Collection[str] | None = None,
) -> None:
    """Refuse a manifest whose bindings break their descriptors. Raises ``ContractErrors`` with every error found."""
    errors = ErrorCollector()
    collect_binding_errors(manifest, errors, descriptors, templates=templates, services=services)
    errors.raise_if_any(ContractErrors)


def collect_binding_errors(
    manifest: Mapping[str, Any], errors: ErrorCollector, descriptors: Mapping[str, BindingDescriptor] | None = None, *,
    templates: Mapping[str, Mapping[str, Any]] | None = None, services: Collection[str] | None = None,
) -> None:
    """Add every binding error of ``manifest`` to ``errors``. ``descriptors`` default to the shipped registry. ``templates``
    maps a binding key to its template (a mapping); an adapter's target check reads it, and skips what it cannot read.
    ``services`` are the engine record ids a ``service`` field can name; with none given, that field is not resolved."""
    bindings = manifest.get("bindings") if isinstance(manifest, Mapping) else None
    if not isinstance(bindings, Mapping):
        return
    known = _cached_shipped() if descriptors is None else descriptors
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else None
    for key, block in bindings.items():
        at: tuple[PathPart, ...] = ("bindings", str(key))
        descriptor = known.get(key)
        if descriptor is None:
            errors.add(at, "binding_unknown", "no adapter description names this binding key")
        elif not isinstance(block, Mapping):
            errors.add(at, "type_mismatch", "a binding must be a mapping")
        else:
            _check_block(descriptor, block, at, inputs, (templates or {}).get(key), services, errors)


def _check_block(
    descriptor: BindingDescriptor, block: Mapping[str, Any], at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None,
    template: Mapping[str, Any] | None, services: Collection[str] | None, errors: ErrorCollector,
) -> None:
    allowed: dict[str, BindingField] = dict(descriptor.fields)
    role_known = True
    role_field = descriptor.role_field
    if role_field is not None and role_field in block:
        chosen = block[role_field]
        if isinstance(chosen, str) and chosen in descriptor.roles:
            allowed.update(descriptor.roles[chosen])
        else:
            role_known = False   # the fields of an unknown role cannot be told; the role is the one error
            if isinstance(chosen, str):
                errors.add(at + (role_field,), "role_unknown", "this role is not listed by the binding's description")
            else:
                errors.add(at + (role_field,), "type_mismatch", "a role must be text")
    for name in block:
        if name not in allowed and role_known:
            errors.add(at + (str(name),), "binding_field_unknown", "this field is not one the binding's description lists")
    missing = [name for name, spec in allowed.items() if spec.required and name not in block]
    if missing and role_known:
        errors.add(at, "required_field_missing", f"binding is missing {missing}")
    present = [spec for name, spec in allowed.items() if name in block and not (spec.kind == "role" and not role_known)]
    before = len(errors)
    for spec in present:
        if spec.kind != "role":
            _check_field(spec, block[spec.name], at + (spec.name,), inputs, services, errors)
    hook = binding_registry.target_check(descriptor.check_targets)
    needs_hook = [spec for spec in present if spec.kind in _MAP_KINDS]
    if needs_hook and hook is None:
        errors.add(at, "binding_unavailable", "no reviewed code checks the targets of this adapter's bindings")
    elif needs_hook and hook is not None and len(errors) == before:
        # A field with a shape error has no targets to read, so the hook runs only on a block the core check passed.
        for spec in needs_hook:
            hook(field=spec.name, kind=spec.kind, value=block[spec.name], at=at + (spec.name,), inputs=inputs,
                 template=template, errors=errors)
    if descriptor.inputs_via == "map" and len(errors) == before:
        # Last, and only on a block with no other error, so a binding keeps the errors it always gave.
        _check_required_inputs_bound(block, present, at, inputs, errors)


def _check_required_inputs_bound(block: Mapping[str, Any], present: list[BindingField], at: tuple[PathPart, ...],
                                 inputs: Mapping[str, Any] | None, errors: ErrorCollector) -> None:
    """A binding whose inputs reach the engine only through its input map must map every required input: a required input
    that no target applies could never reach the engine (``input_unbound``, CODE_TO_DATA_S3, D4). An optional input may stay
    unmapped. A map that did not pass the core check has no keys to read, and is skipped."""
    if not isinstance(inputs, Mapping):
        return
    for spec in present:
        if spec.kind != "input_map" or not isinstance(block[spec.name], Mapping):
            continue
        for name, input_spec in inputs.items():
            if isinstance(input_spec, Mapping) and input_spec.get("required") is True and name not in block[spec.name]:
                errors.add(at + (spec.name,), "input_unbound", f"the required input {name} is not in this map")


def _check_field(spec: BindingField, value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None,
                 services: Collection[str] | None, errors: ErrorCollector) -> None:
    kind = spec.kind
    limit = spec.maximum
    if kind == "template":
        # An extension package names its own template as ``package://<path>``; the path after the scheme is the reference.
        reference = value.removeprefix("package://") if isinstance(value, str) else value
        if not isinstance(reference, str) or len(reference) > MAX_TEXT or not _TEMPLATE_REF.fullmatch(reference) \
                or {".", ".."} & set(reference.split("/")):
            errors.add(at, "template_invalid", "a template reference must be a canonical relative path")
    elif kind == "relative_path":
        if not isinstance(value, str) or len(value) > MAX_TEXT or not _RELATIVE_PATH.fullmatch(value) \
                or {".", ".."} & set(value.split("/")):
            errors.add(at, "relative_path_invalid", "this must be a canonical slash-prefixed relative path")
    elif kind == "input_map":
        _check_input_map(value, at, inputs, errors)
    elif kind in ("input_list", "target_list"):
        _check_list(kind, value, at, inputs, limit or MAX_LIST, errors)
    elif kind == "target":
        if not isinstance(value, str) or not value or len(value) > (limit or MAX_TEXT):
            errors.add(at, "type_mismatch", "a target must be short non-empty text")
    elif kind in ("selectors", "requirements"):
        if not isinstance(value, Mapping):
            errors.add(at, "type_mismatch", f"this {kind} block must be a mapping")
    elif kind == "service":
        if not isinstance(value, str) or not _SERVICE_ID.fullmatch(value):
            errors.add(at, "type_mismatch", "a service must be an engine record id")
        elif services is not None and value not in services:
            errors.add(at, "service_unknown", "no engine record has this id")
    elif kind == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > (limit or MAX_TEXT):
            errors.add(at, "type_mismatch", "this must be short non-empty text")
    elif kind == "enum":
        if not isinstance(value, str) or value not in spec.values:
            errors.add(at, "enum_invalid", f"this must be one of {list(spec.values)}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            errors.add(at, "type_mismatch", "this must be true or false")


def _check_input_map(value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None, errors: ErrorCollector) -> None:
    if not isinstance(value, Mapping):
        errors.add(at, "type_mismatch", "an input map must be a mapping")
        return
    if inputs is None:
        return   # with broken inputs there is nothing to name; that error is listed elsewhere
    for logical in value:
        if logical not in inputs:
            errors.add(at + (str(logical),), "reference_unknown", "this key is not a declared input")


def _check_list(kind: str, value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None, limit: int,
                errors: ErrorCollector) -> None:
    if not isinstance(value, list):
        errors.add(at, "type_mismatch", "this must be a list")
        return
    if len(value) > limit:
        errors.add(at, "list_too_long", f"this list holds more than {limit} items")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or len(item) > MAX_TEXT:
            errors.add(at + (index,), "type_mismatch", "a list item must be short non-empty text")
        elif kind == "input_list" and inputs is not None and item not in inputs:
            errors.add(at + (index,), "reference_unknown", "this item is not a declared input")


# ------------------------------------------------------------------------------------------------------------- run

def resolve_role(
    binding_key: str, binding: Mapping[str, Any], serves: binding_registry.Entry | None,
    descriptors: Mapping[str, BindingDescriptor] | None = None,
) -> binding_registry.RoleImplementation | None:
    """The code a binding's role runs for the entry ``serves``, or ``None``. Every dispatcher asks this and nothing else:
    the binding's description must list a role field, the binding must hold a non-empty text role the description lists,
    and code registered for that role must serve this entry (``None`` asks for any entry). Anything else is
    ``binding_unavailable`` before any effect."""
    known = _cached_shipped() if descriptors is None else descriptors
    descriptor = known.get(binding_key)
    role_field = descriptor.role_field if descriptor is not None else None
    if descriptor is None or role_field is None or not isinstance(binding, Mapping):
        return None
    chosen = binding.get(role_field)
    if not isinstance(chosen, str) or chosen not in descriptor.roles:
        return None
    return binding_registry.role_implementation(chosen, serves)


def binding_has_roles(binding_key: str, descriptors: Mapping[str, BindingDescriptor] | None = None) -> bool:
    """Whether the description of ``binding_key`` selects a role, so a binding of it must resolve one to run."""
    known = _cached_shipped() if descriptors is None else descriptors
    descriptor = known.get(binding_key)
    return descriptor is not None and descriptor.role_field is not None


def listed_roles(descriptors: Mapping[str, BindingDescriptor] | None = None) -> Iterable[tuple[str, str]]:
    """Every ``(binding key, role id)`` the descriptors list."""
    known = _cached_shipped() if descriptors is None else descriptors
    for key, descriptor in known.items():
        for role_id in descriptor.roles:
            yield key, role_id


from gateway import binding_targets  # noqa: E402,F401  (registers the reviewed target checks)
