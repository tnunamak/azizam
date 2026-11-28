"""A runner runtime that runs a job from what it pinned, not from what is live now (S2 design, D1, D2 and D3).

``gateway/engine_protocol/jobs.py`` asks its runtime for the connection and the request of an operation. This runtime
answers both from the job's envelope:

- The request carries the pinned manifest (the pinned contract and the pinned binding, selectors included) and the
  pinned template. Nothing reads a registry or a workflow file, so an edit or a delete after accept changes nothing.
- The connection is the live one, checked against the pin first: the engine address, the protocol description's
  revision and the adapter's id and interface version must be the ones the job was accepted with. The envelope holds the
  address only as a digest, so a changed address cannot be followed; the run fails as ``definition_unavailable``.

Both are read on each call, so a restart that builds a new runtime for the same job meets the same pin and the same
checks. A pin that is missing, damaged or not a manifest raises ``DefinitionUnavailable`` and nothing is sent.

The module names no workflow, engine, model, intent or modality.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from gateway import job_envelope, media_jobs, pinned_definitions
from gateway.definition_store import DefinitionUnavailable
from gateway.engine_protocol.adapter import AdapterOrigin
from gateway.engine_protocol.description import ProtocolDescription
from gateway.engine_protocol.types import CanonicalRequest, Connection, ResultContext
from gateway.result_bundle import ResultLimits


def adapter_identity(protocol: ProtocolDescription) -> str:
    """The id a job pins for the code that runs it: the named adapter, or the built-in executor the description names."""
    return protocol.adapter_identity


def build_envelope(
    manifest: Mapping[str, Any], binding_key: str, *, template: bytes | None, protocol: ProtocolDescription, adapter: Any,
    service_id: str, origin: str, source_kind: str = "workflow",
) -> job_envelope.JobEnvelope:
    """What a job pins when it runs through this runtime: the manifest's contract fields, the one binding, the template
    bytes, the protocol description's revision, the adapter's identity and the engine's address (as a digest)."""
    return job_envelope.build_job_envelope(
        source_kind=source_kind, source_id=str(manifest["id"]),
        contract={key: manifest[key] for key in pinned_definitions.PINNED_MANIFEST_FIELDS if key in manifest},
        binding=dict(manifest["bindings"][binding_key]), template=template,
        protocol_id=protocol.id, protocol_revision=protocol.revision, adapter_id=adapter_identity(protocol),
        interface_version=adapter.interface_version, service_id=service_id, origin=origin,
    )


class PinnedRuntime:
    """The runner's runtime for one job. ``connection`` returns the live ``Connection`` of the engine now."""

    def __init__(
        self, *, job_id: str, adapter: Any, protocol: ProtocolDescription, connection: Callable[[], Connection],
        timing: tuple[float, float], jobs_path: Path | None = None, context: ResultContext | None = None,
    ) -> None:
        # The adapter must be the one built from this very description: an adapter built from another revision of the same
        # protocol would run that revision's request shape under this job's pin. One that cannot say what it was built
        # from is refused too.
        origin = getattr(adapter, "built_from", None)
        if origin != AdapterOrigin.of(protocol):
            raise DefinitionUnavailable(f"job {job_id}: the adapter was not built from the protocol description given")
        self.job_id = job_id
        self.adapter = adapter
        self._protocol = protocol
        self._origin = origin
        self._connection = connection
        self._timing = timing
        self._jobs_path = jobs_path
        self._context = context or ResultContext(ResultLimits())

    @property
    def wait_s(self) -> float:
        """The longest this job waits on its engine, queue time included: the wait of the service it was pinned to."""
        return float(self._timing[1])

    def _pin(self) -> pinned_definitions.PinnedDefinitions:
        return pinned_definitions.load(self.job_id, self._jobs_path or media_jobs.DB_PATH)

    def _check_identity(self, pin: pinned_definitions.PinnedDefinitions, conn: Connection) -> None:
        envelope = pin.envelope
        protocol = self._protocol
        if (envelope.protocol_id, envelope.protocol_revision) != (protocol.id, protocol.revision):
            raise DefinitionUnavailable(f"job {self.job_id}: the protocol description is not the one the job pinned")
        if (envelope.protocol_id, envelope.protocol_revision, envelope.adapter_id) != self._origin:
            raise DefinitionUnavailable(f"job {self.job_id}: the adapter was not built from the protocol description the job pinned")
        if (envelope.adapter_id, envelope.interface_version) != (adapter_identity(protocol), self.adapter.interface_version):
            raise DefinitionUnavailable(f"job {self.job_id}: the adapter is not the one the job pinned")
        if envelope.origin_digest != job_envelope.origin_digest(conn.address):
            raise DefinitionUnavailable(f"job {self.job_id}: the engine address is not the one the job pinned")

    def connection_for(self, _op: str) -> Connection:
        conn = self._connection()
        self._check_identity(self._pin(), conn)
        return conn

    def poll_timing(self, _op: str) -> tuple[float, float]:
        return self._timing

    def result_context(self) -> ResultContext:
        return self._context

    def request(self, op: str, body: Mapping[str, Any]) -> CanonicalRequest:
        pin = self._pin()
        self._check_identity(pin, self._connection())
        binding_key = self._protocol.binding
        selectors = self._protocol.result_selectors
        params: dict[str, Any] = {"manifest": pin.manifest(binding_key, {binding_key: selectors} if selectors is not None else {})}
        if pin.template is not None:
            params["template"] = pin.graph()
        return CanonicalRequest(op=op, body=copy.deepcopy(dict(body)), params=params)
