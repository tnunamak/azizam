"""The owner API of the workflow registry (CODE_TO_DATA_S4_S8, I6): routes under ``/admin/api/workflows`` over the
``WorkflowRegistryStore``. The ETag is the registry's ``"os-N"`` generation; a write needs ``If-Match`` (a create does not);
a stale one is 412 ``generation_mismatch`` naming the current revision. A refused save is 422 with every problem and its
path. A workflow contract holds no secret, so the bodies are plain JSON of the ``workflow_owner`` response class."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway import egress, workflow_policy
from gateway.capabilities import EXPOSURE_CLASSES, RESOURCE_TIERS, TRUST_TIERS
from gateway.session_guard import SessionEnded
from gateway.workflow_registry_store import (
    BundledWorkflowRemoved,
    RegistryNotApplied,
    WorkflowIdIsBundled,
    WorkflowRegistryStore,
)
from gateway.workflow_runtime_types import (
    GENERATION_MISMATCH, ID_CLASH, KEEP, Actor, Commit, RegistryConflict, RegistryRefused, WorkflowEntry, WorkflowFacts,
)

HISTORY_SHOWN = 20
FACT_TEXTS = ("exposure", "trust_tier", "resource_tier", "resource_class")
INVALID = "workflow_invalid"

EntryProblems = Callable[[WorkflowEntry], list[dict[str, Any]]]


class BodyError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path, self.message = path, message


class _Reply(Exception):
    def __init__(self, response: JSONResponse) -> None:
        super().__init__("reply")
        self.response = response


class BodyRefused(_Reply):
    """The injected body reader refused the request (not JSON, not an object); ``response`` is the answer to send."""


def _clean(text: str) -> str:
    """A gateway sentence as the error shape would send it (control characters and URL user info removed)."""
    return json.loads(egress.safe_error_response(status.HTTP_400_BAD_REQUEST, "x", text).body)["error"]["message"]


def _plain(content: Mapping[str, Any], status_code: int = 200, *, etag: int | None = None) -> JSONResponse:
    response = egress.plain_json_response(content, response_class="workflow_owner", status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    if etag is not None:
        response.headers["ETag"] = f'"os-{etag}"'
    return response


def _error(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return egress.safe_error_response(status_code, code, message, extra=extra)


def _refused(problems: list[dict[str, Any]]) -> JSONResponse:
    code = ID_CLASH if problems and all(p["code"] == ID_CLASH for p in problems) else INVALID
    message = _clean(problems[0]["message"]) if problems else "the workflow was refused"
    shown = [{**p, "message": _clean(p["message"])} for p in problems]
    return _plain({"error": {"code": code, "message": message}, "problems": shown}, status.HTTP_422_UNPROCESSABLE_ENTITY)


def facts_from_body(body: Mapping[str, Any]) -> Any:
    """``KEEP`` when the body has no ``facts``, ``None`` for ``facts: null``, else the facts it states."""
    if "facts" not in body:
        return KEEP
    raw = body["facts"]
    if raw is None:
        return None
    tags = raw.get("tags", []) if isinstance(raw, Mapping) else None
    if not isinstance(raw, Mapping) or not all(isinstance(raw.get(name), str) for name in FACT_TEXTS) \
            or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise BodyError("facts", "facts is null or an object of exposure, trust_tier, resource_tier, resource_class and tags")
    return WorkflowFacts(raw["exposure"], raw["trust_tier"], raw["resource_tier"], raw["resource_class"], tuple(tags))


def _save_body(body: Mapping[str, Any]) -> tuple[dict[str, Any], Any, str | None]:
    contract = body.get("contract")
    if not isinstance(contract, Mapping):
        raise BodyError("contract", "the body holds the workflow document as contract")
    overrides = body.get("overrides")
    if overrides is not None and not isinstance(overrides, str):
        raise BodyError("overrides", "overrides is the id of the bundled workflow it replaces")
    return dict(contract), facts_from_body(body), overrides


def fact_choices(entries: Any) -> dict[str, list[str]]:
    """The closed value sets the owner view offers as pick-lists (I6 addendum). A class a stored record already states
    is offered too, so the view can show it."""
    classes = {*workflow_policy.known_resource_classes()}
    classes.update(entry.facts.resource_class for entry in entries if entry.facts is not None)
    return {"exposure": list(EXPOSURE_CLASSES), "trust_tier": list(TRUST_TIERS), "resource_tier": list(RESOURCE_TIERS),
            "resource_class": sorted(classes)}


def entry_view(entry: WorkflowEntry, problems: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": entry.id, "state": entry.state, "published": entry.published, "record_revision": entry.record_revision,
        "contract_digest": entry.contract_digest, "record_digest": entry.record_digest,
        "provenance": {"trusted": entry.provenance.trusted, "source": entry.provenance.source},
        "needs_configuration": entry.needs_configuration, "entry_problems": problems,
    }


def register_workflow_owner_routes(
    router: APIRouter, *, store: WorkflowRegistryStore, guard: Callable[[Request], Any],
    read_json: Callable[[Request], Any], entry_problems: EntryProblems | None = None,
) -> None:
    """``entry_problems(entry)`` is the projection layer's list of what keeps an entry from running (I3); without it an
    entry reports only that it needs configuration."""

    def problems_of(entry: WorkflowEntry) -> list[dict[str, Any]]:
        if entry_problems is not None:
            return entry_problems(entry)
        return [{"code": "needs_configuration"}] if entry.needs_configuration else []

    def match(request: Request) -> int:
        raw = request.headers.get("if-match", "")
        text = raw[4:-1] if raw.startswith('"os-') and raw.endswith('"') else ""
        if not (text.isascii() and text.isdecimal()):
            raise _Reply(_error(status.HTTP_412_PRECONDITION_FAILED, GENERATION_MISMATCH, 'If-Match: "os-N" is required'))
        return int(text)

    def detail(entry: WorkflowEntry) -> dict[str, Any]:
        history = [{**dataclasses.asdict(row)} for row in store.history(entry.id, limit=HISTORY_SHOWN)]
        facts = dataclasses.asdict(entry.facts) if entry.facts is not None else None
        return {**entry_view(entry, problems_of(entry)), "contract": json.loads(json.dumps(dict(_thaw(entry.contract)))),
                "facts": facts, "overrides": entry.overrides, "incarnation": entry.incarnation, "history": history}

    def held_detail(workflow_id: str, record: Mapping[str, Any], problems: Any, serving: WorkflowEntry | None
                    ) -> dict[str, Any]:
        """A stored workflow the serving view leaves out: its stored contract, why it is held, and, apart, the
        bundled workflow that serves in its place (if any)."""
        history = [{**dataclasses.asdict(row)} for row in store.history(workflow_id, limit=HISTORY_SHOWN)]
        return {"id": workflow_id, "held_out": True, "hold_problems": [_with_clean(problem.as_dict()) for problem in problems],
                "contract": json.loads(json.dumps(record.get("manifest"))), "history": history,
                "serving": detail(serving) if serving is not None else None}

    async def run(request: Request, work: Callable[[Actor, Any], Commit], *, created: bool = False,
                  needs_match: bool = True, read_body: bool = True) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            if_match = match(request) if needs_match else None
            body = await read_json(request) if read_body else {}
            actor = Actor(ctx.actor_kind, ctx.audit_actor_id, ctx.actor_label, ctx.browser_session)
            commit = await asyncio.to_thread(work, actor, (if_match, body))
        except _Reply as reply:
            return reply.response
        except BodyError as error:
            return _refused([{"path": [error.path], "pointer": f"/{error.path}", "code": INVALID, "message": error.message}])
        except SessionEnded as error:
            return _error(status.HTTP_401_UNAUTHORIZED, error.code, error.message)
        except RegistryConflict as error:
            if error.code == GENERATION_MISMATCH:
                return _error(status.HTTP_412_PRECONDITION_FAILED, GENERATION_MISMATCH,
                              "the registry changed; refresh and retry", current_revision=error.current_revision)
            return _error(status.HTTP_409_CONFLICT, error.code, "a workflow with this id already exists")
        except WorkflowIdIsBundled as error:
            return egress.error_with_projection(status.HTTP_409_CONFLICT, "id_is_bundled", str(error),
                                                {"bundled_workflow": error.bundled} if error.bundled else {})
        except BundledWorkflowRemoved as error:
            return _error(status.HTTP_409_CONFLICT, "bundled_workflow_removed",
                          f"workflow {error.workflow_id!r} is no longer a bundled workflow; its run-control revision "
                          "cannot be restored")
        except RegistryRefused as error:
            return _refused([problem.as_dict() for problem in error.problems])
        except RegistryNotApplied as error:
            return _plain({"error": {"code": "registry_not_applied",
                                     "message": "The change is saved, but the running gateway could not apply it."},
                           "commit": dataclasses.asdict(error.commit)}, status.HTTP_500_INTERNAL_SERVER_ERROR,
                          etag=error.commit.revision)
        return _plain({"commit": dataclasses.asdict(commit)}, status.HTTP_201_CREATED if created else status.HTTP_200_OK,
                      etag=commit.revision)

    @router.get("/workflows")
    async def list_workflows(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        snapshot = await asyncio.to_thread(store.snapshot)
        # A workflow's problems can read the live service registry (an adapter's binding check), so off the loop.
        rows = await asyncio.to_thread(
            lambda: [entry_view(entry, problems_of(entry)) for _id, entry in sorted(snapshot.workflows.items())])
        # Stored workflows that do not serve because their names clash with names the registry already held.
        held_out = [{"id": wid, "problems": [_with_clean(problem.as_dict()) for problem in problems]}
                    for wid, problems in snapshot.held_out.items()]
        return _plain({"workflows": rows, "held_out": held_out, "revision": snapshot.revision,
                       "choices": fact_choices(snapshot.workflows.values())}, etag=snapshot.revision)

    @router.post("/workflows/check")
    async def check_workflow(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            contract, facts, overrides = _save_body(await read_json(request))
        except _Reply as reply:
            return reply.response
        except BodyError as error:
            return _refused([{"path": [error.path], "pointer": f"/{error.path}", "code": INVALID, "message": error.message}])
        snapshot = await asyncio.to_thread(store.snapshot)
        report = await asyncio.to_thread(store.check, contract, workflow_id=None, facts=facts, overrides=overrides,
                                         snapshot=snapshot)
        return _plain({"report": {"ok": report.ok, "errors": [_with_clean(e.as_dict()) for e in report.errors]}},
                      etag=snapshot.revision)

    @router.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        snapshot = await asyncio.to_thread(store.snapshot)
        entry = snapshot.get(workflow_id)
        problems = snapshot.held_out.get(workflow_id)
        if problems is not None:   # stored but left out of the serving view: the owner reads the document to repair it
            stored = await asyncio.to_thread(store.get_record, workflow_id)
            if stored is not None:
                return _plain({"workflow": await asyncio.to_thread(held_detail, workflow_id, stored, problems, entry)},
                              etag=snapshot.revision)
        if entry is None:
            return _error(status.HTTP_404_NOT_FOUND, "workflow_not_found", "there is no workflow with this id")
        return _plain({"workflow": await asyncio.to_thread(detail, entry)}, etag=snapshot.revision)

    @router.post("/workflows")
    async def create_workflow(request: Request) -> JSONResponse:
        def work(actor: Actor, args: Any) -> Commit:
            contract, facts, overrides = _save_body(args[1])
            return store.put_workflow(contract, if_match=None, actor=actor, facts=facts, overrides=overrides)

        return await run(request, work, created=True, needs_match=False)

    @router.put("/workflows/{workflow_id}")
    async def update_workflow(workflow_id: str, request: Request) -> JSONResponse:
        def work(actor: Actor, args: Any) -> Commit:
            contract, facts, overrides = _save_body(args[1])
            if contract.get("id") != workflow_id:
                raise RegistryRefused((_id_mismatch(workflow_id),))
            return store.put_workflow(contract, if_match=args[0], actor=actor, facts=facts, overrides=overrides)

        return await run(request, work)

    @router.post("/workflows/{workflow_id}/state")
    async def set_state(workflow_id: str, request: Request) -> JSONResponse:
        return await run(request, lambda actor, args: store.set_state(
            workflow_id, args[1].get("state"), if_match=args[0], actor=actor))

    @router.post("/workflows/{workflow_id}/publish")
    async def set_published(workflow_id: str, request: Request) -> JSONResponse:
        return await run(request, lambda actor, args: store.set_published(
            workflow_id, args[1].get("published"), if_match=args[0], actor=actor))

    @router.post("/workflows/{workflow_id}/restore")
    async def restore(workflow_id: str, request: Request) -> JSONResponse:
        def work(actor: Actor, args: Any) -> Commit:
            revision = args[1].get("record_revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise BodyError("record_revision", "record_revision is the number of a saved revision")
            return store.restore(workflow_id, revision, if_match=args[0], actor=actor)

        return await run(request, work)

    @router.delete("/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str, request: Request) -> JSONResponse:
        return await run(request, lambda actor, args: store.delete_workflow(workflow_id, if_match=args[0], actor=actor),
                         read_body=False)


def _id_mismatch(workflow_id: str):
    from gateway.manifests import ContractError

    return ContractError(("contract", "id"), "id_mismatch", f"the document must be for {workflow_id!r}")


def _with_clean(problem: dict[str, Any]) -> dict[str, Any]:
    return {**problem, "message": _clean(problem["message"])}


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value
