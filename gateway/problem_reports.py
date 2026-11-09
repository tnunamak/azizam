"""Problem reports from agents (docs/design/MCP_PROBLEM_REPORTS.md).

An agent that uses the MCP tools calls ``report_problem`` when a tool fails, differs from its description or forces a
workaround. This module owns what happens next: it attaches the context the gateway already knows, folds repeats of
one failure into one counted report, stores the text as inert plain text, and tells the operator through destinations
the operator configured as data (the operator setting ``problem_report_destinations``).

Report text is untrusted: an agent may have read hostile content. It is cut to length, stripped of control
characters and scrubbed with ``log_scrub`` (the one secret scrubber), then stored and shown as plain text. Nothing
here renders, executes or interprets it.

The webhook poster and the job lookup are injected, so tests never reach a network and this module names no engine.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, log_scrub, secret_refs, secret_store
from gateway.admin_api import AdminAuth, _require_admin
from gateway.problem_report_tool import CATEGORIES, REPORT_HINT, SUMMARY_MAX, TOOL, WORKAROUND_MAX  # noqa: F401
from gateway.session_guard import SessionEnded

SETTING_KEY = "problem_report_destinations"
TEXT_FORMAT = "plain_text"
STATUSES = ("open", "resolved", "ignored")
DESTINATION_TYPES = ("webhook",)
DESTINATION_FORMATS = ("json", "text")
FAILURE_WINDOW_S = 30 * 60
FAILURES_PER_CALLER = 5
HOURLY_LIMIT = 20
NOTIFY_COUNTS = (1, 10, 100, 1000)
NOTIFY_TIMEOUT_S = 5.0
MAX_APPS = 20

class ProblemReportError(Exception):
    def __init__(self, message: str, code: str = "invalid_report") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Caller:
    app_id: str | None
    app_name: str | None
    person_id: str | None

    @property
    def key(self) -> str:
        return self.app_id or (f"person:{self.person_id}" if self.person_id else "anonymous")


Poster = Callable[..., Awaitable[None]]


async def post_webhook(url: str, *, body: Any, headers: Mapping[str, str], as_json: bool) -> None:
    """The one place this module touches a network. No redirects; any non-2xx status raises."""
    from gateway import engine_egress  # the gateway's one client factory: a destination on an engine address is checked
    async with engine_egress.async_client(timeout=NOTIFY_TIMEOUT_S, follow_redirects=False) as http:
        if as_json:
            response = await http.post(url, json=body, headers=dict(headers))
        else:
            response = await http.post(url, content=str(body).encode("utf-8"), headers=dict(headers))
        response.raise_for_status()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS problem_reports (
  id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  category TEXT NOT NULL,
  tool TEXT, target TEXT, engine TEXT, error_code TEXT, error_text TEXT,
  summary TEXT NOT NULL,
  workaround_used TEXT,
  operator_workaround TEXT,
  count INTEGER NOT NULL,
  apps_json TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  last_context_json TEXT NOT NULL,
  last_notify_error TEXT
);
CREATE INDEX IF NOT EXISTS problem_reports_last_seen ON problem_reports(last_seen);
"""

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NUMBERS = re.compile(r"\d+")
_LONG_ID = re.compile(r"\b[0-9a-f]{8,}\b")


def clean_text(value: Any, limit: int) -> str:
    """Inert plain text: control characters removed, registered secrets scrubbed, cut to ``limit``."""
    text = _CONTROL.sub("", str(value))
    return log_scrub.scrub(text, limit=limit)[:limit]


def _normal(value: Any, limit: int = 200) -> str:
    text = _LONG_ID.sub("#", str(value or "").lower())
    return re.sub(r"\s+", " ", _NUMBERS.sub("#", text)).strip()[:limit]


