"""The job envelope: a typed, closed-grammar record of what a job pinned (S2 design, D1), and the restart table (D3).

The envelope holds definitions and identities only, never caller content. Caller inputs stay in the execution spool.
A value the gateway made (a generated input) is saved with the id of the reviewed generator that made it; the
caller of ``parse_envelope`` passes the reviewed ``generator id -> persistence class`` map, so this module names no
generator, engine, model or modality.

The grammar is closed: an unknown key, a wrong type or a malformed digest raises ``ValueError`` that names the field
path. ``Envelope.to_json`` and ``parse_envelope`` are inverse: ``parse_envelope(e.to_json(), generators=g) == e``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

ENVELOPE_VERSION = 2          # the version the writer writes
SUPPORTED_VERSIONS = (1, 2)   # the versions the reader accepts (docs/design/CODE_TO_DATA_S4_S8.md, 5.4 and 7.4)

SOURCE_KINDS = ("workflow", "offering")
# How a reviewed generator's value persists: kept, kept as a digest, or not kept at all.
PERSISTENCE_CLASSES = ("value", "digest", "none")

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class Envelope:
    envelope_version: int
    source_kind: str
    source_id: str
    source_revision: str | int
    contract: str
    binding: str
    template: str | None
    protocol_id: str
    protocol_revision: str | int
    adapter_id: str
    interface_version: int
    service_id: str
    origin_digest: str
    # {input name: {"generator": id, "value": ...}}; a "none" generator has no "value" key, and an erased entry has
    # {"generator": id, "erased": True} in its place. Read-only: the envelope
    # copies what it is given and hands out read-only views, so a held Envelope cannot change after it is checked.
    generated: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    bundle: str | None = None
    # Version 2 (section 7.4). A version 1 envelope has none of the three: ``incarnation`` and ``binding_key`` are None,
    # and nothing fills them from the live registry. ``record_revision`` is audit only; execution never reads it.
    incarnation: str | None = None
    binding_key: str | None = None
    record_revision: int | None = None

    def __post_init__(self) -> None:
        frozen = MappingProxyType({name: MappingProxyType(dict(entry)) for name, entry in self.generated.items()})
        object.__setattr__(self, "generated", frozen)

    def to_json(self) -> dict[str, Any]:
        definitions: dict[str, Any] = {"contract": self.contract, "binding": self.binding}
        if self.template is not None:
            definitions["template"] = self.template
        definitions["protocol"] = {"id": self.protocol_id, "revision": self.protocol_revision}
        source: dict[str, Any] = {"kind": self.source_kind, "id": self.source_id, "revision": self.source_revision}
        if self.envelope_version >= 2:
            source["incarnation"] = self.incarnation
            source["binding_key"] = self.binding_key
            source["record_revision"] = self.record_revision
        return {
            "envelope_version": self.envelope_version,
            "source": source,
            "definitions": definitions,
            "executor": {"adapter_id": self.adapter_id, "interface_version": self.interface_version},
            "upstream": {"service_id": self.service_id, "origin_digest": self.origin_digest},
            "generated": {name: dict(entry) for name, entry in self.generated.items()},
            "results": {"bundle": self.bundle},
        }


def definition_shas(envelope: Envelope) -> frozenset[str]:
    """Every blob sha the envelope references."""
    shas = {envelope.contract, envelope.binding}
    if envelope.template is not None:
        shas.add(envelope.template)
    return frozenset(shas)


def _mapping(value: Any, path: str, keys: set[str], optional: frozenset[str] = frozenset()) -> Mapping[str, Any]:
    """Check that ``value`` is a mapping whose keys are exactly ``keys`` (the ``optional`` ones may be absent)."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: expected an object, got {type(value).__name__}")
    unknown = sorted(str(k) for k in value if k not in keys)
    if unknown:
        raise ValueError(f"{path}: unknown key {unknown[0]!r}")
    for key in sorted(keys - optional):
        if key not in value:
            raise ValueError(f"{path}.{key}: required")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: expected a non-empty string")
    return value


def _integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path}: expected an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path}: expected an integer of at least {minimum}")
    return value


def _revision(value: Any, path: str) -> str | int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _text(value, path)


def _digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{path}: expected 'sha256:' followed by 64 lowercase hex digits")
    return value


