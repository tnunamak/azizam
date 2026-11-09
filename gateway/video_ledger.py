"""The merged reader of a wire's jobs: the run core's job store beside the older tracking records.

A wire id names either a job of the run core or an older record. The ledger finds which, pages across both with one total
order, and deletes in the store that holds the item. It renders nothing and calls no engine: the older records are read
through an injected port, so this module holds no knowledge of how they were made. When the port is absent or empty the
ledger is a thin wrapper over ``JobService.list``.

The order key of every item, in both stores, is ``(created_at, raw id)``; ``desc`` reverses both parts. A cursor is the
wire id of the last item of the previous page, and it resolves to a position in that order even when the item is in the
other store."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gateway.workflow_runtime_types import CallerContext, JobService, RunJobView


class LegacyRecords(Protocol):
    """The older store, as the ledger needs it. Both calls answer only for records the caller may see and that are not deleted."""

    def find(self, raw_id: str, caller: CallerContext) -> Mapping[str, Any] | None:
        """One record by its raw id, with a ``created_at``; None when unknown, deleted or not the caller's."""

    def visible(self, caller: CallerContext) -> Sequence[tuple[str, Mapping[str, Any]]]:
        """Every record the caller may list, as ``(raw id, record)``."""


@dataclass(frozen=True)
class JobHit:
    view: RunJobView

    @property
    def key(self) -> tuple[int, str]:
        return (int(self.view.created_at_s or 0), self.view.job_id)


@dataclass(frozen=True)
class LegacyHit:
    raw_id: str
    record: Mapping[str, Any]

    @property
    def key(self) -> tuple[int, str]:
        return (int(self.record.get("created_at") or 0), self.raw_id)


Hit = JobHit | LegacyHit


@dataclass(frozen=True)
class ListPage:
    items: tuple[Hit, ...]
    has_more: bool


class CursorNotFound(Exception):
    """The ``after`` id names no item the caller can see now (deleted, evicted, never theirs)."""


class VideoLedger:
    def __init__(self, jobs: JobService, job_id_of: Callable[[str], str | None], intents: Sequence[str],
                 legacy: LegacyRecords | None = None, is_job_id: Callable[[str], bool] = lambda raw: False,
                 video_workflows: Callable[[], Sequence[str]] = lambda: ()) -> None:
        self._jobs = jobs
        self._job_id_of = job_id_of           # the wire's own rule: wire id -> raw id, None when malformed
        self._intents = tuple(intents)
        self._legacy = legacy
        self._is_job_id = is_job_id           # the job store's id form: such an id never names an older record
        self._video_workflows = video_workflows   # ids of the workflows whose record declares video output, read per call

    def _is_video(self, view: Any) -> bool:
        """A job counts when it is filed under one of this wire's intents, or when its workflow's record declares video output,
        whichever route made it (the generic run route files jobs under its own intent)."""
        return view.intent in self._intents or view.workflow_id in self._video_workflows()

    def names_a_job(self, wire_id: str) -> bool:
        """True when the id has the job store's form. Such an id that ``lookup`` does not find (unknown, hidden, deleted,
        reaped, or a job of another kind) is unknown; it never falls back to the older records."""
        raw = self._job_id_of(wire_id)
        return raw is not None and self._is_job_id(raw)

    def lookup(self, wire_id: str, caller: CallerContext) -> Hit | None:
        """The item an id names, or None. A job counts only when it is a video job (see ``_is_video``)."""
        raw = self._job_id_of(wire_id)
        if raw is None:
            return None
        if self._is_job_id(raw):
            view = self._jobs.get(raw, caller)
            return JobHit(view) if view is not None and self._is_video(view) else None
        record = self._legacy.find(raw, caller) if self._legacy is not None else None
        return LegacyHit(raw, record) if record is not None else None

    def page(self, caller: CallerContext, limit: int, after: str | None, order: str) -> ListPage:
        boundary: tuple[int, str] | None = None
        if after is not None:
            found = self.lookup(after, caller)
            if found is None:
                raise CursorNotFound(after)
            boundary = found.key
        views, _ = self._jobs.list(caller, intents=self._intents, workflows=tuple(self._video_workflows()), limit=limit + 1,
                                  after_key=boundary, order=order)
        merged: list[Hit] = [JobHit(view) for view in views]
        if self._legacy is not None:
            older = [LegacyHit(raw, record) for raw, record in self._legacy.visible(caller)]
            if boundary is not None:
                older = [hit for hit in older if (hit.key > boundary if order == "asc" else hit.key < boundary)]
            older.sort(key=lambda hit: hit.key, reverse=order != "asc")
            merged.extend(older[:limit + 1])
        merged.sort(key=lambda hit: hit.key, reverse=order != "asc")
        return ListPage(tuple(merged[:limit]), len(merged) > limit)

    def delete(self, wire_id: str, caller: CallerContext) -> Hit | None:
        """Delete a job of the run core and return its hit; an older record is returned untouched for its own deleter.
        Raises ``JobDeleteConflict`` when the cancel is not confirmed and ``JobNotFound`` when the job is gone."""
        found = self.lookup(wire_id, caller)
        if isinstance(found, JobHit):
            self._jobs.delete(found.view.job_id, caller)
        return found
