"""Normal-user account view over the durable admin store.

The user portal is intentionally narrow. It lets a signed-in OIDC user create
user-owned Apps, see non-secret API-key records, create keys for Apps they own,
inspect owned-key access, and delete their own keys or Apps. A person's own
App is always "Same as my access plan"; choosing plans and changing App
ownership remain admin-plane actions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
from collections import deque
from collections.abc import Callable, Iterator
from functools import lru_cache
import time
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway import admin_store, avatars, session_guard, session_policy, trusted_addresses
from gateway import receipt_store
from gateway.content_access import Own, ReceiptRecord, Viewer, decide
from gateway import content_makers
from gateway.receipt_context import person_label, receipt_context
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import app_key_prompt_memory, generate_app_api_key
from gateway.runtime_auth import hash_api_key

__all__ = [
    "UserPortalError",
    "build_user_portal_snapshot",
    "create_user_app",
    "delete_user_app",
    "get_user_app_credential",
    "mint_user_app_key",
    "rename_person_app",
    "rename_person_key",
    "rename_user_app",
    "rename_user_app_key",
    "request_user_access",
    "revoke_user_app_key",
    "set_user_app_key_privacy_permission",
]


@lru_cache(maxsize=128)
def _server_secret(path: Path) -> bytes:
    """The store's dedicated persistent 32-byte secret. Each purpose key is derived from it under its own label.

    Publish a complete private file without replacing a concurrent winner.
    Never follow a leaf symlink, including dangling links.
    """
    from gateway import private_io
    private_io.ensure_private_dir(path.parent)
    if not path.exists() and not path.is_symlink():
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".session-view-") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(secrets.token_bytes(32))
            handle.flush()
            os.fsync(handle.fileno())
            try:
                os.link(handle.name, path)
            except FileExistsError:
                pass
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("session view key must be a regular file")
        os.fchmod(handle.fileno(), 0o600)
        key = handle.read(33)
    if len(key) != 32:
        raise ValueError("session view key must contain 32 bytes")
    return key


def _session_view_key(path: Path) -> bytes:
    """The key that makes session view ids: the secret at ``path``, derived under the session-view label."""
    return hmac.new(_server_secret(path), b"azizam.session-view.v1", hashlib.sha256).digest()


def _session_view_secret_path(admin_path: Path) -> Path:
    """The secret file beside the admin store. The session-view and activity-cursor keys both derive from it."""
    return admin_path.parent / ".session_view_secret"


def session_view_key_problem(path: Path) -> str | None:
    """Why the session view key cannot be used right now, or None when it can. A key that cannot be read is not
    cached, so the next call reads the file again and a repaired file takes effect on the next request."""
    try:
        _session_view_key(_session_view_secret_path(path))
    except (OSError, ValueError) as exc:
        return exc.__class__.__name__
    return None


def _session_view_field(session: AdminSession, path: Path) -> dict[str, str]:
    """The public view id of a signed-in browser session. Every signed-in session has one, service-account
    sessions included: the browser's pages compare it with the sign-in they show (web/src/lib/session-end.ts).
    When it cannot be made, the account is unavailable (503) rather than signed in without one."""
    try:
        key = _session_view_key(_session_view_secret_path(path))
    except (OSError, ValueError):
        logging.getLogger(__name__).error("Session view id unavailable: dedicated key cannot be read")
        raise UserPortalError("session_view_unavailable",
                              "Your sign-in cannot be checked right now. Try again.", status_code=503) from None
    return {"session_view_id": hmac.new(key, session.family_id.encode(), hashlib.sha256).hexdigest()}


class UserPortalError(ValueError):
    """Raised when a user-plane account action is not allowed."""

    def __init__(self, code: str, message: str, *, status_code: int = 400, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})  # extra fields for the error body, such as the administrators to confirm


def _sanitize_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {"hash", "api_key", "key", "secret", "session_id", "session_timing_json"}
    out = {k: v for k, v in record.items() if k not in blocked}
    if "session_id" in record:
        # Name the binding, never the sign-in session it is bound to.
        out["session_bound"] = record["session_id"] is not None
    return out


def _key_prefix(api_key: str) -> str:
    return api_key[:18]


def _credential_active(record: Mapping[str, Any]) -> bool:
    return record.get("revoked_at") is None


def _credential_not_found() -> UserPortalError:
    """Return the single public 404 for every inaccessible App credential.

    Credential lookup has to inspect the stored record before it can prove App
    ownership.  That internal distinction must not escape through the portal:
    a missing, inactive, wrong-kind, or another user's credential is the same
    public result.
    """
    return UserPortalError(
        "credential_not_found",
        "API key not found for this account",
        status_code=404,
    )


def _plan_summary(
    app: Mapping[str, Any], *, path: Path, listing: tuple[str | None, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The plan an App runs under, as the portal shows it: its name and summary, no rules.

    ``listing`` holds the person's plan id and every plan, read once for a listing
    of one person's Apps.
    """
    from gateway.access_plans import PlanError, plan_from_document, plan_summary

    assignment = app.get("access_plan")
    same_as_person = assignment == admin_store.PERSON_ASSIGNMENT
    if not same_as_person:
        plan_id = assignment
    elif listing is not None:
        plan_id = listing[0]
    else:
        plan_id = admin_store.person_plan_id(str(app.get("owner_user_id") or ""), path=path)
    if not plan_id:
        record = None
    elif listing is not None:
        record = listing[1].get(str(plan_id))
    else:
        record = admin_store.get_access_plan(str(plan_id), path=path)
    if record is None:
        return None
    try:
        summary = plan_summary(plan_from_document(record["plan"], plan_id=str(record["id"])))
    except PlanError:
        summary = None
    return {"id": record["id"], "name": record["name"], "summary": summary, "same_as_person": same_as_person}


