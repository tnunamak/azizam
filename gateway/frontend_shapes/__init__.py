"""Frontend rows over reviewed wire shapes (CODE_TO_DATA_S4_S8 I5, section 8).

``registry/protocol_frontends.yml`` holds one row per modality route under ``frontends:``: the shape that speaks its wire,
the routes, a selector in the kernel's closed schema that says which workflows it serves, and ``mode``. This package
loads and validates the rows, builds the shapes, and answers ``profile_problems(contract)``: why no bundled row that
selects a contract can serve it (``gateway.frontend_profiles.check`` calls it at save)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from gateway.frontend_shapes._base import Shape
from gateway.frontend_shapes.mesh import LocalAI3D, RawGlb
from gateway.frontend_shapes.shapes import (
    AudioGenerate,
    ImageJobs,
    OpenAIImagesJson,
    OpenAIImagesMultipart,
    OpenAIImagesVariations,
    OpenAIVideoCreate,
    OpenAIVideoEdit,
    OpenAIVideoRemix,
)
from gateway.workflow_kernel import selector_matches, selector_problems
from gateway.workflow_runtime_types import FrontendRow, RouteSpec

ROWS_PATH = Path(__file__).resolve().parents[2] / "registry" / "protocol_frontends.yml"
ROWS_KEY = "frontends"

SHAPE_CLASSES: Mapping[str, Callable[..., Shape]] = {cls.shape_id: cls for cls in (
    OpenAIImagesJson, OpenAIImagesMultipart, OpenAIImagesVariations, ImageJobs, AudioGenerate, OpenAIVideoCreate, OpenAIVideoEdit, OpenAIVideoRemix, LocalAI3D, RawGlb)}


class RowError(ValueError):
    """A frontend row that cannot be used. The message names the row and the rule."""


def _receipt_opens(row_id: str, raw: Mapping[str, Any]) -> Any:
    value = raw.get("receipt_opens", "after_policy")
    if value not in ("before_policy", "after_policy"):
        raise RowError(f"{row_id}: receipt_opens must be before_policy or after_policy")
    return value


def _receipt_closes(row_id: str, raw: Mapping[str, Any]) -> Any:
    value = raw.get("receipt_closes", "terminal")
    if value not in ("terminal", "submit"):
        raise RowError(f"{row_id}: receipt_closes must be terminal or submit")
    return value


def _backend_at_run(row_id: str, raw: Mapping[str, Any]) -> bool:
    value = raw.get("backend_at_run", False)
    if not isinstance(value, bool):
        raise RowError(f"{row_id}: backend_at_run must be true or false")
    return value


def _history_facts(row_id: str, raw: Mapping[str, Any]) -> bool:
    value = raw.get("history_facts", True)
    if not isinstance(value, bool):
        raise RowError(f"{row_id}: history_facts must be true or false")
    return value


def _unconfigured(row_id: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
    found = raw.get("unconfigured")
    if found is None:
        return None
    if (not isinstance(found, Mapping) or set(found) != {"status", "code", "message"} or not isinstance(found["status"], int)
            or not all(isinstance(found[k], str) and found[k] for k in ("code", "message"))):
        raise RowError(f"{row_id}: unconfigured must be a mapping of status, code and message")
    return dict(found)
def _cancel_wait_s(row_id: str, raw: Mapping[str, Any]) -> float:
    value = raw.get("cancel_wait_s", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RowError(f"{row_id}: cancel_wait_s must be a number of seconds, 0 or more")
    return float(value)


def _refusal_receipt(row_id: str, raw: Mapping[str, Any]) -> Any:
    value = raw.get("refusal_receipt", "failed")
    if value not in ("failed", "rejected"):
        raise RowError(f"{row_id}: refusal_receipt must be failed or rejected")
    return value


def _row(raw: Mapping[str, Any]) -> FrontendRow:
    row_id = raw.get("id")
    if not isinstance(row_id, str) or not row_id:
        raise RowError(f"a frontend row needs an id: {raw!r}")
    if raw.get("shape") not in SHAPE_CLASSES:
        raise RowError(f"{row_id}: unknown shape {raw.get('shape')!r}")
    if raw.get("mode") not in ("sync", "job"):
        raise RowError(f"{row_id}: mode must be sync or job")
    selects = raw.get("selects")
    if not isinstance(selects, Mapping):
        raise RowError(f"{row_id}: selects must be a mapping")
    problems = selector_problems(selects)
    if problems:
        raise RowError(f"{row_id}: " + "; ".join(f"{p.pointer or '/'} {p.message}" for p in problems))
    routes = tuple(RouteSpec(methods=tuple(route["methods"]), canonical=route["canonical"],
                             aliases=tuple(route.get("aliases") or ()), runtime_policy=dict(route.get("runtime_policy") or {}))
                   for route in raw.get("routes") or ())
    if not routes:
        raise RowError(f"{row_id}: a row needs at least one route")
    texts = {route.runtime_policy.get("job_failure_text", "adapter") for route in routes}
    if len(texts) != 1 or not texts <= {"adapter", "category_copy"}:
        raise RowError(f"{row_id}: runtime_policy.job_failure_text must be adapter or category_copy, the same on every route")
    retries = {route.runtime_policy.get("job_retries", "binding") for route in routes}
    if len(retries) != 1 or not retries <= {"binding", "none"}:
        raise RowError(f"{row_id}: runtime_policy.job_retries must be binding or none, the same on every route")
    answers = {route.runtime_policy.get("default_answer") for route in routes}
    shape_forms = SHAPE_CLASSES[raw["shape"]].answer_forms
    if len(answers) != 1 or not answers <= {None, *shape_forms}:
        raise RowError(f"{row_id}: runtime_policy.default_answer must be one of {sorted(shape_forms) or 'none (the shape has no answer forms)'}, "
                       "the same on every route")
    names = {route.runtime_policy.get("receipt_names", "id") for route in routes}
    if len(names) != 1 or not names <= {"id", "short"}:
        raise RowError(f"{row_id}: runtime_policy.receipt_names must be id or short, the same on every route")
    return FrontendRow(id=row_id, shape=raw["shape"], result_headers=_names(row_id, raw, "result_headers"),
                       result_receipt=_names(row_id, raw, "result_receipt"), refusal_bodies=_refusal_bodies(row_id, raw), routes=routes, selects=dict(selects), mode=raw["mode"],
                       job_failure_text=next(iter(texts)), job_retries=next(iter(retries)), default_answer=next(iter(answers)), receipt_names=next(iter(names)),
                       receipt_protocol=str(raw.get("receipt_protocol") or ""),
                       strictness=raw.get("strictness", "exact"), lists_as_model=bool(raw.get("lists_as_model", False)),
                       receipt_opens=_receipt_opens(row_id, raw), receipt_closes=_receipt_closes(row_id, raw),
                       history_facts=_history_facts(row_id, raw), backend_at_run=_backend_at_run(row_id, raw), unconfigured=_unconfigured(row_id, raw),
                       cancel_wait_s=_cancel_wait_s(row_id, raw),
                       refusal_receipt=_refusal_receipt(row_id, raw), run_receipt_path=str(raw.get("run_receipt_path") or ""))


def _names(row_id: str, raw: Mapping[str, Any], key: str) -> dict[str, str]:
    """A row's mapping of metadata keys to names (header names, receipt metadata names): text on both sides."""
    found = raw.get(key) or {}
    if not isinstance(found, Mapping) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in found.items()):
        raise RowError(f"{row_id}: {key} must map metadata keys to names")
    return dict(found)


