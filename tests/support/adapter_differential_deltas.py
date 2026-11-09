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
    "job_row_metadata", "job_create_failure_text", "idempotency_conflict", "executor_crash_text", "engine_session_id", "remove",
})
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
        # A remove of a list item names that whole item as its old value (a replay drops one engine call or one
        # listed job); every other delta names one leaf value, so it cannot hide a change inside a subtree.
        removes_list_item = self.kind == "remove" and self.path.endswith("]")
        if not self.subtree and any(isinstance(value, (dict, list)) for value in (self.new,) + (() if removes_list_item else (self.old,))):
            out.append(f"{self.path}: a delta names one value, not a subtree")
        if self.kind == "engine_session_id" and not self._engine_session_path():
            out.append(f"{self.path}: an engine_session_id delta adds only the prompt's {ENGINE_SESSION_LEAF}")
        if self.kind == "engine_session_id" and self.old is not MISSING:
            out.append(f"{self.path}: an engine_session_id delta adds a key the old prompt did not have (old is MISSING)")
        return out

    def _engine_session_path(self) -> bool:
        outer, _, inner = self.path.partition("#")
        return outer.startswith("$.engine_requests[") and outer.endswith(".body.text") and inner == ENGINE_SESSION_LEAF


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
    # Acceptance matrix row 18: the same key with another body is a conflict, not the first job again.
    _IMAGES + "J10": (
        Delta("idempotency_conflict", "$.caller[1].response.status", 202, 409,
              "the same Idempotency-Key with another body answers 409 (acceptance matrix row 18), not the first job"),
        Delta("idempotency_conflict", "$.caller[1].response.body.text#$.error", None,
              {"code": "idempotency_conflict", "message": "this Idempotency-Key was used for a different request",
               "type": "invalid_request_error"},
              "the answer is the conflict error; the old answer was the first job, whose error field is null", subtree=True),
    ),
    # Run-core rule 7d3eb681a: a failure category is a word from the job store's list, so no client text names an exception.
    _IMAGES + "J11": (
        Delta("job_create_failure_text", "$.caller[0].response.body.text#$.error.message",
              "Could not create image media job: RuntimeError", "the job could not be created",
              "the client text no longer names the exception class (run-core rule 7d3eb681a); the class is logged"),
        Delta("job_create_failure_text", "$.caller[0].response.body.text#$.error.code", MISSING, "job_not_created",
              "the refusal carries the stable code the run core names for a job that was not stored"),
    ),
    # Run-core rule 7d3eb681a and the S7 addendum's job-retry rule: an unexpected error is not retried and stores the
    # fixed sentence of the failure's category; only an adapter's named retry reason retries.
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