def _credentials_for_app(
    app_id: str, *, path: Path, active: Mapping[str, list[dict[str, Any]]] | None = None,
    cutover: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """``active`` holds every listed app's credentials, read in one query. ``cutover`` holds the remedy of each key the
    cut-over would remove (``admin_store.cutover_for_bindings``); a key the cut-over keeps carries none."""
    credentials = []
    for record in (admin_store.list_active_credentials(subject_kind="app", app_id=app_id, path=path)
                   if active is None else active.get(app_id, ())):
        item = _sanitize_credential(record)
        item["active"] = True
        item.update(cutover.get(record["id"], {}) if cutover else {})
        credentials.append(item)
    return credentials


def _app_belongs_to_user(app_id: str, user_id: str, *, path: Path) -> dict[str, Any]:
    app = admin_store.get_app(app_id, path=path)
    # The keyless session App is not a self-service App: it never gets a key.
    if (app is None or app.get("owner_user_id") != user_id
            or admin_store.is_session_app(app_id, path=path)):
        raise UserPortalError(
            "app_not_found",
            "App not found for this account",
            status_code=404,
        )
    _refuse_disabled(app)
    return app


def _refuse_disabled(app: Mapping[str, Any]) -> None:
    if app.get("disabled_at"):
        raise UserPortalError(
            "app_disabled",
            "App is disabled",
            status_code=403,
        )


def _user_from_session(session: AdminSession, *, path: Path) -> dict[str, Any]:
    user = admin_store.get_user(session.subject, path=path)
    if user is None:
        raise UserPortalError(
            "session_user_not_found",
            "Signed-in user was not found in the admin store",
            status_code=401,
        )
    return user


def _live_role_for_user(user_id: str, *, path: Path) -> str:
    roles = admin_store.effective_admin_roles("user", user_id, path=path)
    for role in ("owner", "superadmin", "admin"):
        if role in roles:
            return role
    return "none"


def _cutover_view(
    person_id: str, apps: list[dict[str, Any]], credentials_by_app: Mapping[str, list[dict[str, Any]]], *,
    owner: bool, path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, int] | None]:
    """The cut-over's remedy for each binding on these Apps, and for the owner, how many trusted addresses on each App
    have one (owner visibility step 2). Keys are read for every person; addresses only for the owner, who lists them."""
    keys = [key for app in apps for key in credentials_by_app.get(str(app["id"]), [])]
    addresses: list[dict[str, Any]] = []
    if owner and apps:
        listed = {str(app["id"]) for app in apps}
        addresses = [entry for entry in admin_store.list_trusted_addresses(path=path) if entry["app_id"] in listed]
    view = admin_store.cutover_for_bindings(person_id, keys=keys, addresses=addresses, path=path)
    if not owner:
        return view, None
    counts: dict[str, int] = {}
    for entry in addresses:
        if entry["id"] in view:
            counts[entry["app_id"]] = counts.get(entry["app_id"], 0) + 1
    return view, counts


