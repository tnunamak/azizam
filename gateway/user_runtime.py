"""Normal-user account view over the durable admin store.

The user portal is intentionally narrow. It lets a signed-in OIDC user create
user-owned Apps, see non-secret API-key records, create keys for Apps they own,
inspect owned-key access, and delete their own keys or Apps. A person's own
App is always "Same as my access plan"; choosing plans and changing App
ownership remain admin-plane actions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway import admin_store, avatars, session_guard
from gateway import receipt_store
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
    "request_user_access",
    "revoke_user_app_key",
]


class UserPortalError(ValueError):
    """Raised when a user-plane account action is not allowed."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _sanitize_credential(record: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {"hash", "api_key", "key", "secret", "session_id"}
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
) -> list[dict[str, Any]]:
    """``active`` holds every listed app's credentials, read in one query."""
    credentials = []
    for record in (admin_store.list_active_credentials(subject_kind="app", app_id=app_id, path=path)
                   if active is None else active.get(app_id, ())):
        item = _sanitize_credential(record)
        item["active"] = True
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
    if app.get("disabled_at"):
        raise UserPortalError(
            "app_disabled",
            "App is disabled",
            status_code=403,
        )
    return app


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


def _app_payload(
    app: Mapping[str, Any],
    *,
    path: Path,
    credentials_by_app: Mapping[str, list[dict[str, Any]]] | None = None,
    plan_listing: tuple[str | None, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    credentials = _credentials_for_app(str(app["id"]), path=path, active=credentials_by_app)
    plan = _plan_summary(app, path=path, listing=plan_listing)
    return {
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
    apps: list[dict[str, Any]] = [
        _app_payload(app, path=path, credentials_by_app=credentials_by_app, plan_listing=plan_listing)
        for app in listed
    ]

    person_id = str(user["id"])
    return {
        "authenticated": True,
        "user": {
            "id": user.get("id"),
            # The stable id the generated avatar is drawn from (a rename keeps the colour), and the
            # choice. The picture URL is a gateway path, never the identity provider's address.
            "person_id": person_id,
            "avatar": avatars.public_view(person_id, avatars.get(person_id, path=path), viewer_id=person_id),
            "email": user.get("email") or session.email,
            "name": user.get("display_name") or user.get("email") or session.email,
            "role": _live_role_for_user(str(user["id"]), path=path),
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
) -> dict[str, Any]:
    """Return recent API-call receipts for the signed-in Home page.

    Members are scoped server-side to their owned Apps. Owners see the
    instance-level feed by default, including service apps and no-key local
    network receipts.
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
    owned = _owned_app_ids(str(user["id"]), path=path)
    owner_roles = admin_store.effective_admin_roles("user", str(user["id"]), path=path)
    owner = bool(owner_roles & {"owner", "superadmin"})
    receipts = receipt_store.recent(
        limit=safe_limit,
        audience="client",
        app_ids=None if owner else owned,
        path=receipts_path,
    )
    return {
        "authenticated": True,
        # No fabricated rows: an empty list means the user has made no
        # attributable calls yet (or none in the retention window).
        "receipts": receipts,
        "scoped_app_count": len(owned),
        "scope": "instance" if owner else "me",
    }


def get_user_receipt(
    session: AdminSession | None,
    request_id: str,
    *,
    path: Path = admin_store.DB_PATH,
    receipts_path: Path = receipt_store.DB_PATH,
) -> dict[str, Any]:
    """Return one client-safe receipt visible to the signed-in user.

    It carries the person, App and time the Activity list shows
    (``receipt_context``).

    Owners can open the same instance-scoped receipts shown on Home and each
    access is audited. Members stay limited to their own Apps; receipts for
    other Apps or no App are reported as not found so existence is not disclosed.
    """
    if session is None:
        raise UserPortalError(
            "not_authenticated", "Sign in to view this receipt", status_code=401
        )
    user = _user_from_session(session, path=path)

    owner_check = receipt_store.get(
        request_id, audience="internal", path=receipts_path
    )
    not_found = UserPortalError(
        "receipt_not_found", "Receipt not found for this account", status_code=404
    )
    if owner_check is None:
        raise not_found
    receipt_app_id = owner_check.get("app_id")
    owned_app_ids = set(_owned_app_ids(str(user["id"]), path=path))
    owner_roles = admin_store.effective_admin_roles("user", str(user["id"]), path=path)
    owner = bool(owner_roles & {"owner", "superadmin"})
    if not owner and (not receipt_app_id or receipt_app_id not in owned_app_ids):
        raise not_found

    found = receipt_store.get_with_created_at(
        request_id, audience="client", path=receipts_path
    )
    if found is None:
        raise not_found
    detail, created_at = found
    app = admin_store.get_app(receipt_app_id, path=path) if receipt_app_id else None
    app = app or {}
    person = admin_store.get_user(str(app["owner_user_id"]), path=path) if app.get("owner_user_id") else None
    context = receipt_context(
        detail, created_at, app=app,
        person_labels={person["id"]: person_label(person)} if person else {},
    )
    detail = {**detail, **{key: context[key] for key in ("created_at", "person_label", "app_label")}}
    if owner:
        admin_store.record_audit(
            "receipt.detail.instance",
            actor_kind="user",
            actor_id=str(user["id"]),
            actor_label=str(user.get("email") or user.get("display_name") or user["id"]),
            target_kind="receipt",
            target_id=request_id,
            metadata={"scope": "instance", "app_id": receipt_app_id},
            path=path,
        )
    return detail


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


def ensure_try_session_key(
    session: AdminSession,
    *,
    reuse_id: str | None = None,
    before_mint: Callable[[], None] | None = None,
    now: int | None = None,
    path: Path = admin_store.DB_PATH,
) -> dict[str, Any]:
    """Return the Try page's key for this sign-in, minting one only if needed.

    ``before_mint`` runs just before a new key is made, never for a reused one; it can refuse by raising.

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
    if before_mint is not None:
        before_mint()
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
