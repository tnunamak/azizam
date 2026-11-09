"""The per-scenario list of expected golden differences (golden design section 4; design addenda of S4_S8).

A moved route is compared with the golden recorded on its old handler. The only allowed differences are the ones
listed here, per scenario, each with the exact JSON path, the old value, the new value and the reason. The list is
applied to the OLD trace, and the result must equal the NEW trace: a path that does not exist, an old value that is
not what the golden holds, or any difference the list does not name, fails. Nothing here masks a status, an error
code, a header, an engine request, a receipt field or an asset row; fix such a mismatch in the shape, the template
or the adapter, never with an entry.

A path is ``$.key[0].key``. To reach a value inside the JSON text of a body, add ``#`` and a second path into that
JSON: ``$.caller[0].response.body.text#$.status``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: The differences the design allows, and nothing else.
KINDS = frozenset({
    "sync_workflow_lifecycle", "envelope_v2", "workflow_run_metadata", "graph_template_meta",
    "mesh_route_replacement", "idempotency_acceptance", "terminal_cancel_noop", "bounded_wait", "receipt_status_words",
    "job_row_metadata", "job_create_failure_text", "idempotency_key_reuse_conflict", "executor_crash_text", "engine_session_id", "remove",
    # The video move (CODE_TO_DATA_S7_VIDEO_MOVE.md section 5.4). Each names one thing that differs and nothing else.
    "video_id_namespace", "record_store_moved", "orphan_marked_by_runner", "receipt_names_job", "status_follows_runner",
})
#: The video-move kinds. None of them may change an HTTP status or an error body: a delta of one of these kinds whose
#: path goes through a ``status`` that follows ``response`` (the HTTP status) or through an ``error`` is refused.
VIDEO_KINDS = frozenset({"video_id_namespace", "record_store_moved", "orphan_marked_by_runner", "receipt_names_job",
                         "status_follows_runner"})
#: The only deltas that may touch an error body or a refusal status. Each kind lists the exact paths it may touch (regular
#: expressions over the whole path), and a status change is allowed only for ``idempotency_key_reuse_conflict`` (a different body
#: under a used Idempotency-Key answers 409, not the first job: Stripe's rule for key reuse). The other two are the failure texts
#: that no longer name an exception class (run-core rule 7d3eb681a). Every other kind, and every other path, is refused.
ERROR_ALLOWED: dict[str, tuple[str, ...]] = {
    "idempotency_key_reuse_conflict": (r"\$\.caller\[\d+\]\.response\.status", r"\$\.caller\[\d+\]\.response\.body\.text#\$\.error"),
    "job_create_failure_text": (r"\$\.caller\[\d+\]\.response\.body\.text#\$\.error\.(message|code)",),
    "executor_crash_text": (r"\$\.media_jobs\.media_jobs\[\d+\]\.error", r"\$\.caller\[\d+\]\.response\.body\.text#\$\.error"),
}
STATUS_CHANGE_ALLOWED = {"idempotency_key_reuse_conflict": (202, 409)}
#: The engine-request delta the design allows: the adapter's session id in the engine prompt (``client_id``). Nothing else in
#: an engine request may be named.
ENGINE_SESSION_LEAF = "$.client_id"
#: Places a moved route keeps identical (golden design section 4): engine requests, every header, the receipt's
#: accepted inputs and resolved workflow, and the stored asset rows. No kind may name a path through one of them.
KEPT_IDENTICAL = frozenset({"engine_requests", "headers", "accepted_inputs", "resolved_workflow", "assets"})
_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+)\]")


class DeltaError(AssertionError):
    """A delta that does not hold against the trace it is applied to."""


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


#: As a delta's ``old``: the golden holds no such key, and the delta adds it (the key's parent must exist).
MISSING = _Missing()


@dataclass(frozen=True)
class Delta:
    """One allowed difference. A ``remove`` deletes the element at ``path`` (a list item or a key); ``old`` is the
    value the golden holds there, and ``new`` must be None. Removing a list item shifts the items after it, so
    a manifest lists the removals from the highest index down."""
    kind: str
    path: str
    old: Any
    new: Any
    reason: str
    #: ``old`` or ``new`` may be a JSON object or list, named whole, where the wire replaces a null with an object (an error
    #: body). The old value must still equal what the golden holds, so nothing inside it is hidden.
    subtree: bool = False

    def _video_problems(self) -> list[str]:
        try:
            return _video_problems_of(self.path, self.kind)
        except DeltaError:
            return []                       # the malformed path is reported by ``problems``

    def _steps_or_none(self) -> list[str | int] | None:
        try:
            return [step for part in self.path.split("#") for step in _steps(part)]
        except DeltaError:
            return None                     # the malformed path is reported by ``problems``

    def _wire_problems(self) -> list[str]:
        """Checks that hold for EVERY kind: no delta rewrites an error body, and none moves a refusal status."""
        steps = self._steps_or_none()
        if steps is None:
            return []
        out = []
        permitted = any(re.fullmatch(pattern, self.path) for pattern in ERROR_ALLOWED.get(self.kind, ()))
        if "error" in steps and not permitted:
            out.append(f"{self.path}: no delta may change an error body")
        if any(a == "response" and b == "status" for a, b in zip(steps, steps[1:])):
            if permitted and STATUS_CHANGE_ALLOWED.get(self.kind) == (self.old, self.new):
                return out
            for value in (self.old, self.new):
                if not isinstance(value, int) or isinstance(value, bool) or value >= 400:
                    out.append(f"{self.path}: no delta may change a refusal status (400 and above) or a status that is not a number")
                    break
        return out

    def _remove_problems(self) -> list[str]:
        """A remove drops a list item inside a body's JSON, or one scalar leaf under a response body. It never drops a
        whole observation, a header, an engine request or a subtree that holds one."""
        steps = self._steps_or_none()
        if steps is None:
            return []
        out = []
        in_json_text = "#" in self.path
        leaf_in_body = ("body" in steps and isinstance(steps[-1], str) and not isinstance(self.old, (dict, list)))
        item_in_json = in_json_text and isinstance(steps[-1], int)
        if not (item_in_json or leaf_in_body):
            out.append(f"{self.path}: a remove may only drop a list item inside a body's JSON text or one scalar leaf "
                       "under a response body")
        protected = _protected_inside(self.old)
        if protected:
            out.append(f"{self.path}: the removed value holds {protected!r}, which no delta may change")
        return out

    def problems(self) -> list[str]:
        out = []
        if self.kind not in KINDS:
            out.append(f"{self.path}: kind {self.kind!r} is not one of the allowed differences")
        if self.kind == "remove" and self.new is not None:
            out.append(f"{self.path}: a remove has no new value (new must be None)")
        if not self.reason.strip():
            out.append(f"{self.path}: a delta needs a reason")
        if self.old == self.new:
            out.append(f"{self.path}: old and new are equal, so the entry does nothing")
        if not self.path.startswith("$"):
            out.append(f"{self.path}: a path starts with $")
        else:
            try:
                allowed = {"engine_requests"} if self.kind == "engine_session_id" and self._engine_session_path() else set()
                kept = [step for part in self.path.split("#") for step in _steps(part) if step in KEPT_IDENTICAL and step not in allowed]
            except DeltaError as error:
                kept = []
                out.append(str(error))
            if kept:
                out.append(f"{self.path}: {kept[0]!r} is kept identical by a moved route; fix the shape, not the golden")
            out.extend(self._wire_problems())
            if self.kind == "remove":
                out.extend(self._remove_problems())
            if self.kind in VIDEO_KINDS:
                out.extend(self._video_problems())
        # A remove of a list item names that whole item as its old value (a replay drops one engine call or one
        # listed job); every other delta names one leaf value, so it cannot hide a change inside a subtree.
        removes_list_item = self.kind == "remove" and self.path.endswith("]")
        # No kind replaces a subtree unless it says so (``subtree``); a named subtree may not hold a protected key, and an
        # ancestor of a protected leaf (``$.caller[0].response``) is refused the same way.
        if not self.subtree and any(isinstance(value, (dict, list)) for value in (self.new,) + (() if removes_list_item else (self.old,))):
            out.append(f"{self.path}: a delta names one value, not a subtree")
        if self.subtree:
            for value in (self.old, self.new):
                found = _protected_inside(value)
                if found:
                    out.append(f"{self.path}: the named subtree holds {found!r}, which no delta may change")
        if self.kind == "engine_session_id" and not self._engine_session_path():
            out.append(f"{self.path}: an engine_session_id delta adds only the prompt's {ENGINE_SESSION_LEAF}")
        if self.kind == "engine_session_id" and self.old is not MISSING:
            out.append(f"{self.path}: an engine_session_id delta adds a key the old prompt did not have (old is MISSING)")
        return out

    def _engine_session_path(self) -> bool:
        outer, _, inner = self.path.partition("#")
        return outer.startswith("$.engine_requests[") and outer.endswith(".body.text") and inner == ENGINE_SESSION_LEAF


#: What each video-move kind may touch (design 5.4). A leaf family is the first step of the path (a trace section) or the
#: last step (the field). A kind not listed, or a path outside its families, is refused. ``status_follows_runner`` is
#: test mechanics only: it changes no trace value, so it has no family and every delta of that kind is refused.
KIND_LEAVES: dict[str, dict[str, frozenset[str]]] = {
    "video_id_namespace": {"last": frozenset({"id", "video_id", "after", "first_id", "last_id",
                                              "remixed_from_video_id", "extended_from_video_id"})},
    # A relocation names where a field is read from, never what it holds: owner, person, model and every other
    # meaning-bearing value are refused. The same value at its new home is not a difference, so it needs no delta.
    "record_store_moved": {"first": frozenset({"records", "request_facts"}),
                           "last": frozenset({"store", "source", "table", "read_from"})},
    "orphan_marked_by_runner": {"last": frozenset({"marked_by", "marked_at"})},
    "receipt_names_job": {"first": frozenset({"receipts"}), "last": frozenset({"job_id", "workflow"})},
    "status_follows_runner": {},
}


_PROTECTED_KEYS = KEPT_IDENTICAL | {"error"}


def _protected_inside(value: Any) -> str | None:
    """The first protected key found anywhere inside ``value``, looking through nested objects, lists and JSON text."""
    if isinstance(value, str) and _is_json_document_text(value):
        value = json.loads(value)
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in _PROTECTED_KEYS:
                return key
            found = _protected_inside(inner)
            if found:
                return found
    elif isinstance(value, list):
        for inner in value:
            found = _protected_inside(inner)
            if found:
                return found
    return None


def _video_problems_of(path: str, kind: str) -> list[str]:
    steps = [step for part in path.split("#") for step in _steps(part)]
    out = []
    if any(a == "response" and b == "status" for a, b in zip(steps, steps[1:])):
        out.append(f"{path}: a video-move delta cannot change an HTTP status")
    families = KIND_LEAVES.get(kind, {})
    if not families:
        out.append(f"{path}: {kind} is test mechanics (drive, not a wire change) and authorizes no trace change")
        return out
    first = next((s for s in steps if isinstance(s, str)), None)
    last = next((s for s in reversed(steps) if isinstance(s, str)), None)
    if "first" in families and first not in families["first"]:
        out.append(f"{path}: {kind} may only change {sorted(families['first'])} sections, not {first!r}")
    if "last" in families and last not in families["last"]:
        out.append(f"{path}: {kind} may only change {sorted(families['last'])} fields, not {last!r}")
    return out


def _steps(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise DeltaError(f"path {path!r} must start with $")
    steps: list[str | int] = []
    rest = path[1:]
    for match in _STEP.finditer(rest):
        steps.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
    rebuilt = "".join(f".{s}" if isinstance(s, str) else f"[{s}]" for s in steps)
    if rebuilt != rest:
        raise DeltaError(f"path {path!r} is not of the form $.key[0].key")
    return steps


def _walk(node: Any, steps: Sequence[str | int], path: str) -> tuple[Any, str | int]:
    """The container that holds the last step, and that step."""
    for step in steps[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            raise DeltaError(f"unknown path {path}: nothing at {step!r}") from None
    if not steps:
        raise DeltaError(f"path {path!r} names the whole trace")
    last = steps[-1]
    try:
        node[last]
    except (KeyError, IndexError, TypeError):
        raise DeltaError(f"unknown path {path}: nothing at {last!r}") from None
    return node, last


def _parent(node: Any, steps: Sequence[str | int], path: str) -> tuple[Any, str | int]:
    """The container that holds the last step, and that step; the last step itself may be absent."""
    if not steps:
        raise DeltaError(f"path {path!r} names the whole trace")
    for step in steps[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            raise DeltaError(f"unknown path {path}: nothing at {step!r}") from None
    return node, steps[-1]


def _differs(held: Any, old: Any) -> bool:
    """Whether the golden's value is not the delta's old value, by the final comparison's type-aware equality
    (``0`` is not ``False``, ``1`` is not ``1.0``)."""
    from tests.support import adapter_differential as ad
    return ad.traces_differ(held, old)


def _is_json_document_text(value: Any) -> bool:
    """A string that parses as a JSON object or array: replacing it whole would hide a change inside it."""
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(value), (dict, list))
    except (ValueError, RecursionError):
        return False


def _set(root: Any, steps: Sequence[str | int], path: str, old: Any, new: Any, remove: bool) -> None:
    """Change the value at ``steps`` in ``root``: replace ``old`` by ``new``, delete it, or (``old`` is MISSING) add it."""
    if old is MISSING:
        holder, key = _parent(root, steps, path)
        if not isinstance(holder, dict) or not isinstance(key, str):
            raise DeltaError(f"unmatched delta at {path}: a key can be added only to an object")
        if key in holder:
            raise DeltaError(f"unmatched delta at {path}: the golden holds {holder[key]!r}, the delta says it is missing")
        holder[key] = new
        return
    holder, key = _walk(root, steps, path)
    if _differs(holder[key], old):
        raise DeltaError(f"unmatched delta at {path}: the golden holds {holder[key]!r}, the delta says {old!r}")
    if remove:
        del holder[key]
    else:
        holder[key] = new


def _replace(document: Any, path: str, old: Any, new: Any, *, remove: bool = False, subtree: bool = False) -> None:
    outer, _, inner = path.partition("#")
    if not inner:
        holder, key = _parent(document, _steps(outer), outer)
        held = holder[key] if isinstance(holder, (dict, list)) and (key in holder if isinstance(holder, dict) else key < len(holder)) else None
        if not subtree and _is_json_document_text(held):
            raise DeltaError(f"{path}: the value is a serialized JSON document; name a leaf with {outer}#$.key, "
                             "never the whole text")
        _set(document, _steps(outer), path, old, new, remove)
        return
    holder, key = _walk(document, _steps(outer), outer)
    text = holder[key]
    if not isinstance(text, str):
        raise DeltaError(f"{outer} holds no JSON text")
    parsed = json.loads(text)
    _set(parsed, _steps(inner), path, old, new, remove)
    spaced = '": ' in text
    holder[key] = json.dumps(parsed, ensure_ascii=False, separators=(", ", ": ") if spaced else (",", ":"))


def apply_deltas(golden: Mapping[str, Any], deltas: Sequence[Delta]) -> dict[str, Any]:
    """The old trace with every listed difference applied. Raises ``DeltaError`` for a delta that does not hold."""
    document = json.loads(json.dumps(golden))
    for delta in deltas:
        problems = delta.problems()
        if problems:
            raise DeltaError("; ".join(problems))
        _replace(document, delta.path, delta.old, delta.new, remove=delta.kind == "remove", subtree=delta.subtree)
    return document


#: scenario name -> its expected differences. A route that moves adds its entries here, in the commit that moves it.
#: A name ``s7_parity_images/<case>`` belongs to a case of tests/test_s7_parity_images.py: it is applied to the old route's
#: trace as that case observes it, not to a golden file (docs/design/CODE_TO_DATA_S7_PARITY.md section 4).
_IMAGES = "s7_parity_images/"
MANIFEST: dict[str, tuple[Delta, ...]] = {
    # J10: a different body under one Idempotency-Key answers 409, not the first job (the named exception idempotency_key_reuse_conflict).
    _IMAGES + "J10": (
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.status", 202, 409,
              "the same Idempotency-Key with another body answers 409 (acceptance matrix row 18; Stripe's rule for key reuse), not the first job"),
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.body.text#$.error", None,
              {"code": "idempotency_conflict", "message": "this Idempotency-Key was used for a different request",
               "type": "invalid_request_error"},
              "the answer is the conflict error; the old answer was the first job, whose error field is null", subtree=True),
    ),
    _IMAGES + "J11": (
        Delta("job_create_failure_text", "$.caller[0].response.body.text#$.error.message",
              "Could not create image media job: RuntimeError", "the job could not be created",
              "the client text no longer names the exception class (run-core rule 7d3eb681a); the class is logged"),
        Delta("job_create_failure_text", "$.caller[0].response.body.text#$.error.code", MISSING, "job_not_created",
              "the refusal carries the stable code the run core names for a job that was not stored"),
    ),
    _IMAGES + "R06": (
        Delta("executor_crash_text", "$.media_jobs.media_jobs[0].error", "Image media job executor error: RuntimeError", "The job failed.",
              "an unexpected error stores the fixed sentence, not the exception class (run-core rule 7d3eb681a)"),
        Delta("executor_crash_text", "$.caller[1].response.body.text#$.error", "Image media job executor error: RuntimeError",
              "The job failed.", "the same sentence, as the job document shows it"),
        Delta("executor_crash_text", "$.caller[1].response.body.text#$.retryable", None, False,
              "an unexpected error is not retried, so the job states that it is not retryable (S7 addendum, job-retry rule)"),
    ),
    # The adapter's session id in the engine prompt; the event wait needs it. Engine-facing only.
    _IMAGES + "E07b": (
        Delta("engine_session_id", "$.engine_requests[3].body.text#$.client_id", MISSING, "<hex32:1>",
              "the node-graph adapter names its session in the prompt (client_id); the old edit route sent none"),
    ),
    _IMAGES + "V05b": (
        Delta("engine_session_id", "$.engine_requests[3].body.text#$.client_id", MISSING, "<hex32:1>",
              "the node-graph adapter names its session in the prompt (client_id); the old variations route sent none"),
    ),
    # The job row keeps every old leaf with its old value. The mode is the run core's (startup resume reads it, and a row job
    # resumes through the run core); the other leaves are the run core's own bookkeeping, added to what the old row held.
    _IMAGES + "J07": (
        Delta("job_row_metadata", "$.media_jobs.media_jobs[0].job_json.metadata.mode", "async_image_generation", "workflow.run",
              "the mode names the run core, whose startup resume reads it"),
        *(Delta("job_row_metadata", f"$.media_jobs.media_jobs[0].job_json.metadata.{key}", MISSING, value,
                f"the run core records {key} on every job it runs; resume and failure text read it")
          for key, value in (("binding_key", "comfyui"), ("failure_text", "category_copy"), ("frontend", "image-jobs"),
                             ("resource_class", "image"), ("retries", "binding"), ("run_receipt_path", "/v1/images/generations"))),
    ),
}


#: Goldens re-recorded on purpose, each with the reason. A name here is NOT a delta: the golden file holds the new trace, so the
#: guard (KEPT_IDENTICAL, no error or status change) does not compare it with the old route. Keep this list honest: a re-record
#: that hides an engine-facing change is listed under ENGINE_FACING_CHANGES too.
_ASSET_URL = ("the job view lists asset_url and url for a finished asset: the run core signs the link on read (I2); "
              "content-length follows")
_ORIGIN = ("the Library sidecar of a stored asset gains an origin block (request_id, workflow_name, takes_text); "
           "it holds no prompt text (test_the_library_sidecar_origin_block_holds_no_prompt_text)")
_VIDEO_RUN_CORE = ("create answers once the job is queued and the run core runs it: ids are video_mj_*, the first status is "
                   "queued, the prompt is not echoed, and the job rows are the run core's")
_UPLOAD_FIRST = "the prompt names the uploaded file, so upload-first is the causal order"
RERECORDED: dict[str, str] = {
    "video_job_succeeds": _ASSET_URL,
    "audio_generate_job": _ASSET_URL,
    **{f"s7_{cell}_cancel_finished": _ASSET_URL for cell in ("ig", "ie", "iv", "as", "aj")},
    "video_lifecycle": f"{_VIDEO_RUN_CORE}; {_ORIGIN}; engine requests: {_UPLOAD_FIRST}",
    "video_extend": f"{_VIDEO_RUN_CORE}; {_ORIGIN}; engine requests: {_UPLOAD_FIRST} (the reference goes up as a video_<job>.mp4 file, "
                    "before the prompt)",
    "video_create_unreachable": "named delta create_answers_before_the_engine (lead ruling, P8): create answers 200 queued before the "
                                "engine is asked, so an unreachable engine fails the job (engine-unavailable code, no engine address), "
                                "not the create; the old 502 body named the engine address",
    **{f"s7_{cell}": f"{_VIDEO_RUN_CORE}; {_ORIGIN}"
       for cell in ("vc_replay_same", "vc_replay_conflict", "vc_cancel_queued", "vc_cancel_finished", "ve_success",
                    "ve_replay_same", "ve_replay_conflict", "ve_cancel_queued", "ve_cancel_finished")},
    "s7_vc_admission_refusal": "a disabled video service answers 503 service_disabled with the resource decision on the receipt; "
                               "the body is compact JSON (video_id_namespace and json spacing only)",
    "s7_ve_admission_refusal": "as s7_vc_admission_refusal",
    "s7_ve_validation_refusal": "the extend body names the new video id video_<job> where the old route named video_p1 (video_id_namespace)",
    "s7_ve_disconnect": "as s7_ve_validation_refusal",
}
#: Re-records that change what is sent to the engine. These are the behaviour-preservation exceptions to report, not hide.
ENGINE_FACING_CHANGES = frozenset({"video_lifecycle", "video_extend", "s7_ve_success", "s7_ve_replay_same", "s7_ve_replay_conflict",
                                   "s7_ve_cancel_queued", "s7_ve_cancel_finished"})
ORIGIN_KEYS = frozenset({"request_id", "workflow_name", "takes_text", "sources"})