def _app_payload(
    app: Mapping[str, Any],
    *,
    path: Path,
    credentials_by_app: Mapping[str, list[dict[str, Any]]] | None = None,
    plan_listing: tuple[str | None, Mapping[str, Any]] | None = None,
    cutover: Mapping[str, Mapping[str, str]] | None = None,
    addresses_to_check: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    credentials = _credentials_for_app(str(app["id"]), path=path, active=credentials_by_app, cutover=cutover)
    plan = _plan_summary(app, path=path, listing=plan_listing)
    payload: dict[str, Any] = {
        "id": app.get("id"),
        "kind": app.get("kind"),
        "name": app.get("name"),
        "disabled_at": app.get("disabled_at"),
        "access_plan": plan,
        "can_mint_keys": not app.get("disabled_at"),
        "can_delete": not bool(app.get("disabled_at")),
        "credentials": credentials,
        "active_credentials": len(credentials),
        "created_at": app.get("created_at"),
    }
    # Sent to the owner only: the owner is the one who lists and keeps an App's trusted addresses. Keys carry
    # their remedy in `credentials`, so the Apps page counts those itself.
    if addresses_to_check is not None:
        payload["addresses_to_check"] = addresses_to_check.get(str(app["id"]), 0)
    return payload


def build_user_portal_snapshot(
    session: AdminSession | None,
    *,
    auto_create_personal_app: bool = False,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return the account data safe to embed in ``/me``.

    Missing or invalid sessions are represented as an unauthenticated snapshot
    so the HTML page can still serve the paste-a-key flow.
    """
    if session is None:
        return {"authenticated": False, "apps": []}
    try:
        user = _user_from_session(session, path=path)
    except UserPortalError as exc:
        return {
            "authenticated": False,
            "apps": [],
            "error": {"code": exc.code, "message": exc.message},
            # A browser session with no person behind it is still a session: it keeps its view id.
            **_session_view_field(session, path),
        }

    if auto_create_personal_app:
        label = user.get("email") or user.get("display_name") or user["id"]
        admin_store.ensure_personal_app_for_user(
            str(user["id"]),
            name=f"{label} personal access",
            path=path,
        )

    # Session apps, keys and plans for every listed app are read in one query each,
    # so the number of statements does not grow with the number of apps.
    owned = admin_store.list_apps_for_user(user["id"], path=path)
    sessions = admin_store.session_app_ids([str(app["id"]) for app in owned], path=path)
    listed = [app for app in owned if not app.get("disabled_at") and str(app["id"]) not in sessions]
    credentials_by_app = admin_store.list_active_credentials_for_apps([str(app["id"]) for app in listed], path=path)
    plan_listing = (
        admin_store.person_plan_id(str(user["id"]), path=path),
        {str(plan["id"]): plan for plan in admin_store.list_access_plans(path=path)},
    ) if listed else None
    role = _live_role_for_user(str(user["id"]), path=path)
    cutover, addresses_to_check = _cutover_view(
        str(user["id"]), listed, credentials_by_app, owner=role == "owner", path=path,
    )
    apps: list[dict[str, Any]] = [
        _app_payload(
            app, path=path, credentials_by_app=credentials_by_app, plan_listing=plan_listing,
            cutover=cutover, addresses_to_check=addresses_to_check,
        )
        for app in listed
    ]

    person_id = str(user["id"])
    return {
        "authenticated": True,
        # Public per-sign-in epoch, stable across renewal, distinct from every credential.
        **_session_view_field(session, path),
        "user": {
            "id": user.get("id"),
            # The stable id the generated avatar is drawn from (a rename keeps the colour), and the
            # choice. The picture URL is a gateway path, never the identity provider's address.
            "person_id": person_id,
            "avatar": avatars.public_view(person_id, avatars.get(person_id, path=path), viewer_id=person_id),
            "email": user.get("email") or session.email,
            "name": user.get("display_name") or user.get("email") or session.email,
            "role": role,
            # A new App starts on this plan ("Same as you"), so the New app
            # dialog can say what it will be allowed to use before it exists.
            "access_plan": _plan_summary(
                {"access_plan": admin_store.PERSON_ASSIGNMENT, "owner_user_id": str(user["id"])},
                path=path,
                listing=plan_listing,
            ),
        },
        "apps": apps,
    }


# Activity feed default page size. Small on purpose: the user portal asks "did
# my recent calls work?", not "give me a filterable log". Larger pages are an
# admin concern.
USER_ACTIVITY_DEFAULT_LIMIT = 20
USER_ACTIVITY_MAX_LIMIT = 100
# Receipts one feed request may read, whether or not they make the page. A page of rows the person may not read ends
# here, with a cursor for the next request, so no request scans the whole receipt store.
USER_ACTIVITY_SCAN_LIMIT = 1000
_ACTIVITY_BATCH = 50


def _owned_app_ids(user_id: str, *, path: Path) -> list[str]:
    """Return the ids of every App owned by ``user_id`` (the receipt scope).

    This is the privacy boundary for the activity feed: only receipts whose
    ``app_id`` is in this server-derived set are ever shown to the user. The
    set is computed from ownership in the admin store — never from a
    client-supplied filter.
    """
    return [
        str(app["id"])
        for app in admin_store.list_apps_for_user(user_id, path=path)
        if app.get("id")
    ]


def build_user_activity_snapshot(
    session: AdminSession | None,
    *,
    limit: int = USER_ACTIVITY_DEFAULT_LIMIT,
    path: Path = admin_store.DB_PATH,
    receipts_path: Path = receipt_store.DB_PATH,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return the recent receipts the signed-in person made (D51).

    Owning an App does not make its requests the owner's. The candidates are the Apps the person owns and the
    receipts the person made, through any App. Each row is then read by its maker. Candidates are read newest first
    in batches, each authorized before it counts toward the page. ``cursor`` is a previous page's ``next_cursor``;
    one this person was not issued, or one that is malformed or expired, is refused with a 400.
    """
    if session is None:
        return {"authenticated": False, "receipts": []}
    try:
        user = _user_from_session(session, path=path)
    except UserPortalError as exc:
        return {
            "authenticated": False,
            "receipts": [],
            "error": {"code": exc.code, "message": exc.message},
        }

    safe_limit = max(1, min(int(limit or USER_ACTIVITY_DEFAULT_LIMIT), USER_ACTIVITY_MAX_LIMIT))
    user_id = str(user["id"])
    before = _open_activity_cursor(cursor, viewer_id=user_id, admin_path=path) if cursor else None
    owned = _owned_app_ids(user_id, path=path)
    viewer = Viewer(person_id=user_id, auth_kind="session")
    sources = [_newest_receipts(receipts_path, app_ids=owned)] if owned else []
    sources.append(_newest_receipts(receipts_path, maker_person_id=user_id))
    scan = _ActivityScan(sources, before=before)
    apps = content_makers.Apps(path)
    allowed: list[str] = []
    # A row this person may not read is passed over without taking a place on the page.
    for row in scan:
        app_id = str(row.get("app_id") or "") or None
        makers = content_makers.row_makers(
            explicit=[str(row.get("maker_person_id") or "")], app_id=app_id,
            owner_keys=[str(row.get("owner_key_hash") or "")], apps=apps,
        )
        record = ReceiptRecord(person_id=makers[0] if len(makers) == 1 else None, app_id=app_id)
        if isinstance(decide(viewer, record, "read"), Own):
            allowed.append(str(row["request_id"]))
            if len(allowed) == safe_limit:
                break
    has_more = scan.pending()
    visible = receipt_store.get_many(allowed, audience="client", viewer_person_id=user_id, path=receipts_path)
    return {
        "authenticated": True,
        # No fabricated rows: an empty list means the user has made no
        # attributable calls yet (or none in the retention window).
        "receipts": [visible[request_id] for request_id in allowed if request_id in visible],
        "scoped_app_count": len(owned),
        "scope": "me",
        # A page ends at its size or at the scan limit. The cursor resumes after the last receipt read.
        "has_more": has_more,
        "next_cursor": (_seal_activity_cursor(scan.last_key, viewer_id=user_id, admin_path=path)
                        if has_more and scan.last_key else None),
    }


# An activity cursor is sealed with AES-GCM under a key derived from the store's secret, and it is bound to the person it
# was issued to. The client cannot read the position it names, cannot take it to another person's feed, and cannot use
# it after ACTIVITY_CURSOR_TTL_SECONDS. Every refused cursor gets the same 400.
ACTIVITY_CURSOR_TTL_SECONDS = 60 * 60
_ACTIVITY_CURSOR_LABEL = b"azizam.activity-cursor.v1"
_ACTIVITY_CURSOR_NONCE_BYTES = 12
_ACTIVITY_CURSOR_MAX_CHARS = 1024
_ACTIVITY_CURSOR_ID_MAX_CHARS = 128
# SQLite stores an integer in 64 bits. A position outside that range would fail inside the query, not at the client.
_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2**63), 2**63 - 1


def _activity_now() -> int:
    return int(time.time())


def _is_sqlite_int(value: object) -> bool:
    # ``type(...) is int`` also refuses bool, and a float such as the JSON number 1e999 (infinity).
    return type(value) is int and _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX


def _activity_cursor_aad(viewer_id: str) -> bytes:
    return _ACTIVITY_CURSOR_LABEL + b"\0" + viewer_id.encode("utf-8")


def _activity_cursor_key(admin_path: Path) -> bytes:
    secret = _server_secret(_session_view_secret_path(admin_path))
    return hmac.new(secret, _ACTIVITY_CURSOR_LABEL, hashlib.sha256).digest()


def _activity_cursor_key_or_unavailable(admin_path: Path) -> bytes:
    try:
        return _activity_cursor_key(admin_path)
    except (OSError, ValueError):
        logging.getLogger(__name__).error("Activity cursor unavailable: dedicated key cannot be read")
        raise UserPortalError("activity_unavailable", "Your activity cannot be loaded right now. Try again.",
                              status_code=503) from None


def _seal_activity_cursor(position: tuple[int, str], *, viewer_id: str, admin_path: Path) -> str:
    plaintext = json.dumps(
        {"created_at": position[0], "exp": _activity_now() + ACTIVITY_CURSOR_TTL_SECONDS, "request_id": position[1]},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    nonce = secrets.token_bytes(_ACTIVITY_CURSOR_NONCE_BYTES)
    key = _activity_cursor_key_or_unavailable(admin_path)
    sealed = AESGCM(key).encrypt(nonce, plaintext, _activity_cursor_aad(viewer_id))
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")


def _open_activity_cursor(cursor: str, *, viewer_id: str, admin_path: Path) -> tuple[int, str]:
    """The feed position a cursor names. A cursor that is malformed, altered, issued to someone else or expired gets the
    same 400, so the answer does not say which check failed."""
    key = _activity_cursor_key_or_unavailable(admin_path)
    refused = UserPortalError("invalid_cursor", "This page of activity is no longer valid. Load the newest page again.",
                              status_code=400)
    if len(cursor) > _ACTIVITY_CURSOR_MAX_CHARS:
        raise refused
    try:
        blob = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except ValueError:
        raise refused from None
    # Only the spelling this server writes is accepted, so a cursor that decodes to the same bytes is refused too.
    if base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=") != cursor:
        raise refused
    try:
        plaintext = AESGCM(key).decrypt(blob[:_ACTIVITY_CURSOR_NONCE_BYTES], blob[_ACTIVITY_CURSOR_NONCE_BYTES:],
                                        _activity_cursor_aad(viewer_id))
        data = json.loads(plaintext)
    except (ValueError, InvalidTag):
        raise refused from None
    # The seal proves this server wrote the cursor, but its fields still reach a query, so they are checked too.
    if not isinstance(data, dict) or set(data) != {"created_at", "exp", "request_id"}:
        raise refused
    created_at, request_id, expires_at = data["created_at"], data["request_id"], data["exp"]
    if not (_is_sqlite_int(created_at) and _is_sqlite_int(expires_at)) or expires_at < _activity_now():
        raise refused
    if type(request_id) is not str or not 0 < len(request_id) <= _ACTIVITY_CURSOR_ID_MAX_CHARS:
        raise refused
    return created_at, request_id


def _newest_receipts(receipts_path: Path, **filters: Any) -> Callable[[tuple[int, str] | None, int], list[dict[str, Any]]]:
    """One source of candidate receipts: the newest page after a cursor, read with the same filters each time."""
    def read(before: tuple[int, str] | None, limit: int) -> list[dict[str, Any]]:
        return receipt_store.recent(limit=limit, audience="internal", before=before, path=receipts_path, **filters)
    return read


class _ActivityScan:
    """Candidate receipts from several sources, newest first, each once, read in batches.

    A source is read only when its next row is needed, and no request reads past ``USER_ACTIVITY_SCAN_LIMIT`` rows.
    When the limit stops the scan, rows already read are still returned, and nothing newer than ``last_key`` is left
    unread, so a cursor made from ``last_key`` resumes without a gap.
    """

    def __init__(self, sources: list[Callable[[tuple[int, str] | None, int], list[dict[str, Any]]]],
                 *, before: tuple[int, str] | None) -> None:
        self._sources = sources
        self._fetch_after = [before] * len(sources)
        self._queues: list[deque[dict[str, Any]]] = [deque() for _ in sources]
        self._exhausted = [False] * len(sources)
        self._read = 0
        self._seen: set[str] = set()
        self.last_key: tuple[int, str] | None = None

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[int, str]:
        return int(row.get("_stored_at") or 0), str(row["request_id"])

    def _fill(self, index: int) -> None:
        if self._queues[index] or self._exhausted[index]:
            return
        want = min(_ACTIVITY_BATCH, USER_ACTIVITY_SCAN_LIMIT - self._read)
        if want <= 0:
            return
        rows = self._sources[index](self._fetch_after[index], want)
        self._read += len(rows)
        self._exhausted[index] = len(rows) < want
        if rows:
            self._fetch_after[index] = self._key(rows[-1])
        self._queues[index].extend(rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            for index in range(len(self._sources)):
                self._fill(index)
            if any(not queue and not self._exhausted[index] for index, queue in enumerate(self._queues)):
                # The limit stopped a source whose next row is unknown, so a newer row may be unread. Stop here.
                return
            heads = [(self._key(queue[0]), index) for index, queue in enumerate(self._queues) if queue]
            if not heads:
                return
            _, index = max(heads)
            row = self._queues[index].popleft()
            self.last_key = self._key(row)
            request_id = str(row["request_id"])
            if request_id in self._seen:
                continue
            self._seen.add(request_id)
            yield row

    def pending(self) -> bool:
        """Whether a later request may find more candidates after the rows this scan has read."""
        return any(self._queues) or not all(self._exhausted)


def get_user_receipt(
    session: AdminSession | None,
    request_id: str,
    *,
    path: Path = admin_store.DB_PATH,
    receipts_path: Path = receipt_store.DB_PATH,
) -> dict[str, Any]:
    """Return one client-safe receipt the signed-in person made.

    It carries the person, App and time the Activity list shows (``receipt_context``). Owning an App does
    not let its owner open a member's receipt (D51). An absent receipt and one the person may not read get
    the same 404, so existence is not disclosed.
    """
    if session is None:
        raise UserPortalError(
            "not_authenticated", "Sign in to view this receipt", status_code=401
        )
    user = _user_from_session(session, path=path)
    user_id = str(user["id"])
    not_found = UserPortalError(
        "receipt_not_found", "Receipt not found for this account", status_code=404
    )
    stored = receipt_store.get(request_id, audience="internal", path=receipts_path)
    if stored is None:
        raise not_found
    app_id = str(stored.get("app_id") or "") or None
    makers = content_makers.row_makers(
        explicit=[str(stored.get("maker_person_id") or "")], app_id=app_id,
        owner_keys=[str(stored.get("owner_key_hash") or "")], apps=content_makers.Apps(path),
    )
    record = ReceiptRecord(person_id=makers[0] if len(makers) == 1 else None, app_id=app_id)
    if not isinstance(decide(Viewer(person_id=user_id, auth_kind="session"), record, "read"), Own):
        raise not_found

    # The full request-history entry goes only to the maker the receipt names. Owning its App does not make a viewer
    # the maker, so the App's owner sees kind and decision only, as anyone else does.
    found = receipt_store.get_with_created_at(
        request_id, audience="client", viewer_person_id=str(user["id"]), path=receipts_path,
    )
    if found is None:
        raise not_found
    detail, created_at = found
    app = admin_store.get_app(app_id, path=path) if app_id else None
    app = app or {}
    maker = makers[0] if len(makers) == 1 else None
    person = admin_store.get_user(maker, path=path) if maker else None
    context = receipt_context(
        detail, created_at, app=app,
        person_labels={person["id"]: person_label(person)} if person else {},
        person_id=maker,
    )
    return {**detail, **{key: context[key] for key in ("created_at", "person_label", "app_label")}}


def mint_user_app_key(
    session: AdminSession,
    app_id: str,
    *,
    label: str | None = None,
    expires_at: int | None = None,
    session_id: str | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create an API key for an App already owned by ``session.subject``.

    The insert checks in its own transaction that the sign-in is still live (``gateway/session_guard.py``),
    so a sign-out that lands between the check on the cookie and the insert leaves no key behind, for a
    permanent key as for a Try key. ``session_id`` binds the key to that sign-in (its family, so a
    renewal keeps it), which revokes it on sign-out.
    """
    user = _user_from_session(session, path=path)
    app = _app_belongs_to_user(app_id, str(user["id"]), path=path)
    api_key = generate_app_api_key()

    try:
        record = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=str(app["id"]),
            hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key),
            display_name=label or "Self-service key",
            created_by=str(user["id"]),
            expires_at=expires_at,
            session_id=session_id,
            session_timing=session_policy.lifetime_claims(session) if session_id else None,
            session=session_guard.BrowserSession.of(session, str(user["id"])),
            path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    admin_store.record_audit(
        "credential.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="credential",
        target_id=record["id"],
        metadata={"app_id": app["id"], "prefix": record["prefix"]},
        path=path,
    )
    out = _sanitize_credential(record)
    out["active"] = True
    out["api_key"] = api_key
    out["prompt_memory"] = app_key_prompt_memory(app, record)
    return out


def add_user_trusted_address(
    session: AdminSession,
    app_id: str,
    data: Mapping[str, Any],
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Trust a client address for an App the signed-in person answers for (owner visibility step 2).

    The same rule as the admin route: only the accountable person adds an address, and the address
    records them as its creator. The insert runs in the sign-in's own transaction, so a sign-out that
    lands first adds nothing. The App is looked up the way key minting looks it up: another person's
    App is not found (404), a disabled App is refused (403), and a session App takes no address.
    """
    user = _user_from_session(session, path=path)
    _app_belongs_to_user(app_id, str(user["id"]), path=path)
    try:
        address = trusted_addresses.normalize_entry(data.get("address"))
        record = admin_store.add_trusted_address(
            app_id, address, note=str(data.get("note") or "")[:200], created_by=str(user["id"]),
            max_entries=trusted_addresses.MAX_ENTRIES_PER_APP,
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    except ValueError as exc:
        raise UserPortalError("invalid_trusted_address", str(exc), status_code=400) from None
    admin_store.record_audit(
        "app.trusted_address.add",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=app_id,
        metadata={"address": address, "entry_id": record["id"]},
        path=path,
    )
    return record


def _own_credential(credential_id: str, user: Mapping[str, Any], *, path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The live App key the signed-in person answers for, and its App (owner visibility §3.5).

    One ownership-scoped lookup. Every other key answers as a missing one does: another person's key, a key on a session
    App, a revoked key, or a key whose App is gone. The App's own refusal (disabled) comes only once the App is known to
    be the person's.
    """
    found = admin_store.person_app_credential(credential_id, str(user["id"]), path=path)
    if found is None:
        raise _credential_not_found()
    credential, app = found
    _refuse_disabled(app)
    return credential, app


KEEP_KEY_REFUSED = (
    "Nobody can prove who made this key, so it cannot be kept. Replace it instead: "
    "POST /me/api/credentials/{credential_id}/replace issues a new key on this App (shown once) and revokes this one."
)
# One step: an App holds each address once, so a separate add could not come before the removal of the old entry.
KEEP_ADDRESS_REFUSED = (
    "Nobody can prove who added this address, so it cannot be kept. Add it again with "
    "POST /me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd, which replaces it in one step."
)
KEEP_NEEDS_CONFIRMATION = (
    "Administrators at the time could also have made this {what}: {names}. Confirm that and keep it again with "
    "the body {{\"confirm_admins_could_have_made_it\": true}}."
)


def _keep_needs_confirmation(exc: admin_store.KeepNeedsConfirmation, what: str) -> UserPortalError:
    return UserPortalError(
        "keep_needs_confirmation",
        KEEP_NEEDS_CONFIRMATION.format(what=what, names=admin_store.administrators_sentence(exc.administrators)),
        status_code=409,
        details={"administrators": dict(exc.administrators)},
    )


def adopt_user_app_key(
    session: AdminSession,
    credential_id: str,
    *,
    confirmed_administrators: bool = False,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Keep an App key on an App the signed-in person answers for: they become its recorded creator.

    Only the owner keeps, and only a key nobody else could have made (owner visibility §3.5). Any other key is refused
    with ``keep_not_provable``, and the person replaces it instead (``replace_user_app_key``). When an administrator on
    record could also have made it, the keep needs ``confirmed_administrators``, otherwise ``keep_needs_confirmation``
    names them.
    """
    user = _user_from_session(session, path=path)
    _, app = _own_credential(credential_id, user, path=path)
    try:
        record = admin_store.adopt_app_credential(
            credential_id, app_id=str(app["id"]), person_id=str(user["id"]),
            confirmed_administrators=confirmed_administrators,
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    except admin_store.KeepNotProvable:
        raise UserPortalError("keep_not_provable", KEEP_KEY_REFUSED.format(credential_id=credential_id),
                              status_code=409) from None
    except admin_store.KeepNeedsConfirmation as exc:
        raise _keep_needs_confirmation(exc, "key") from None
    if record is None:  # revoked since the check above
        raise _credential_not_found()
    out = _sanitize_credential(record)
    out["active"] = True
    return out


def replace_user_app_key(
    session: AdminSession,
    credential_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Replace an App key the signed-in person answers for: a new key on the same App, and this one revoked.

    The way a person keeps using a key that cannot be kept. The new key has the old key's name and is made the
    way a key from the Apps page is made (no expiry). The secret is returned once, with the replaced id.
    """
    user = _user_from_session(session, path=path)
    credential, app = _own_credential(credential_id, user, path=path)
    app_id = str(app["id"])
    api_key = generate_app_api_key()
    try:
        record = admin_store.replace_app_credential(
            credential_id, app_id=app_id, person_id=str(user["id"]), hash=hash_api_key(api_key),
            prefix=_key_prefix(api_key), display_name=credential.get("display_name") or "Self-service key",
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if record is None:  # revoked since the check above
        raise _credential_not_found()
    out = _sanitize_credential(record)
    out["active"] = True
    out["api_key"] = api_key
    out["prompt_memory"] = app_key_prompt_memory(app, record)
    return {"credential": out, "replaced_credential_id": credential_id}


def adopt_user_trusted_address(
    session: AdminSession,
    app_id: str,
    entry_id: str,
    *,
    confirmed_administrators: bool = False,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Keep a trusted address on an App the signed-in person answers for: they become its recorded creator.

    Only the owner keeps, and only an address nobody else could have added. Any other address is refused with
    ``keep_not_provable``: the person adds the address again (``readd_user_trusted_address``). Another person's App is
    not found (404), as it is for adding an address to it.
    """
    user = _user_from_session(session, path=path)
    _app_belongs_to_user(app_id, str(user["id"]), path=path)
    try:
        record = admin_store.adopt_trusted_address(
            app_id, entry_id, person_id=str(user["id"]), confirmed_administrators=confirmed_administrators,
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    except admin_store.KeepNotProvable:
        raise UserPortalError("keep_not_provable", KEEP_ADDRESS_REFUSED.format(app_id=app_id, entry_id=entry_id),
                              status_code=409) from None
    except admin_store.KeepNeedsConfirmation as exc:
        raise _keep_needs_confirmation(exc, "address") from None
    if record is None:
        raise UserPortalError("trusted_address_not_found", "Trusted address not found for this App", status_code=404)
    return record


def remove_user_trusted_address(
    session: AdminSession,
    app_id: str,
    entry_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Remove one trusted address from an App the signed-in person answers for.

    A removal only narrows access. Its audit event is written in the same transaction as the removal, and the sign-in
    is checked there too. Another person's App gets the same 404 as adding an address to it (``_app_belongs_to_user``).
    """
    user = _user_from_session(session, path=path)
    _app_belongs_to_user(app_id, str(user["id"]), path=path)
    try:
        removed = admin_store.delete_trusted_address(
            app_id, entry_id, path=path, session=session_guard.BrowserSession.of(session, str(user["id"])),
            audit={"action": "app.trusted_address.remove", "actor_kind": "user", "actor_id": str(user["id"])},
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if removed is None:
        raise UserPortalError("trusted_address_not_found", "Trusted address not found for this App", status_code=404)
    return removed


def readd_user_trusted_address(
    session: AdminSession,
    app_id: str,
    entry_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Add an unkept trusted address again on an App the signed-in person answers for, as its creator.

    The way a person keeps an address that cannot be kept: the entry is replaced by the same address, made by them, in
    one transaction with the audit event and the sign-in check (``admin_store.readd_trusted_address``). Another person's
    App, and a session App, get the same 404 as adding an address to them (``_app_belongs_to_user``).
    """
    user = _user_from_session(session, path=path)
    _app_belongs_to_user(app_id, str(user["id"]), path=path)
    try:
        readded = admin_store.readd_trusted_address(
            app_id, entry_id, created_by=str(user["id"]),
            audit={"action": "app.trusted_address.readd", "actor_kind": "user", "actor_id": str(user["id"])},
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if readded is None:
        raise UserPortalError("trusted_address_not_found", "Trusted address not found for this App", status_code=404)
    return readded


def ensure_try_session_key(
    session: AdminSession,
    *,
    reuse_id: str | None = None,
    now: int | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return the Try page's key for this sign-in, minting one only if needed.

    The key expires with the sign-in session and is revoked when it signs
    out. The secret is shown once, so the browser keeps it and passes its id
    as ``reuse_id``; while that key is live for this session it is returned
    without a secret (``reused``). Each new mint revokes this session's
    older Try keys except the newest one, so a second open tab keeps working.
    """
    ts = int(time.time()) if now is None else int(now)
    expires_at = int(session.expires_at)
    if not session.family_id or expires_at <= ts:
        raise UserPortalError("login_required", "Sign in again to use Try.", status_code=401)
    user = _user_from_session(session, path=path)
    apps = [
        app for app in admin_store.list_apps_for_user(str(user["id"]), path=path)
        if str(app.get("name") or "").lower() == admin_store.TRY_APP_NAME.lower()
        and not admin_store.is_session_app(str(app["id"]), path=path)
    ]
    if apps:
        app = _app_belongs_to_user(str(apps[0]["id"]), str(user["id"]), path=path)
    else:
        created = create_user_app(session, name=admin_store.TRY_APP_NAME, path=path)["app"]
        app = admin_store.get_app(str(created["id"]), path=path) or created
    app_name = str(app.get("name") or admin_store.TRY_APP_NAME)
    session_keys = [
        record for record in admin_store.list_active_credentials(
            subject_kind="app", app_id=str(app["id"]), now=ts, path=path,
        )
        if record.get("session_id") == session.family_id
    ]
    reused = next((record for record in session_keys if record["id"] == reuse_id), None)
    if reused is not None:
        out = _sanitize_credential(reused)
        out.update(active=True, reused=True, app_name=app_name)
        return out
    admin_store.revoke_older_session_credentials(
        str(app["id"]), session.family_id, keep=1, now=ts, path=path,
    )
    minted = mint_user_app_key(
        session, str(app["id"]), label=admin_store.TRY_KEY_LABEL, expires_at=expires_at,
        session_id=session.family_id, path=path,
    )
    minted.update(reused=False, app_name=app_name)
    return minted


def create_user_app(
    session: AdminSession,
    *,
    name: str,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create a new user-owned App: "Same as my access plan" (design §2.9)."""
    user = _user_from_session(session, path=path)
    cleaned_name = " ".join(str(name or "").split())
    if not cleaned_name:
        raise UserPortalError(
            "app_name_required",
            "Name the App before creating it.",
            status_code=400,
        )
    if len(cleaned_name) > 80:
        raise UserPortalError(
            "app_name_too_long",
            "App names must be 80 characters or fewer.",
            status_code=400,
        )
    app = admin_store.create_app(
        cleaned_name,
        kind="personal",
        owner_user_id=str(user["id"]),
        access_plan=admin_store.PERSON_ASSIGNMENT,
        path=path,
    )
    admin_store.record_audit(
        "app.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=str(app["id"]),
        metadata={"email": user.get("email"), "name": cleaned_name},
        path=path,
    )
    return {"app": _app_payload(app, path=path)}


def delete_user_app(
    session: AdminSession,
    app_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Remove a user-owned App from self-service use and revoke its active keys."""
    user = _user_from_session(session, path=path)
    app = _app_belongs_to_user(app_id, str(user["id"]), path=path)
    revoked = 0
    for credential in admin_store.list_active_credentials(
        subject_kind="app",
        app_id=str(app["id"]),
        path=path,
    ):
        if admin_store.revoke_credential(str(credential["id"]), path=path):
            revoked += 1
    disabled = admin_store.disable_app(str(app["id"]), path=path) or app
    admin_store.record_audit(
        "app.self_delete",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=str(app["id"]),
        metadata={"email": user.get("email"), "revoked_credentials": revoked},
        path=path,
    )
    return {
        "app": {
            "id": disabled.get("id"),
            "name": disabled.get("name"),
            "disabled_at": disabled.get("disabled_at"),
            "revoked_credentials": revoked,
        }
    }


def _name_error(exc: admin_store.NameRejected) -> UserPortalError:
    return UserPortalError(exc.code, str(exc), status_code=exc.status_code)


def _rename_app(
    user: Mapping[str, Any], app: Mapping[str, Any], name: str, *,
    browser_session: session_guard.BrowserSession | None, via: str | None, path: Path,
) -> dict[str, Any]:
    try:
        cleaned = admin_store.clean_name(name, code="app_name", noun="app")
        renamed = admin_store.update_app(
            str(app["id"]), name=cleaned, owner_user_id=str(user["id"]), session=browser_session,
            audit={"action": "app.self_rename", "actor_kind": "user", "actor_id": str(user["id"]),
                   "metadata": {"via": via} if via else {}},
            path=path)
    except admin_store.NameRejected as exc:
        raise _name_error(exc) from None
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if renamed is None:
        raise UserPortalError("app_not_found", "App not found for this account", status_code=404)
    return {"app": _app_payload(renamed, path=path)}


def _rename_key(
    user: Mapping[str, Any], before: Mapping[str, Any], display_name: str, *,
    browser_session: session_guard.BrowserSession | None, via: str | None, path: Path,
) -> dict[str, Any]:
    try:
        label = admin_store.clean_name(display_name, code="key_label", noun="key")
        renamed = admin_store.rename_app_credential(
            str(before["id"]), label, owner_user_id=str(user["id"]), session=browser_session,
            audit={"action": "credential.self_rename", "actor_kind": "user", "actor_id": str(user["id"]),
                   "metadata": {"via": via} if via else {}},
            path=path)
    except admin_store.NameRejected as exc:
        raise _name_error(exc) from None
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if renamed is None:
        raise _credential_not_found()
    _, after = renamed
    out = _sanitize_credential(after)
    out["active"] = True
    return out


def rename_user_app(
    session: AdminSession,
    app_id: str,
    name: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Give an App owned by ``session.subject`` a new name.

    The name is not a credential, so no step-up; the write checks in its own transaction that the sign-in is
    still live. Receipts already written keep the name they were made with.
    """
    user = _user_from_session(session, path=path)
    app = _app_belongs_to_user(app_id, str(user["id"]), path=path)
    return _rename_app(user, app, name, browser_session=session_guard.BrowserSession.of(session, str(user["id"])),
                       via=None, path=path)


def rename_user_app_key(
    session: AdminSession,
    credential_id: str,
    display_name: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Give an active key of an App owned by ``session.subject`` a new label.

    Only the label changes: no new key and no new destination, so no step-up.
    """
    user = _user_from_session(session, path=path)
    before = get_user_app_credential(session, credential_id, path=path)
    return _rename_key(user, before, display_name,
                       browser_session=session_guard.BrowserSession.of(session, str(user["id"])), via=None, path=path)


def _person(person_id: str, *, path: Path) -> dict[str, Any]:
    user = admin_store.get_user(person_id, path=path)
    if user is None:
        raise UserPortalError(
            "session_user_not_found", "Signed-in user was not found in the admin store", status_code=401)
    return user


def _find_app(user_id: str, ref: str, *, path: Path) -> dict[str, Any]:
    """The person's own App an MCP client names: by its id, or by its name when exactly one has it."""
    ref = str(ref or "").strip()
    apps = admin_store.list_apps_for_user(user_id, path=path)
    sessions = admin_store.session_app_ids([str(app["id"]) for app in apps], path=path)
    owned = [app for app in apps if not app.get("disabled_at") and str(app["id"]) not in sessions]
    matches = [app for app in owned if app["id"] == ref] or [
        app for app in owned if ref and admin_store.name_key(app["name"]) == admin_store.name_key(ref)]
    if not matches:
        raise UserPortalError("app_not_found", f"No app of yours is named \u201c{ref}\u201d.", status_code=404)
    if len(matches) > 1:
        raise UserPortalError(
            "app_ambiguous", f"More than one app is named \u201c{ref}\u201d. Use an app id instead: "
            + ", ".join(str(app["id"]) for app in matches) + ".", status_code=409)
    return matches[0]


def _find_key(app: Mapping[str, Any], ref: str, *, path: Path) -> dict[str, Any]:
    """An active key of ``app`` an MCP client names: by id, or by its name when exactly one has it."""
    ref = str(ref or "").strip()
    keys = admin_store.list_active_credentials(subject_kind="app", app_id=str(app["id"]), path=path)
    matches = [key for key in keys if key["id"] == ref] or [
        key for key in keys if ref and admin_store.name_key(key.get("display_name")) == admin_store.name_key(ref)]
    if not matches:
        raise UserPortalError(
            "credential_not_found", f"No key of \u201c{app['name']}\u201d is named \u201c{ref}\u201d.", status_code=404)
    if len(matches) > 1:
        raise UserPortalError(
            "key_ambiguous", f"More than one key of \u201c{app['name']}\u201d is named \u201c{ref}\u201d. "
            "Use a key id instead: " + ", ".join(str(key["id"]) for key in matches) + ".", status_code=409)
    return matches[0]


def rename_person_app(
    person_id: str, app_ref: str, name: str, *, via: str, path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """A signed-in client renames one of ``person_id``'s own Apps. No browser sign-in asks, so ``via`` names the client path.

    The person's own rules apply, as for ``rename_user_app``: only their own Apps, never someone else's.
    """
    user = _person(person_id, path=path)
    app = _find_app(str(user["id"]), app_ref, path=path)
    return _rename_app(user, app, name, browser_session=None, via=via, path=path)


def rename_person_key(
    person_id: str, app_ref: str, key_ref: str, name: str, *, via: str, path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """A signed-in client renames an active key of one of ``person_id``'s own Apps."""
    user = _person(person_id, path=path)
    app = _find_app(str(user["id"]), app_ref, path=path)
    return _rename_key(user, _find_key(app, key_ref, path=path), name, browser_session=None, via=via, path=path)


def request_user_access(
    session: AdminSession,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Create or return the signed-in user's self-service Personal App."""
    user = _user_from_session(session, path=path)
    label = user.get("email") or user.get("display_name") or user["id"]
    app = admin_store.ensure_personal_app_for_user(
        str(user["id"]),
        name=f"{label} personal access",
        path=path,
    )
    admin_store.record_audit(
        "app.self_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="app",
        target_id=app["id"],
        metadata={"email": user.get("email"), "access_plan": app.get("access_plan")},
        path=path,
    )
    return {"app": _app_payload(app, path=path)}


def get_user_app_credential(
    session: AdminSession,
    credential_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return an active App credential owned by ``session.subject``.

    The ownership lookup stays server-side.  Its result is deliberately folded
    into the same public 404 as every other inaccessible credential state.
    """
    user = _user_from_session(session, path=path)
    credential = admin_store.get_credential(credential_id, path=path)
    if (
        credential is None
        or credential.get("subject_kind") != "app"
        or not _credential_active(credential)
    ):
        raise _credential_not_found()
    app_id = str(credential.get("app_id") or credential.get("subject_id") or "")
    app = admin_store.get_app(app_id, path=path)
    if app is None or app.get("owner_user_id") != str(user["id"]):
        raise _credential_not_found()
    if app.get("disabled_at"):
        raise UserPortalError(
            "app_disabled",
            "App is disabled",
            status_code=403,
        )
    return credential


def revoke_user_app_key(
    session: AdminSession,
    credential_id: str,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Delete an API key for an App owned by ``session.subject``."""
    user = _user_from_session(session, path=path)
    before = get_user_app_credential(session, credential_id, path=path)
    app_id = str(before.get("app_id") or before.get("subject_id") or "")
    admin_store.revoke_credential(credential_id, path=path)
    after = admin_store.get_credential(credential_id, path=path) or before
    admin_store.record_audit(
        "credential.self_revoke",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="credential",
        target_id=credential_id,
        metadata={"app_id": app_id, "prefix": before.get("prefix")},
        path=path,
    )
    out = _sanitize_credential(after)
    out["active"] = False
    return out


def set_user_app_key_privacy_permission(
    session: AdminSession,
    credential_id: str,
    enabled: bool,
    *,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Set request-history access on one live key the signed-in person owns."""
    user = _user_from_session(session, path=path)
    if not isinstance(enabled, bool):
        raise UserPortalError("invalid_privacy_permission", "enabled must be true or false", status_code=400)
    try:
        updated = admin_store.set_app_credential_privacy_permission(
            credential_id, person_id=str(user["id"]), enabled=enabled,
            session=session_guard.BrowserSession.of(session, str(user["id"])), path=path,
        )
    except session_guard.SessionEnded as exc:
        raise UserPortalError(exc.code, exc.message, status_code=exc.status_code) from None
    if updated is None:
        raise _credential_not_found()
    out = _sanitize_credential(updated)
    out["active"] = True
    return out
