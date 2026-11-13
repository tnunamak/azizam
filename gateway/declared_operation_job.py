"""A public media job that runs an operation an owner's engine description declares.

The static intent table (registry/media_job_intents.yml) names the intents the gateway ships. An owner can add a kind
of engine with a description alone; the operations it declares are then jobs too. An intent outside the table is such a
job only when an enabled protocol engine's description declares it and the model names that engine's offering
(``dispatch.resolve`` checks both, including that the offering lists the task). Declared operations never enter the
table. They are listed from the engine records on every read (``declared``): the public models listing, the OpenAPI
document and a refusal's supported intents name them while an enabled engine declares them, and with no such engine
those three are what the table alone gives.

The job runs on the versioned job runner (``engine_protocol.jobs.run``). The wrapper publishes a version 1 runner's
single fetched file; version 2 bundles and terminal replays already belong to the runner's completion transaction.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway import media_job_intents, media_jobs
from gateway.capabilities import Capability
from gateway.engine_contract_view import connection_config
from gateway.engine_protocol import description, dispatch
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.errors import ProtocolError, UnknownOperation
from gateway.engine_protocol.types import ResultBundle

MODE = "declared_operation"


def resolve(registry: Mapping[str, Any], intent: str, model: Any) -> dispatch.Target | None:
    """The target for a declared operation, or None when no enabled engine declares it for this model.

    Only a job operation counts: the runner needs the description's submit, poll and fetch steps."""
    if any(row.intent == intent for row in media_job_intents.load_rows()):
        return None
    try:
        target = dispatch.resolve(registry, model, intent)
    except UnknownOperation:
        return None
    except ProtocolError as error:
        if error.code == "engine_disabled":
            return None
        raise
    if target is None or target.description.operations[intent].job is None:
        return None
    return target


def declared(registry: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Every declared job operation an enabled engine serves, as ``(engine id, offering id, operation)``.

    Read from the engine records and the installed descriptions, per call, so an engine turned on or off elsewhere shows
    at once. An operation the static table already names is not listed here; ``resolve`` decides each triple, so this
    list and the job route agree. With no enabled protocol engine the result is empty."""
    found: list[tuple[str, str, str]] = []
    docs: dict[str, description.ProtocolDescription] | None = None
    for engine_id, service in (registry.get("services") or {}).items():
        if not dispatch.opted_in(service) or service.get("enabled", True) is False:
            continue
        if docs is None:
            docs = description.load_registry()
        doc = docs.get(connection_config(service).get("protocol"))
        if doc is None:
            continue
        for offering in dispatch.offerings_of(str(engine_id), service):
            for operation in doc.operations:
                if operation in media_job_intents.SUBMITTABLE_INTENTS:
                    continue
                try:
                    target = resolve(registry, operation, offering.get("id"))
                except ProtocolError:
                    continue  # an ambiguous or refused record is not offered; the job route refuses it the same way
                if target is not None and target.engine_id == str(engine_id):
                    found.append((str(engine_id), str(offering["id"]), operation))
    return found


def intents(registry: Mapping[str, Any]) -> list[str]:
    """The declared operations as submit intents, sorted and without repeats."""
    return sorted({operation for _engine, _model, operation in declared(registry)})


def capabilities(registry: Mapping[str, Any]) -> list[Capability]:
    """Project declared jobs for the member catalog, without inferring modalities.

    Installed descriptions currently declare no input/output modalities; leave them empty so presentation uses the
    unclassified vocabulary rather than guessing from the operation's name.
    """
    return [Capability(intent=operation, backend=engine_id, model=model, execution="async")
            for engine_id, model, operation in declared(registry)]


def model_rows(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One public models-listing row per offering that serves a declared operation."""
    rows: dict[str, dict[str, Any]] = {}
    for engine_id, model, operation in declared(registry):
        row = rows.setdefault(model, {"id": model, "object": "model", "created": 0, "owned_by": engine_id,
                                      "azizam_operations": []})
        row["azizam_operations"].append(operation)
    return [{**row, "azizam_operations": sorted(row["azizam_operations"])} for row in rows.values()]


async def run(runtime: dispatch.Runtime, intent: str, body: Mapping[str, Any], *, job_id: str) -> None:
    """Run the job and keep the fetched file as its asset. ``body`` is the caller's input object; the description's
    submit map decides which of its fields reach the engine."""
    refs = await protocol_jobs.run(runtime, intent, body, job_id=job_id)
    if refs is None or isinstance(refs, ResultBundle):
        # A terminal replay or a committed bundle has no second completion owner.
        return
    if len(refs) != 1:
        raise ProtocolError("The engine must return exactly one result file.", code="malformed_response")
    ref = refs[0]
    media_jobs.update_status(
        job_id, media_jobs.STATUS_SUCCEEDED, progress=100, path=media_jobs.DB_PATH,
        add_assets=[{"asset_id": ref.id, "kind": "file", "media_type": ref.content_type, "size_bytes": ref.size}])
    media_jobs.append_event(job_id, "succeeded", {"status": "succeeded"}, path=media_jobs.DB_PATH)