def _refusal_bodies(row_id: str, raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{code: {message, type?, code?, retry_after_s?}}``: the words a row has for a refusal code of the core."""
    found = raw.get("refusal_bodies") or {}

    def usable(body: Any) -> bool:
        return (isinstance(body, Mapping) and "message" in body and set(body) <= {"message", "type", "code", "retry_after_s"}
                and all(isinstance(body[k], str) and body[k] for k in body if k != "retry_after_s")
                and (not isinstance(body.get("retry_after_s"), bool)) and isinstance(body.get("retry_after_s", 1), int))

    if not isinstance(found, Mapping) or not all(isinstance(code, str) and usable(body) for code, body in found.items()):
        raise RowError(f"{row_id}: refusal_bodies must map a code to its message, and optionally its type, code and retry_after_s")
    return {code: dict(body) for code, body in found.items()}


def parse_rows(document: Mapping[str, Any]) -> tuple[FrontendRow, ...]:
    """The rows of a release document, validated. Raises ``RowError`` for the first row that is not usable, and for an id
    used twice."""
    rows = tuple(_row(raw) for raw in document.get(ROWS_KEY) or ())
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            raise RowError(f"{row.id}: the id is used twice")
        seen.add(row.id)
    return rows


@lru_cache(maxsize=1)
def _bundled(signature: tuple[int, int]) -> tuple[FrontendRow, ...]:
    return parse_rows(yaml.safe_load(ROWS_PATH.read_text(encoding="utf-8")) or {})


def bundled_rows() -> tuple[FrontendRow, ...]:
    stat = ROWS_PATH.stat()
    return _bundled((stat.st_mtime_ns, stat.st_size))


def build_shape(row: FrontendRow, **options: Any) -> Shape:
    return SHAPE_CLASSES[row.shape](**options)


def profile_problems(contract: Mapping[str, Any]) -> tuple[str, ...]:
    """Why the bundled rows that select this contract cannot serve it, for the rows whose wire is single-purpose
    (``Shape.save_gate``). Empty when no such row selects it (the generic run route serves it) or when at least one
    selecting row can serve it. The other shapes' profiles narrow the workflows a request can reach (``to_run_request``)."""
    if not isinstance(contract, Mapping):
        return ()
    identity = contract.get("id") if isinstance(contract.get("id"), str) else ""
    reasons: list[str] = []
    for row in bundled_rows():
        try:
            selected = selector_matches(row.selects, identity, contract)
        except (AttributeError, TypeError):
            selected = False
        if not selected:
            continue
        shape = build_shape(row)
        if not shape.save_gate:
            continue
        found = shape.profile(contract)
        if not found:
            return ()
        reasons.extend(found)
    return tuple(dict.fromkeys(reasons))
