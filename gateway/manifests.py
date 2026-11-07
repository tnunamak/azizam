"""Workflow manifest loading, validation, and backend binding helpers."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
from fractions import Fraction
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

import yaml


class ManifestError(ValueError):
    """Raised when a manifest or backend binding is invalid."""


# ------------------------------------------------------------------------------------------------ contract errors
# Every refusal of a contract document carries a place (a path of keys and indexes) and a stable code. A check collects
# all its errors and raises them together (CODE_TO_DATA_S3, D8). ``str()`` of the raised error is still the message a
# check would have raised first, so a caller that catches ``ManifestError`` sees what it saw before.

MAX_CONTRACT_ERRORS = 100
TOO_MANY_ERRORS = "too_many_errors"

PathPart = str | int


def _pointer_part(part: PathPart) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class ContractError:
    """One refusal: where (``path``), which rule (``code``) and a short sentence (``message``)."""

    path: tuple[PathPart, ...]
    code: str
    message: str

    @property
    def pointer(self) -> str:
        """The path as a JSON Pointer, for display."""
        return "".join("/" + _pointer_part(part) for part in self.path)

    def as_dict(self) -> dict[str, Any]:
        return {"path": list(self.path), "pointer": self.pointer, "code": self.code, "message": self.message}


def _sort_key(error: ContractError) -> tuple[tuple[int, int, str], ...]:
    # Indexes sort by number and keys by text; the two never compare with each other.
    return tuple((0, part, "") if isinstance(part, int) else (1, 0, str(part)) for part in error.path)


class ContractErrors(ManifestError):
    """All the errors of one check. ``errors`` is sorted by path and holds at most ``MAX_CONTRACT_ERRORS`` entries and a
    final ``too_many_errors`` entry. ``primary`` is the error the check found first, in the order of its checks; the
    message of the exception, and so its ``str()``, is the message of ``primary``."""

    def __init__(self, errors: Iterable[ContractError], primary: ContractError | None = None, *, final: bool = False) -> None:
        """``final`` says the list is already sorted and capped (a report's): it is kept as it is."""
        found = list(errors)
        if not found:
            raise ValueError("ContractErrors needs at least one error")
        self.primary = primary if primary is not None else found[0]
        ordered = found if final else sorted(found, key=_sort_key)
        if len(ordered) > MAX_CONTRACT_ERRORS and not final:
            dropped = len(ordered) - MAX_CONTRACT_ERRORS
            ordered = ordered[:MAX_CONTRACT_ERRORS]
            ordered.append(ContractError((), TOO_MANY_ERRORS, f"{dropped} more errors are not listed"))
        self.errors = ordered
        # Not ``super().__init__``: a subclass may also inherit an exception that takes other arguments.
        ValueError.__init__(self, self.primary.message)

    @property
    def code(self) -> str:
        return self.primary.code

    @property
    def path(self) -> tuple[PathPart, ...]:
        return self.primary.path

    def as_list(self) -> list[dict[str, Any]]:
        return [error.as_dict() for error in self.errors]


class ErrorCollector:
    """Gathers the errors of a check, in the order the check finds them."""

    def __init__(self) -> None:
        self.errors: list[ContractError] = []

    def add(self, path: Iterable[PathPart], code: str, message: str) -> None:
        self.errors.append(ContractError(tuple(path), code, message))

    def __len__(self) -> int:
        return len(self.errors)

    def raise_if_any(self, factory: Callable[[list[ContractError]], ContractErrors] = ContractErrors) -> None:
        if self.errors:
            raise factory(self.errors)

    @contextlib.contextmanager
    def stop_at_a_crash_after_an_error(self) -> Iterator[None]:
        """A check that keeps going after an error can reach a value that the error already refused, and crash on it.
        The check used to stop at its first error, so that crash is an effect of an error already listed: the errors
        collected so far stand, and the crash is dropped. A crash with no error before it is a real bug and is raised."""
        try:
            yield
        except (TypeError, AttributeError, KeyError, IndexError):
            if not self.errors:
                raise


def refusal(path: Iterable[PathPart], code: str, message: str) -> ContractErrors:
    """A single-error ``ContractErrors``, for a check that has nothing to gather."""
    return ContractErrors([ContractError(tuple(path), code, message)])


# The input types that carry media: a value that names a stored or supplied file, not a plain value.
MEDIA_INPUT_TYPES = frozenset({"asset", "image"})


@dataclass
class InputValidationResult:
    accepted: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(frozen=True)
class Provenance:
    """Where a workflow record came from and whether its owner reviewed it (CODE_TO_DATA_S3, D2). The loader sets it from
    the origin of the record; a document never says it."""

    trusted: bool
    source: str | None


# What the release's own files are: reviewed, and local.
BUNDLED_PROVENANCE = Provenance(trusted=True, source="local")
# A record whose origin is not known is not reviewed.
UNKNOWN_PROVENANCE = Provenance(trusted=False, source=None)

# The one version of the workflow document the validator reads. A document in any other version is refused.
SUPPORTED_SCHEMA_VERSIONS = (2,)
SCHEMA_VERSION_MESSAGE = (
    "this schema version is not supported; convert the document with scripts/workflow_contract.py convert")


@dataclass(frozen=True)
class ManifestIdentity:
    id: str
    version: str | None
    manifest_hash: str
    trusted: bool
    source: str | None


@dataclass
class BackendBindingResult:
    """Result of applying validated inputs to a backend-specific request.

    Carries enough provenance for a route receipt without exposing the raw
    request body payload itself outside of the ``request`` field.
    """

    request: dict[str, Any]
    backend: str
    identity: ManifestIdentity
    applied_inputs: list[str] = field(default_factory=list)
    ignored_inputs: dict[str, str] = field(default_factory=dict)
    backend_metadata: dict[str, Any] = field(default_factory=dict)

    def receipt_provenance(self) -> dict[str, Any]:
        """Provenance summary safe to surface on a route receipt."""
        return {
            "backend": self.backend,
            "manifest_id": self.identity.id,
            "manifest_version": self.identity.version,
            "manifest_hash": self.identity.manifest_hash,
            "manifest_trusted": self.identity.trusted,
            "manifest_source": self.identity.source,
            "applied_inputs": list(self.applied_inputs),
            "ignored_inputs": dict(self.ignored_inputs),
            "backend_metadata": dict(self.backend_metadata),
        }


def load_workflow_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    if not isinstance(manifest, dict):
        raise ManifestError(f"manifest {manifest_path} must be a mapping")
    enforce_contract(manifest)
    return manifest


def enforce_contract(manifest: Any) -> None:
    """What a loader (and a save) refuses a workflow document for, all in one raise: the manifest checks, the schema 2
    contract (closed structure, typed inputs, outputs, generators) and the bindings against their adapters' descriptions.
    A schema 1 document is refused with ``schema_version_unsupported``; nothing converts it here (CODE_TO_DATA_S3, D3)."""
    from gateway import binding_descriptors
    from gateway.workflow_contract import check_contract

    validate_manifest(manifest, binding_descriptors=binding_descriptors.cached_shipped_binding_descriptors())
    check_contract(manifest).raise_if_any()


# Wording shown to a person that has no part in what a workflow does. Changing it is not a new workflow, so it stays out
# of the hash that receipts record.
_PRESENTATION_ONLY_KEYS = frozenset({"prompt_hint"})


def manifest_identity(manifest: dict[str, Any], provenance: Provenance = UNKNOWN_PROVENANCE) -> ManifestIdentity:
    """The identity of a document: its hash, and the provenance the caller gives (the document holds none)."""
    validate_manifest(manifest)
    identity_fields = {key: value for key, value in manifest.items() if key not in _PRESENTATION_ONLY_KEYS}
    encoded = json.dumps(identity_fields, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return ManifestIdentity(
        id=str(manifest["id"]),
        version=manifest.get("version"),
        manifest_hash=hashlib.sha256(encoded).hexdigest(),
        trusted=provenance.trusted,
        source=provenance.source,
    )


# A result's name is one URL path segment of the job's result route, so it holds only characters that survive a round
# trip through a path unchanged: no "/", no "%", no dot segment. The result store applies the same rule.
RESULT_NAME_RULE = "a result name starts with a letter or digit and holds only letters, digits, '.', '_' and '-' (at most 128)"
RESULT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_manifest(
    manifest: dict[str, Any], descriptors: Mapping[str, Any] | None = None, *,
    binding_descriptors: Mapping[str, Any] | None = None,
) -> None:
    """Check a manifest. ``descriptors`` are the selector descriptors by binding key to check selectors against; the
    default is the shipped registry. A run from a pin passes the descriptor of the protocol it was started with.
    ``binding_descriptors`` (binding key to ``BindingDescriptor``), when given, also close every binding block against its
    adapter's description; ``enforce_contract`` (the loaders' check) passes the shipped ones.

    Every error is collected. The raised ``ContractErrors`` has them all, each with a path and a code; its message is
    the first one found, in the order of the checks below."""
    from gateway import output_selectors

    errors = ErrorCollector()
    selector_start: list[int] = []
    with errors.stop_at_a_crash_after_an_error():
        collect_manifest_errors(manifest, errors, descriptors, selector_start, binding_descriptors)
    # A manifest whose first problem is a selector is still refused with a ``SelectorError``, as it was.
    first_is_selector = selector_start[:1] == [0] and (len(selector_start) == 1 or selector_start[1] > 0) and len(errors) > 0
    errors.raise_if_any(output_selectors.SelectorErrors if first_is_selector else ContractErrors)


def collect_manifest_errors(
    manifest: dict[str, Any],
    errors: ErrorCollector,
    descriptors: Mapping[str, Any] | None = None,
    selector_start: list[int] | None = None,
    binding_descriptors: Mapping[str, Any] | None = None,
) -> None:
    """Add every error of the manifest to ``errors``. ``selector_start``, when given, receives how many errors there were
    before the selector check began, then how many there were after it. ``binding_descriptors``, when given, adds the binding closure errors after all others."""
    if not isinstance(manifest, Mapping):
        # As before: a root that is not a mapping is refused for its first missing field (or, if it holds every field
        # name, for its type). Nothing below can read it.
        missing = next((name for name in ("schema_version", "id", "intent", "inputs", "outputs", "bindings")
                        if not _holds(manifest, name)), None)
        if missing is None:
            errors.add((), "type_mismatch", "manifest must be a mapping")
        else:
            errors.add((missing,), "required_field_missing", f"manifest missing required field: {missing}")
        return
    if "schema_version" in manifest:
        version = manifest["schema_version"]
        if isinstance(version, bool) or version not in SUPPORTED_SCHEMA_VERSIONS:
            # The rest would be read against a grammar this document does not use.
            errors.add(("schema_version",), "schema_version_unsupported", SCHEMA_VERSION_MESSAGE)
            return
    for field_name in ("schema_version", "id", "intent", "inputs", "outputs", "bindings"):
        if field_name not in manifest:
            errors.add((field_name,), "required_field_missing", f"manifest missing required field: {field_name}")
    inputs = manifest.get("inputs")
    bindings = manifest.get("bindings")
    outputs = manifest.get("outputs")
    if "inputs" in manifest and not isinstance(inputs, dict):
        errors.add(("inputs",), "type_mismatch", "manifest inputs must be a mapping")
    if "bindings" in manifest and not isinstance(bindings, dict):
        errors.add(("bindings",), "type_mismatch", "manifest bindings must be a mapping")
    if isinstance(outputs, dict):
        for name in outputs:
            if not isinstance(name, str) or not RESULT_NAME_RE.fullmatch(name):
                errors.add(("outputs", str(name)), "result_name_invalid", f"output {name!r}: {RESULT_NAME_RULE}")
    if "purpose" in manifest:
        purpose = manifest["purpose"]
        if not isinstance(purpose, str) or not purpose.strip():
            errors.add(("purpose",), "value_empty", "manifest purpose must be a non-empty string")
        elif len(purpose.split()) > 8:
            errors.add(("purpose",), "too_many_words", "manifest purpose must contain at most 8 words")
    for field_name in ("summary", "license", "prompt_hint"):
        if field_name in manifest:
            value = manifest[field_name]
            if not isinstance(value, str) or not value.strip():
                errors.add((field_name,), "value_empty", f"manifest {field_name} must be a non-empty string")
    # The length rule comes after all three text checks, in the order the check has always used.
    summary = manifest.get("summary")
    if isinstance(summary, str) and summary.strip() and len(summary.split()) > 40:
        errors.add(("summary",), "too_many_words", "manifest summary must contain at most 40 words")
    if isinstance(inputs, dict):
        for name, spec in inputs.items():
            if not isinstance(spec, dict):
                continue
            for key in ("label", "help"):
                if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
                    errors.add(("inputs", name, key), "value_empty", f"input {name} {key} must be a non-empty string")
            if spec.get("type") in ("number", "integer") and "default" in spec:
                if isinstance(spec["default"], float) and not math.isfinite(spec["default"]):
                    errors.add(("inputs", name, "default"), "number_not_finite", f"input {name} default must be a finite number")
    if isinstance(bindings, dict):
        for backend, block in bindings.items():
            if not isinstance(block, dict) or "edge_only_inputs" not in block:
                continue
            edge_only = block["edge_only_inputs"]
            # With broken inputs there is nothing to name; that error is already listed.
            if not isinstance(edge_only, list) or (
                isinstance(inputs, dict) and not all(isinstance(name, str) and name in inputs for name in edge_only)
            ):
                errors.add(("bindings", backend, "edge_only_inputs"), "reference_unknown",
                           f"binding {backend} edge_only_inputs must name declared inputs")
    # A binding that declares output selectors is checked against its adapter's descriptor (CODE_TO_DATA_S2, D8).
    from gateway import output_selectors

    if selector_start is not None:
        selector_start.append(len(errors))
    output_selectors.collect_manifest_errors(manifest, errors, descriptors)
    if selector_start is not None:
        selector_start.append(len(errors))   # with the entry above: how many errors the selector check added
    if binding_descriptors is not None:
        from gateway import binding_descriptors as bindings_module

        bindings_module.collect_binding_errors(manifest, errors, binding_descriptors)


def _holds(value: Any, name: str) -> bool:
    """``name in value`` for any value; a value that cannot answer does not hold it."""
    try:
        return name in value
    except TypeError:
        return False


def validate_inputs(
    manifest: dict[str, Any],
    supplied: dict[str, Any],
) -> InputValidationResult:
    validate_manifest(manifest)
    specs = manifest["inputs"]
    result = InputValidationResult()

    for name, value in supplied.items():
        spec = specs.get(name)
        if spec is None:
            result.rejected[name] = "unknown input"
            continue
        reason = validate_input_value(name, value, spec)
        if reason is None:
            result.accepted[name] = value
        else:
            result.rejected[name] = reason

    for name, spec in specs.items():
        if spec.get("required") and name not in supplied:
            result.rejected[name] = "required input missing"

    return result


DEPLOYMENT_INPUT_KEYS = ("default", "minimum", "maximum")


def apply_deployment_inputs(manifest: dict[str, Any], narrowing: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The manifest with a deployment's narrowing applied to its declared numeric inputs: one effective input spec.

    ``narrowing`` maps an input name to any of ``default``, ``minimum`` and ``maximum``. A deployment may only tighten the
    manifest's bounds: a minimum below the manifest's, or a maximum above it, is refused. The default must lie inside the
    resulting bounds. Everything is checked here, at load, so a bad deployment never reaches a request. Returns a new
    manifest; the argument is not modified.
    """
    validate_manifest(manifest)
    inputs = copy.deepcopy(manifest["inputs"])
    errors = ErrorCollector()
    with errors.stop_at_a_crash_after_an_error():
        for name, limits in narrowing.items():
            _narrow_input(name, limits, inputs, errors)
    errors.raise_if_any()
    effective = copy.deepcopy(manifest)
    effective["inputs"] = inputs
    return effective


def _narrow_input(name: str, limits: Mapping[str, Any], inputs: dict[str, Any], errors: ErrorCollector) -> None:
    """Apply one input's narrowing to ``inputs``. The first problem of an input is its error; the next input is still checked."""
    at = ("inputs", name)
    spec = inputs.get(name)
    if not isinstance(spec, dict):
        errors.add(at, "input_unknown", f"deployment narrows input {name}, which the manifest does not declare")
        return
    if spec.get("type") not in ("number", "integer"):
        errors.add(at, "input_not_numeric", f"deployment narrows input {name}, which is not a number")
        return
    if not isinstance(limits, Mapping):
        errors.add(at, "type_mismatch", f"deployment narrowing for input {name} must be a mapping")
        return
    unknown = sorted(set(limits) - set(DEPLOYMENT_INPUT_KEYS))
    if unknown:
        errors.add(at, "limit_unknown", f"deployment sets {unknown} for input {name}; only {list(DEPLOYMENT_INPUT_KEYS)} apply")
        return
    for key, value in limits.items():
        # Python ints are exact and finite; only a float can be NaN or infinite (an int too large for a float is fine).
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
            errors.add(at + (key,), "number_not_finite", f"deployment value {key} for input {name} must be a finite number")
            return
    if "minimum" in limits:
        if "minimum" in spec and limits["minimum"] < spec["minimum"]:
            errors.add(at + ("minimum",), "bound_widened", f"deployment minimum for {name} is below the manifest's {spec['minimum']}")
            return
        spec["minimum"] = limits["minimum"]
    if "maximum" in limits:
        if "maximum" in spec and limits["maximum"] > spec["maximum"]:
            errors.add(at + ("maximum",), "bound_widened", f"deployment maximum for {name} is above the manifest's {spec['maximum']}")
            return
        spec["maximum"] = limits["maximum"]
    if "default" in limits:
        spec["default"] = limits["default"]
    if "minimum" in spec and "maximum" in spec and spec["minimum"] > spec["maximum"]:
        errors.add(at, "bounds_empty", f"effective bounds for {name} are empty: {spec['minimum']} > {spec['maximum']}")
        return
    if "default" in spec:
        reason = _validate_value(name, spec["default"], spec)
        if reason is not None:
            errors.add(at + ("default",), "default_out_of_bounds", f"default for {name} is outside its effective bounds: {reason}")


def apply_backend_binding(
    manifest: dict[str, Any],
    backend: str,
    backend_request: dict[str, Any],
    accepted_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Apply validated portable inputs to a backend-specific request copy.

    Backward-compatible thin wrapper around :func:`bind_backend_request`.
    """
    return bind_backend_request(manifest, backend, backend_request, accepted_inputs).request


def bind_backend_request(
    manifest: dict[str, Any],
    backend: str,
    backend_request: dict[str, Any],
    accepted_inputs: dict[str, Any],
    *,
    identity: ManifestIdentity | None = None,
) -> BackendBindingResult:
    """Apply validated inputs and return a receipt-ready binding result.

    Accepted inputs with no mapping for the chosen backend are recorded as
    ``ignored`` with a reason. Malformed maps fail fast via :class:`ManifestError`.
    The source ``backend_request`` is not mutated.
    """
    identity = identity if identity is not None else manifest_identity(manifest)
    backend_binding = manifest["bindings"].get(backend)
    if not isinstance(backend_binding, dict):
        raise refusal(("bindings", backend), "binding_missing", f"manifest has no binding for backend: {backend}")

    binding_map = backend_binding.get("map", {})
    if not isinstance(binding_map, dict):
        raise refusal(("bindings", backend, "map"), "type_mismatch", f"manifest binding for {backend} has invalid map")

    request = copy.deepcopy(backend_request)
    applied: list[str] = []
    ignored: dict[str, str] = {}

    for input_name, value in accepted_inputs.items():
        # Each mapping gets its own copy, so the request does not share a caller's mutable value.
        value = copy.deepcopy(value)
        if input_name not in binding_map:
            ignored[input_name] = f"no mapping for input under backend {backend}"
            continue
        mappings = binding_map[input_name]
        if not isinstance(mappings, list):
            raise refusal(("bindings", backend, "map", input_name), "type_mismatch", f"binding map for {input_name} must be a list")
        if not mappings:
            ignored[input_name] = f"empty mapping for input under backend {backend}"
            continue
        for index, mapping in enumerate(mappings):
            _apply_mapping(request, mapping, value, ("bindings", backend, "map", input_name, index))
        applied.append(input_name)

    backend_metadata = _backend_metadata(backend_binding)

    return BackendBindingResult(
        request=request,
        backend=backend,
        identity=identity,
        applied_inputs=applied,
        ignored_inputs=ignored,
        backend_metadata=backend_metadata,
    )


def _backend_metadata(backend_binding: dict[str, Any]) -> dict[str, Any]:
    """Extract non-payload binding facts safe for route receipts."""
    metadata: dict[str, Any] = {}
    for key, value in backend_binding.items():
        if key == "map":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            metadata[key] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            metadata[key] = list(value)
    return metadata


def validate_input_value(name: str, value: Any, spec: dict[str, Any]) -> str | None:
    """Validate one value against the manifest input-spec subset."""
    return _validate_value(name, value, spec)


def exact_number(value: Any) -> Fraction | None:
    """A JSON number as an exact Fraction with decimal meaning: an int is itself, a finite float is its shortest
    round-trip decimal (1.1 is 11/10, what the manifest author wrote). A bool, non-finite float or non-number is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float) and math.isfinite(value):
        return Fraction(repr(value))
    return None


def on_grid(value: Any, step: Fraction, offset: Fraction) -> bool:
    """True when (value - offset) / step is a whole number. The one grid test that input validation and the
    workflow contract's generator feasibility share, so they agree by construction."""
    exact = exact_number(value)
    return exact is not None and ((exact - offset) / step).denominator == 1


def _validate_value(name: str, value: Any, spec: dict[str, Any]) -> str | None:
    expected_type = spec.get("type", "string")
    if expected_type == "string":
        if not isinstance(value, str):
            return f"{name} must be a string"
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} must be a number"
        # Only a float can be NaN or infinite. math.isfinite on a huge int would raise OverflowError instead of refusing.
        if isinstance(value, float) and not math.isfinite(value):
            return f"{name} must be a finite number"
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer"
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{name} must be a boolean"
    elif isinstance(expected_type, str) and expected_type in MEDIA_INPUT_TYPES:
        # An image input is a media reference, like an asset: a data URI, URL or gateway reference.
        if not isinstance(value, (str, dict)):
            return f"{name} must be an asset reference"
    else:
        return f"{name} has unsupported type: {expected_type}"

    enum = spec.get("enum")
    if enum is not None and value not in enum:
        return f"{name} must be one of: {', '.join(map(str, enum))}"

    if expected_type in {"number", "integer"}:
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and value < minimum:
            return f"{name} must be >= {minimum}"
        if maximum is not None and value > maximum:
            return f"{name} must be <= {maximum}"
        # Some engines accept only a grid of values, e.g. frames = step*k+offset.
        # This is data on the input spec, not knowledge about a particular model.
        step = spec.get("step")
        if step is not None:
            step_value = exact_number(step)
            if step_value is None or step_value <= 0:
                return f"{name} has an invalid step"
            offset = spec.get("offset", 0)
            offset_value = exact_number(offset)
            if offset_value is None:
                return f"{name} has an invalid offset"
            if not on_grid(value, step_value, offset_value):
                if offset:
                    return f"{name} must be {offset} plus a multiple of {spec['step']}"
                return f"{name} must be a multiple of {spec['step']}"

    return None


def _apply_mapping(request: dict[str, Any], mapping: dict[str, Any], value: Any, at: tuple[PathPart, ...]) -> None:
    if not isinstance(mapping, dict):
        raise refusal(at, "type_mismatch", "binding mapping must be a mapping")

    has_node = "node" in mapping
    has_input = "input" in mapping
    if has_node ^ has_input:
        raise refusal(at, "mapping_incomplete", "binding mapping needs both node and input, or path")

    if has_node and has_input:
        node_id = str(mapping["node"])
        input_name = str(mapping["input"])
        try:
            request[node_id]["inputs"][input_name] = value
        except KeyError as exc:
            raise refusal(at, "target_missing", f"ComfyUI binding target missing: node {node_id} input {input_name}") from exc
        return

    path = mapping.get("path")
    if path:
        if not isinstance(path, str):
            raise refusal(at + ("path",), "type_mismatch", "binding mapping path must be a dotted string")
        _set_path(request, path.split("."), value, at + ("path",))
        return

    raise refusal(at, "mapping_incomplete", "binding mapping needs node/input or path")


def _set_path(target: dict[str, Any], parts: list[str], value: Any, at: tuple[PathPart, ...]) -> None:
    if not parts:
        raise refusal(at, "path_invalid", "binding path cannot be empty")
    cursor: Any = target
    for part in parts[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor.setdefault(part, {})
        else:
            raise refusal(at, "path_invalid", f"binding path crosses non-mapping value at {part}")
    if not isinstance(cursor, dict):
        raise refusal(at, "path_invalid", "binding path target is not a mapping")
    cursor[parts[-1]] = value
