"""Build the record of what a job pinned (S2 design, D1 and D2), from definitions the caller already holds.

A ``JobEnvelope`` is an ``Envelope`` plus the blob bytes it names. ``media_jobs.create`` writes both in the job's own
transaction. This module is pure: it reads no file, no database and no clock, and it names no workflow, model,
engine or modality. A caller hands it the definition mappings (the contract, the binding), the template bytes and
the identities; it returns the record, or raises ``ValueError``.

Nothing here accepts caller content. The inputs are definitions (owner or shipped data) and identities. The
privacy canary test (``tests/test_job_envelope_privacy.py``) holds that line.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gateway.workflow_envelope import ENVELOPE_VERSION, Envelope, parse_envelope

# The engine protocol descriptions carry no revision field yet (S2 design, D9). Until they do, the envelope records
# this marker, so a reader can tell "not revisioned" from a real revision.
UNREVISIONED = "unrevisioned"


@dataclass(frozen=True)
class JobEnvelope:
    """An envelope and the definition blobs it references (contract, binding and, if any, template)."""

    envelope: Envelope
    blobs: tuple[bytes, ...]


def canonical_bytes(data: Mapping[str, Any]) -> bytes:
    """One byte string for one mapping: sorted keys, no spaces. A value that JSON cannot hold raises ``ValueError``."""
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"definition cannot be written as JSON: {exc}") from exc


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def build_job_envelope(
    *,
    source_kind: str,
    source_id: str,
    contract: Mapping[str, Any],
    binding: Mapping[str, Any],
    template: bytes | None,
    protocol_id: str,
    protocol_revision: str | int = UNREVISIONED,
    adapter_id: str,
    interface_version: int,
    service_id: str,
    origin: str,
) -> JobEnvelope:
    """The envelope for one job. ``source_revision`` is the digest of the contract and binding together, so it
    changes when either does. ``origin`` is digested, never stored. Raises ``ValueError`` if the grammar refuses it."""
    contract_body = canonical_bytes(contract)
    binding_body = canonical_bytes(binding)
    blobs = [contract_body, binding_body]
    definitions: dict[str, Any] = {
        "contract": _digest(contract_body),
        "binding": _digest(binding_body),
        "protocol": {"id": protocol_id, "revision": protocol_revision},
    }
    if template is not None:
        blobs.append(template)
        definitions["template"] = _digest(template)
    revision = _digest(canonical_bytes({"contract": definitions["contract"], "binding": definitions["binding"]}))
    envelope = parse_envelope(
        {
            "envelope_version": ENVELOPE_VERSION,
            "source": {"kind": source_kind, "id": source_id, "revision": revision},
            "definitions": definitions,
            "executor": {"adapter_id": adapter_id, "interface_version": interface_version},
            "upstream": {"service_id": service_id, "origin_digest": _digest(origin.encode("utf-8"))},
            "generated": {},
            "results": {"bundle": None},
        }
    )
    return JobEnvelope(envelope=envelope, blobs=tuple(blobs))
