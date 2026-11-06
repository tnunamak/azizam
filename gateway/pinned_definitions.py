"""Read back what a job pinned, so a route compiles from the pin and not from a live file (S2 design, D2 "Use").

``gateway/job_envelope.py`` builds the record at accept time and ``gateway/definition_store.py`` keeps it. This module
reads it for a run: the contract, the binding and the template bytes that the job's envelope names. A run that uses the
pin never reads the workflow file or the registry again, so an edit or a delete after accept cannot change it. If the
job has no envelope, or a blob is missing or damaged, the read raises ``DefinitionUnavailable``. There is no fallback
to the current file.

The module names no workflow, model, engine or modality. A route says which binding key holds its graph; the shape of
the contract is whatever the route pinned.

Which routes compile from the pin is a setting, per route capability: ``workflow_execution.compile_from_pin.<capability>``
in the runtime config, read on each request (``compile_from_pin_enabled``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway import media_jobs
from gateway.definition_store import DefinitionUnavailable, envelope_in, read_blob_in
from gateway.workflow_envelope import Envelope

CONFIG_SECTION = "workflow_execution"
CONFIG_KEY = "compile_from_pin"


def compile_from_pin_enabled(config: Mapping[str, Any], capability: str) -> bool:
    """Whether jobs of the route ``capability`` compile from their pin. Only a literal ``true`` turns it on."""
    section = config.get(CONFIG_SECTION)
    flags = section.get(CONFIG_KEY) if isinstance(section, Mapping) else None
    return isinstance(flags, Mapping) and flags.get(capability) is True


@dataclass(frozen=True)
class PinnedDefinitions:
    """The definitions one job pinned, decoded. ``template`` is the raw bytes, or None if the job pinned none."""

    envelope: Envelope
    contract: Mapping[str, Any]
    binding: Mapping[str, Any]
    template: bytes | None

    def manifest(self, binding_key: str) -> dict[str, Any]:
        """The manifest the route compiles with: the pinned contract, and the pinned binding under ``binding_key``."""
        return {**self.contract, "bindings": {binding_key: dict(self.binding)}}

    def graph(self) -> dict[str, Any]:
        """The pinned template as a JSON object. A job that pinned no template, or one that is not an object, raises
        ``DefinitionUnavailable``."""
        if self.template is None:
            raise DefinitionUnavailable(f"job {self.envelope.source_id}: no template was pinned")
        try:
            graph = json.loads(self.template)
        except ValueError as exc:
            raise DefinitionUnavailable(f"job {self.envelope.source_id}: the pinned template is not JSON") from exc
        if not isinstance(graph, dict):
            raise DefinitionUnavailable(f"job {self.envelope.source_id}: the pinned template is not an object")
        return graph


def _blob(conn: sqlite3.Connection, sha: str) -> bytes:
    body = read_blob_in(conn, sha)
    if "sha256:" + hashlib.sha256(body).hexdigest() != sha:
        raise DefinitionUnavailable(f"definition {sha} is damaged")
    return body


def _mapping(body: bytes, sha: str) -> Mapping[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DefinitionUnavailable(f"definition {sha} is not JSON") from exc
    if not isinstance(data, dict):
        raise DefinitionUnavailable(f"definition {sha} is not an object")
    return data


def load_in(conn: sqlite3.Connection, job_id: str) -> PinnedDefinitions:
    """The job's pinned definitions, read on the caller's connection. Raises ``DefinitionUnavailable`` if the job has
    no envelope, a blob is missing or damaged, or a definition is not a JSON object."""
    envelope = envelope_in(conn, job_id)
    if envelope is None:
        raise DefinitionUnavailable(f"job {job_id} pinned no definitions")
    return PinnedDefinitions(
        envelope=envelope,
        contract=_mapping(_blob(conn, envelope.contract), envelope.contract),
        binding=_mapping(_blob(conn, envelope.binding), envelope.binding),
        template=_blob(conn, envelope.template) if envelope.template is not None else None,
    )


def load(job_id: str, path: Path) -> PinnedDefinitions:
    """``load_in`` on a connection to the jobs database at ``path``."""
    with media_jobs.connection(Path(path)) as conn:
        return load_in(conn, job_id)