class ProblemReports:
    def __init__(self, path: Path | Callable[[], Path], *, destinations: Callable[[], list[Mapping[str, Any]]],
                 post: Poster = post_webhook,
                 job_lookup: Callable[[str], Mapping[str, Any] | None] = lambda _job_id: None,
                 secret_for: Callable[[str, str], str] | None = None,
                 now: Callable[[], float] = time.time, hourly_limit: int = HOURLY_LIMIT) -> None:
        # A callable path is read at first use, so building the store at import reads no setting and touches no disk.
        self._path_source = path
        self._schema_path: Path | None = None  # the database whose tables this store has made
        self._destinations, self._post, self._job_lookup, self._now = destinations, post, job_lookup, now
        self._hourly_limit = hourly_limit
        # (secret reference, origin) -> the destination's bearer token. The store refuses another origin.
        self._secret_for = secret_for
        self._failures: dict[str, deque[tuple[float, dict[str, Any]]]] = {}
        self._submitted: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        if not callable(path):
            self._connect().close()

    @property
    def path(self) -> Path:
        source = self._path_source
        return Path(source() if callable(source) else source)

    def _connect(self) -> sqlite3.Connection:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        if self._schema_path != path:
            conn.executescript(_SCHEMA)
            self._schema_path = path
        return conn

    # -- failures the gateway saw ---------------------------------------------------

    def note_failure(self, caller: Caller, info: Mapping[str, Any]) -> None:
        keep = {k: info[k] for k in ("tool", "target", "job_id", "error", "code", "category", "http_status",
                                      "retryable") if info.get(k) is not None}
        if "error" in keep:
            keep["error"] = clean_text(keep["error"], 300)
        with self._lock:
            queue = self._failures.setdefault(caller.key, deque(maxlen=FAILURES_PER_CALLER))
            queue.append((self._now(), keep))

    def _failure_for(self, caller: Caller, target: str | None, job_id: str | None) -> dict[str, Any]:
        cutoff = self._now() - FAILURE_WINDOW_S
        with self._lock:
            recent = [info for ts, info in self._failures.get(caller.key, ()) if ts >= cutoff]
        for info in reversed(recent):
            if (job_id and info.get("job_id") == job_id) or (not job_id and target and info.get("target") == target):
                return info
        if job_id or target:
            return {}
        return recent[-1] if recent else {}

    def _job_facts(self, caller: Caller, job_id: str) -> dict[str, Any]:
        try:
            job = self._job_lookup(job_id)
        except Exception:  # noqa: BLE001 - context is a nicety; the report must still be taken
            return {}
        if not job:
            return {}
        own = (job.get("app_id") == caller.app_id) if caller.app_id else (
            bool(caller.person_id) and job.get("maker_person_id") == caller.person_id)
        if not own:
            return {}
        facts = {"job_id": job_id, "request_id": job.get("request_id"), "workflow": job.get("workflow"),
                 "engine": job.get("backend"), "receipt_id": job.get("creating_receipt_id"),
                 "error_code": job.get("error_code"), "error": job.get("error")}
        return {k: v for k, v in facts.items() if v is not None}

    # -- taking a report -----------------------------------------------------------

    def _validate(self, args: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(args) - set(TOOL["inputSchema"]["properties"]))
        if unknown:
            raise ProblemReportError(f"unknown argument {unknown[0]!r}")
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ProblemReportError("`summary` is required: say what happened")
        category = args.get("category", "failure")
        if category not in CATEGORIES:
            raise ProblemReportError(f"`category` must be one of {', '.join(CATEGORIES)}")
        for name in ("workaround", "target", "job_id"):
            if args.get(name) is not None and not isinstance(args[name], str):
                raise ProblemReportError(f"`{name}` must be text")
        return {"category": category, "summary": clean_text(summary.strip(), SUMMARY_MAX),
                "workaround": clean_text(args["workaround"].strip(), WORKAROUND_MAX) if args.get("workaround") else None,
                "target": (args.get("target") or "").strip()[:128] or None,
                "job_id": (args.get("job_id") or "").strip()[:128] or None}

    def _take_slot(self, caller: Caller) -> None:
        now = self._now()
        with self._lock:
            window = self._submitted.setdefault(caller.key, deque())
            while window and window[0] < now - 3600:
                window.popleft()
            if len(window) >= self._hourly_limit:
                raise ProblemReportError(
                    f"This app has sent {self._hourly_limit} reports in the last hour. Keep going with your task; "
                    "the operator already has them.", code="rate_limited")
            window.append(now)

    async def submit(self, caller: Caller, args: Mapping[str, Any]) -> dict[str, Any]:
        fields = self._validate(args)
        self._take_slot(caller)
        failure = self._failure_for(caller, fields["target"], fields["job_id"])
        job_id = fields["job_id"] or failure.get("job_id")
        job = self._job_facts(caller, job_id) if job_id else {}
        context: dict[str, Any] = {**failure, **{k: v for k, v in job.items() if k not in ("error", "error_code")}}
        context.update(app_id=caller.app_id, app_name=caller.app_name, person_id=caller.person_id)
        tool = failure.get("tool")
        target = failure.get("target") or fields["target"] or job.get("workflow")
        code = failure.get("code") or job.get("error_code")
        error = failure.get("error") or job.get("error")
        if job.get("error") and "error" not in failure:
            context["error"] = clean_text(job["error"], 300)
        if code and "code" not in context:
            context["code"] = code
        if code or error:  # a failure the gateway saw names the problem, whatever words each agent uses
            basis = f"{tool}|{target}|{code}|{_normal(error)}"
        else:  # nothing seen: the agent's own words are the identity, so two problems stay two reports
            basis = f"-|{target}|{fields['category']}|{_normal(fields['summary'], 120)}"
        fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        row, event = self._upsert(fingerprint, fields, caller, context, tool=tool, target=target, code=code,
                                  error=error, engine=job.get("engine"))
        if event:
            await self._notify(row, event)
            row = self.get(row["id"]) or row
        return {"status": "recorded", "report_id": row["id"], "duplicate": row["count"] > 1, "count": row["count"],
                "known_workaround": row["operator_workaround"],
                "note": ("The operator has been told. Use `known_workaround` if it is set." if row["operator_workaround"]
                         else "The operator has been told. Carry on with your task.")}

    def _upsert(self, fingerprint: str, fields: Mapping[str, Any], caller: Caller, context: Mapping[str, Any], *,
                tool: Any, target: Any, code: Any, error: Any, engine: Any) -> tuple[dict[str, Any], str | None]:
        now = int(self._now())
        app = {"app_id": caller.app_id, "app_name": caller.app_name}
        text = lambda value, limit: clean_text(value, limit) if value else None  # noqa: E731
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM problem_reports WHERE fingerprint=?", (fingerprint,)).fetchone()
            context_json = json.dumps(context, sort_keys=True, default=str)
            if old is None:
                rid = "pr_" + secrets.token_hex(8)
                conn.execute(
                    "INSERT INTO problem_reports(id,fingerprint,status,category,tool,target,engine,error_code,error_text,"
                    "summary,workaround_used,count,apps_json,first_seen,last_seen,last_context_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, fingerprint, "open", fields["category"], text(tool, 64), text(target, 128), text(engine, 128),
                     text(code, 64), text(error, 300), fields["summary"], fields["workaround"], 1,
                     json.dumps([app]), now, now, context_json))
                event: str | None = "problem_report.new"
            else:
                rid = old["id"]
                apps = json.loads(old["apps_json"])
                if app not in apps and len(apps) < MAX_APPS:
                    apps.append(app)
                count = old["count"] + 1
                reopened = old["status"] == "resolved"
                conn.execute(
                    "UPDATE problem_reports SET count=?, last_seen=?, apps_json=?, summary=?, workaround_used=?, "
                    "last_context_json=?, status=?, category=? WHERE id=?",
                    (count, now, json.dumps(apps), fields["summary"], fields["workaround"] or old["workaround_used"],
                     context_json, "open" if reopened else old["status"], fields["category"], rid))
                event = ("problem_report.reopened" if reopened
                         else "problem_report.repeat" if count in NOTIFY_COUNTS else None)
            conn.commit()
        return self.get(rid) or {}, event

    # -- notification ----------------------------------------------------------------

    async def _notify(self, row: Mapping[str, Any], event: str) -> None:
        failed = False
        try:
            destinations = [d for d in self._destinations() if d.get("type") in DESTINATION_TYPES
                            and d.get("enabled", True) and d.get("url")]
        except Exception:  # noqa: BLE001
            destinations = []
        for destination in destinations:
            as_json = destination.get("format", "json") == "json"
            body: Any = self._json_body(row, event) if as_json else self._text_body(row)
            headers = {} if as_json else {"Title": self._title(row), "Tags": "warning"}
            try:
                if destination.get("secret_ref"):
                    if self._secret_for is None:
                        raise LookupError("no secret reader")
                    origin = secret_refs.origin_or_none(destination["url"])
                    if origin is None:  # a secret with no origin to check is never sent
                        raise LookupError("no origin")
                    headers["Authorization"] = "Bearer " + self._secret_for(destination["secret_ref"], origin)
                await asyncio.wait_for(self._post(destination["url"], body=body, headers=headers, as_json=as_json),
                                       NOTIFY_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - delivery never fails the agent's call; the reason is a fixed word
                failed = True
        with closing(self._connect()) as conn:
            conn.execute("UPDATE problem_reports SET last_notify_error=? WHERE id=?",
                         ("delivery_failed" if failed else None, row["id"]))
            conn.commit()

    @staticmethod
    def _title(row: Mapping[str, Any]) -> str:
        what = " ".join(part for part in (row.get("tool"), row.get("target")) if part) or row["category"]
        return f"Azizam problem: {what}"[:80].replace("\n", " ")

    @staticmethod
    def _json_body(row: Mapping[str, Any], event: str) -> dict[str, Any]:
        apps = row["apps"]
        return {"event": event, "id": row["id"], "count": row["count"], "category": row["category"],
                "tool": row["tool"], "target": row["target"], "engine": row["engine"], "error_code": row["error_code"],
                "summary": row["summary"], "workaround_used": row["workaround_used"],
                "app": (apps[-1].get("app_name") if apps else None), "first_seen": row["first_seen"],
                "last_seen": row["last_seen"], "text_format": TEXT_FORMAT}

    @classmethod
    def _text_body(cls, row: Mapping[str, Any]) -> str:
        lines = [row["summary"]]
        if row["tool"] or row["target"]:
            lines.append("Where: " + " ".join(p for p in (row["tool"], row["target"]) if p))
        if row["error_code"]:
            lines.append(f"Error: {row['error_code']}")
        if row["workaround_used"]:
            lines.append(f"Agent did instead: {row['workaround_used']}")
        lines.append(f"Seen {row['count']} time(s). Report {row['id']}.")
        return "\n".join(lines)

    # -- reading and editing ---------------------------------------------------------

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["apps"] = json.loads(data.pop("apps_json"))
        data["last_context"] = json.loads(data.pop("last_context_json"))
        data.pop("fingerprint", None)
        data["text_format"] = TEXT_FORMAT
        return data

    def get(self, report_id: str) -> dict[str, Any] | None:
        if not self.path.exists():  # A read never creates the database.
            return None
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM problem_reports WHERE id=?", (report_id,)).fetchone()
        return self._public(row) if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM problem_reports", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY last_seen DESC, id LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        if not self.path.exists():  # A read never creates the database.
            return []
        with closing(self._connect()) as conn:
            return [self._public(row) for row in conn.execute(sql, params)]

    def update(self, report_id: str, *, status: str | None = None, workaround: str | None = None) -> dict[str, Any] | None:
        if status is not None and status not in STATUSES:
            raise ProblemReportError(f"status must be one of {', '.join(STATUSES)}")
        if workaround is not None and (not isinstance(workaround, str) or len(workaround) > WORKAROUND_MAX * 2):
            raise ProblemReportError("workaround must be text of at most 1000 characters")
        with closing(self._connect()) as conn:
            if status is not None:
                conn.execute("UPDATE problem_reports SET status=? WHERE id=?", (status, report_id))
            if workaround is not None:
                conn.execute("UPDATE problem_reports SET operator_workaround=? WHERE id=?",
                             (clean_text(workaround.strip(), WORKAROUND_MAX * 2) or None, report_id))
            conn.commit()
        return self.get(report_id)


# ---------------------------------------------------------------------------- destinations (owner data)


def destinations_from_settings(admin_path: Path) -> list[dict[str, Any]]:
    stored = admin_store.get_operator_setting(SETTING_KEY, path=admin_path)
    value = stored["value"] if stored else {}
    items = value.get("destinations") if isinstance(value, Mapping) else None
    return [dict(item) for item in items or [] if isinstance(item, Mapping)]


SECRET_MAX = 4096


def _check_secret(item: Mapping[str, Any]) -> None:
    """``secret`` is write-only: absent keeps the stored one (same origin), null clears it, text replaces it."""
    if "secret" not in item or item["secret"] is None:
        return
    value = item["secret"]
    if not isinstance(value, str) or not value or len(value) > SECRET_MAX or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ProblemReportError(f"secret must be text of 1 to {SECRET_MAX} characters without spaces or control characters")


def _validate_destinations(items: Any, stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > 10:
        raise ProblemReportError("destinations must be a list of at most 10")
    by_id = {d.get("id"): d for d in stored}
    out, seen = [], set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ProblemReportError("each destination must be an object")
        did = item.get("id")
        if not isinstance(did, str) or not did.strip() or len(did) > 64 or did in seen:
            raise ProblemReportError("each destination needs a unique id of at most 64 characters")
        seen.add(did)
        if item.get("type") not in DESTINATION_TYPES:
            raise ProblemReportError(f"destination type must be one of {', '.join(DESTINATION_TYPES)}")
        fmt = item.get("format", "json")
        if fmt not in DESTINATION_FORMATS:
            raise ProblemReportError(f"format must be one of {', '.join(DESTINATION_FORMATS)}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProblemReportError("enabled must be true or false")
        url = item.get("url") if item.get("url") is not None else (by_id.get(did) or {}).get("url")
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.netloc or re.search(r"\s", url or ""):
            raise ProblemReportError("a webhook destination needs an http or https url")
        new_origin = secret_refs.origin_or_none(url)
        if new_origin is None:
            raise ProblemReportError(f"destinations[{index}].url has no origin the gateway can check; use a plain "
                                     "https://host/path address")
        _check_secret(item)
        if item.get("secret") is not None and parts.scheme != "https":
            raise ProblemReportError(f"destinations[{index}].secret needs an https url: a token must not cross the "
                                     "network in clear text")
        checked = {"id": did, "type": item["type"], "url": url, "format": fmt, "enabled": enabled}
        old = by_id.get(did) or {}
        if "secret" in item:
            checked["secret"] = item["secret"]  # stored as a reference by the route; never kept as text
        elif old.get("secret_ref") and secret_refs.origin_or_none(old.get("url") or "") == new_origin:
            checked["secret_ref"] = old["secret_ref"]
        out.append(checked)
    return out


def _store_secrets(checked: list[dict[str, Any]], stored: list[dict[str, Any]], *, admin_path: Path,
                   session: Any, made: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Swap each new secret for a reference bound to its url's origin; return the references no longer used.

    Each reference stored is added to ``made`` at once, so the caller can take back what a failed save stored."""
    out = []
    for item in checked:
        item = dict(item)
        if "secret" in item:
            value = item.pop("secret")
            if value is not None:
                item["secret_ref"] = secret_refs.store(value, db_path=admin_path, origin=secret_refs.origin_or_none(item["url"]),
                                                       session=session)
                made.append(item["secret_ref"])
        out.append(item)
    kept = {item.get("secret_ref") for item in out}
    return out, [d["secret_ref"] for d in stored if d.get("secret_ref") and d["secret_ref"] not in kept]


def _discard(refs: list[str], path: Path) -> None:
    """Delete stored secrets that nothing references. No sign-in is passed: this clean-up runs even when it has ended."""
    for ref in refs:
        try:
            secret_store.delete_provider_secret(ref, db_path=path)
        except KeyError:
            pass


def _public_destination(item: Mapping[str, Any]) -> dict[str, Any]:
    parts = urlsplit(str(item.get("url") or ""))
    return {"id": item.get("id"), "type": item.get("type"), "origin": f"{parts.scheme}://{parts.netloc}",
            "format": item.get("format", "json"), "enabled": item.get("enabled", True),
            "has_secret": bool(item.get("secret_ref"))}


# ---------------------------------------------------------------------------- admin routes


def create_problem_reports_router(*, auth: AdminAuth, service: ProblemReports,
                                  admin_path: Path | Callable[[], Path]) -> APIRouter:
    def where() -> Path:  # read per request, like the store's own path: the admin database can move
        return Path(admin_path() if callable(admin_path) else admin_path)

    router = APIRouter(prefix="/admin/api/problem-reports", tags=["Problem reports"])

    def guard(request: Request):
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return _error(403, "owner_required", "Only the gateway owner can read problem reports.")
        return ctx

    def _error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    @router.get("")
    def list_reports(request: Request, status: str | None = None, limit: int = 100):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if status is not None and status not in STATUSES:
            return _error(400, "invalid_request", "status must be open, resolved or ignored")
        return {"text_format": TEXT_FORMAT, "reports": service.list(status, limit)}

    @router.get("/destinations")
    def get_destinations(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return {"destinations": [_public_destination(d) for d in destinations_from_settings(where())]}

    @router.put("/destinations")
    async def put_destinations(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            items = body.get("destinations") if isinstance(body, dict) else None
            path = where()
            stored = destinations_from_settings(path)
            checked = _validate_destinations(items, stored)
        except ProblemReportError as exc:
            return _error(400, "invalid_request", str(exc))
        except ValueError:
            return _error(400, "invalid_request", "the body must be JSON")
        session = getattr(ctx, "browser_session", None)
        made: list[str] = []
        try:  # all or nothing: a failure at any step takes back the secrets this request stored
            checked, unused = _store_secrets(checked, stored, admin_path=path, session=session, made=made)
            admin_store.put_operator_setting(SETTING_KEY, {"destinations": checked}, updated_by=ctx.subject, path=path)
        except BaseException as exc:
            _discard(made, path)
            if isinstance(exc, SessionEnded):
                return _error(401, exc.code, exc.message)
            raise
        _discard(unused, path)  # a replaced, cleared or moved secret is deleted, not left behind
        return {"destinations": [_public_destination(d) for d in checked]}

    @router.get("/{report_id}")
    def get_report(report_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        row = service.get(report_id)
        return row if row else _error(404, "report_not_found", "No such report.")

    @router.patch("/{report_id}")
    async def patch_report(report_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"status", "workaround"}:
                raise ProblemReportError("send status and/or workaround")
            row = service.update(report_id, status=body.get("status"), workaround=body.get("workaround"))
        except ProblemReportError as exc:
            return _error(400, "invalid_request", str(exc))
        except ValueError:
            return _error(400, "invalid_request", "the body must be JSON")
        return row if row else _error(404, "report_not_found", "No such report.")

    return router
