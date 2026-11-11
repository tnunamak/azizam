"""A public media job that runs an operation an owner's engine description declares.

The static intent table (registry/media_job_intents.yml) names the intents the gateway ships. An owner can add a kind
of engine with a description alone; the operations it declares are then jobs too. An intent outside the table is such a
job only when an enabled protocol engine's description declares it and the model names that engine's offering
(``dispatch.resolve`` checks both, including that the offering lists the task). Declared operations never enter the
table, so every published listing stays as it was.

The job runs on the http-v1 job runner (``engine_protocol.jobs.run``). The fetched file becomes the job's one asset;
the content route serves it as it is.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway import media_jobs
from gateway.engine_protocol import dispatch
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.errors import ProtocolError, UnknownOperation

MODE = "declared_operation"


def resolve(registry: Mapping[str, Any], intent: str, model: Any) -> dispatch.Target | None:
    """The target for a declared operation, or None when no enabled engine declares it for this model.

    Only a job operation counts: the runner needs the description's submit, poll and fetch steps."""
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


async def run(runtime: dispatch.Runtime, intent: str, body: Mapping[str, Any], *, job_id: str) -> None:
    """Run the job and keep the fetched file as its asset. ``body`` is the caller's input object; the description's
    submit map decides which of its fields reach the engine."""
    refs = await protocol_jobs.run(runtime, intent, body, job_id=job_id)
    if len(refs or ()) != 1:
        raise ProtocolError("The engine must return exactly one result file.", code="malformed_response")
    ref = refs[0]
    media_jobs.update_status(
        job_id, media_jobs.STATUS_SUCCEEDED, progress=100, path=media_jobs.DB_PATH,
        add_assets=[{"asset_id": ref.id, "kind": "file", "media_type": ref.content_type, "size_bytes": ref.size}])
    media_jobs.append_event(job_id, "succeeded", {"status": "succeeded"}, path=media_jobs.DB_PATH)
