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
    "mesh_route_replacement", "idempotency_acceptance", "terminal_cancel_noop", "bounded_wait", "remove",
})
_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+)\]")


class DeltaError(AssertionError):
    """A delta that does not hold against the trace it is applied to."""


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


def _replace(document: Any, path: str, old: Any, new: Any, *, remove: bool = False) -> None:
    outer, _, inner = path.partition("#")
    holder, key = _walk(document, _steps(outer), outer)
    if not inner:
        if holder[key] != old:
            raise DeltaError(f"unmatched delta at {path}: the golden holds {holder[key]!r}, the delta says {old!r}")
        if remove:
            del holder[key]
        else:
            holder[key] = new
        return
    text = holder[key]
    if not isinstance(text, str):
        raise DeltaError(f"{outer} holds no JSON text")
    parsed = json.loads(text)
    inner_holder, inner_key = _walk(parsed, _steps(inner), path)
    if inner_holder[inner_key] != old:
        raise DeltaError(f"unmatched delta at {path}: the golden holds {inner_holder[inner_key]!r}, the delta says {old!r}")
    if remove:
        del inner_holder[inner_key]
    else:
        inner_holder[inner_key] = new
    spaced = '": ' in text
    holder[key] = json.dumps(parsed, ensure_ascii=False, separators=(", ", ": ") if spaced else (",", ":"))


def apply_deltas(golden: Mapping[str, Any], deltas: Sequence[Delta]) -> dict[str, Any]:
    """The old trace with every listed difference applied. Raises ``DeltaError`` for a delta that does not hold."""
    document = json.loads(json.dumps(golden))
    for delta in deltas:
        problems = delta.problems()
        if problems:
            raise DeltaError("; ".join(problems))
        _replace(document, delta.path, delta.old, delta.new, remove=delta.kind == "remove")
    return document


#: scenario name -> its expected differences. A route that moves adds its entries here, in the commit that moves it.
MANIFEST: dict[str, tuple[Delta, ...]] = {}