def _generated(value: Any, generators: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    path = "generated"
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: expected an object, got {type(value).__name__}")
    parsed: dict[str, dict[str, Any]] = {}
    for name, entry in value.items():
        entry_path = f"{path}.{name}"
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: input names must be non-empty strings")
        entry = _mapping(entry, entry_path, {"generator", "value", "erased"}, frozenset({"value", "erased"}))
        generator = _text(entry["generator"], f"{entry_path}.generator")
        persistence = generators.get(generator)
        if persistence is None:
            raise ValueError(f"{entry_path}.generator: {generator!r} is not a reviewed generator")
        if persistence not in PERSISTENCE_CLASSES:
            raise ValueError(f"{entry_path}.generator: {generator!r} has unknown persistence class {persistence!r}")
        if persistence == "none":
            if entry.get("value") is not None:
                raise ValueError(f"{entry_path}.value: a generator with persistence 'none' keeps no value")
            parsed[name] = {"generator": generator}
        elif "erased" in entry:
            # Erasure emptied the value and left this mark, so a later read can tell it from a job that kept none.
            if entry["erased"] is not True or "value" in entry:
                raise ValueError(f"{entry_path}.erased: expected true, with no value")
            parsed[name] = {"generator": generator, "erased": True}
        elif persistence == "digest":
            if "value" not in entry:
                raise ValueError(f"{entry_path}.value: required")
            parsed[name] = {"generator": generator, "value": _digest(entry["value"], f"{entry_path}.value")}
        else:
            if "value" not in entry:
                raise ValueError(f"{entry_path}.value: required")
            kept = entry["value"]
            scalar = isinstance(kept, (int, float, str)) and not isinstance(kept, bool)
            if not scalar or (isinstance(kept, float) and not math.isfinite(kept)):
                raise ValueError(f"{entry_path}.value: expected a string, an integer or a finite number")
            parsed[name] = {"generator": generator, "value": kept}
    return parsed


def parse_envelope(data: Mapping[str, Any], generators: Mapping[str, str] | None = None) -> Envelope:
    """Parse the closed grammar, or raise ``ValueError`` naming the field path.

    ``generators`` maps each reviewed generator id to its persistence class (``value``, ``digest`` or ``none``).
    A generated entry whose generator is not in the map is refused; with no map, any generated entry is refused.
    """
    generators = generators or {}
    top = _mapping(
        data,
        "envelope",
        {"envelope_version", "source", "definitions", "executor", "upstream", "generated", "results"},
    )
    version = _integer(top["envelope_version"], "envelope_version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"envelope_version: expected one of {', '.join(str(v) for v in SUPPORTED_VERSIONS)}, got {version}")

    if version == 1:
        source = _mapping(top["source"], "source", {"kind", "id", "revision"})
    else:
        source = _mapping(top["source"], "source", {"kind", "id", "revision", "incarnation", "binding_key", "record_revision"},
                          frozenset({"record_revision"}))
    kind = source["kind"]
    if kind not in SOURCE_KINDS:
        raise ValueError(f"source.kind: expected one of {', '.join(SOURCE_KINDS)}")

    definitions = _mapping(
        top["definitions"], "definitions", {"contract", "binding", "template", "protocol"}, frozenset({"template"})
    )
    protocol = _mapping(definitions["protocol"], "definitions.protocol", {"id", "revision"})
    executor = _mapping(top["executor"], "executor", {"adapter_id", "interface_version"})
    upstream = _mapping(top["upstream"], "upstream", {"service_id", "origin_digest"})
    results = _mapping(top["results"], "results", {"bundle"})
    bundle = results["bundle"]
    if bundle is not None:
        bundle = _text(bundle, "results.bundle")

    return Envelope(
        envelope_version=version,
        source_kind=kind,
        source_id=_text(source["id"], "source.id"),
        source_revision=_revision(source["revision"], "source.revision"),
        contract=_digest(definitions["contract"], "definitions.contract"),
        binding=_digest(definitions["binding"], "definitions.binding"),
        template=_digest(definitions["template"], "definitions.template") if "template" in definitions else None,
        protocol_id=_text(protocol["id"], "definitions.protocol.id"),
        protocol_revision=_revision(protocol["revision"], "definitions.protocol.revision"),
        adapter_id=_text(executor["adapter_id"], "executor.adapter_id"),
        interface_version=_integer(executor["interface_version"], "executor.interface_version", minimum=1),
        service_id=_text(upstream["service_id"], "upstream.service_id"),
        origin_digest=_digest(upstream["origin_digest"], "upstream.origin_digest"),
        generated=_generated(top["generated"], generators),
        bundle=bundle,
        incarnation=_text(source["incarnation"], "source.incarnation") if version >= 2 else None,
        binding_key=_text(source["binding_key"], "source.binding_key") if version >= 2 else None,
        record_revision=(_integer(source["record_revision"], "source.record_revision", minimum=0)
                         if version >= 2 and source.get("record_revision") is not None else None),
    )


def classify_recovery(
    *, terminal: bool, submitting: bool, protocol_job: bool, bundle_committed: bool, spool_present: bool
) -> str:
    """The restart table (S2 design, D3). The first matching row wins, in this order.

    ``terminal`` is a job already in a terminal status (succeeded, failed or canceled): restart does nothing to it, and
    a replay of its bundle is a read, not a recovery action. ``spool_present`` means the execution spool still exists
    (it is gone after forget, member-off or the kill switch); it says nothing about durability.
    """
    if terminal:
        return "terminal"
    if bundle_committed:
        return "replay"
    if protocol_job:
        return "poll"
    if submitting:
        return "submission_unknown"
    if spool_present:
        return "resume"
    return "interrupted_before_submit"
