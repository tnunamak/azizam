"""JSON account and People endpoints over the gateway's browser sessions."""
from __future__ import annotations

import hmac
from typing import Callable
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from starlette.responses import JSONResponse

from gateway import accounts, admin_store, browser_sessions
from gateway.settings_api import instance_settings
from gateway.admin_api import _require_admin
from gateway.admin_oidc import SessionError, csrf_token_for_session, safe_browser_return_to


class _AccountRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                response = await handler(request)
            except accounts.AccountError as exc:
                response = JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
                if exc.status == 429:
                    response.headers["retry-after"] = "900"
            response.headers["cache-control"] = "no-store"
            response.headers["referrer-policy"] = "no-referrer"
            return response
        return guarded


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=1024)


class _PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=4096)


class Login(_Body):
    email: str
    password: str
    return_to: str | None = None


class Accept(_PasswordBody):
    token: str
    email: str
    name: str
    password: str | None = None



class Reauth(_Body):
    password: str


class InspectInvite(_Body):
    token: str


class Reset(_PasswordBody):
    token: str
    password: str



class Invite(_Body):
    access_plan: str
    email: str | None = None
    expires_in: StrictInt = 604800


class Expiry(_Body):
    expires_in: StrictInt = 3600


class Person(_Body):
    # A plan id, or null for "No access plan". Omit to leave it unchanged.
    access_plan: str | None = None
    disabled: StrictBool | None = None


def _plan_choice(plan_id, path) -> dict | None:
    """A plan as pickers show it: name and one-line summary (design §3.4)."""
    from gateway.access_plans import PlanError, plan_from_document, plan_summary

    record = admin_store.get_access_plan(str(plan_id), path=path) if plan_id else None
    if record is None:
        return None
    try:
        summary = plan_summary(plan_from_document(record["plan"], plan_id=str(record["id"])))
    except PlanError:
        summary = None
    return {"id": record["id"], "name": record["name"], "summary": summary}


def _plan_choices(path) -> list[dict]:
    return [choice for choice in (_plan_choice(row["id"], path) for row in admin_store.list_access_plans(path=path))
            if choice is not None]


def signin_options(path, oidc: dict) -> dict:
    setting = admin_store.get_operator_setting("sign_in", path=path)
    mode = setting["value"].get("mode", "both") if setting else "both"
    result = {"mode": mode, "built_in": mode in {"built-in", "both"},
            "oidc": mode in {"oidc", "both"} and oidc["enabled"],
            "oidc_label": oidc.get("label") or "single sign-on",
            # The owner's own password works whatever the switch says (see the login routes).
            "owner_password": True,
            "oidc_url": "/login?return_to=%2Finvite%2F", "instance_name": "Azizam",
            "owner_name": "The gateway owner"}
    return result


def create_account_router(*, path_provider: Callable, auth, session_reader, cookie_provider,
                          signing_key_provider, oidc_provider: Callable,
                          on_person_prompt_purge: Callable | None = None,
                          on_person_removed: Callable[[str], None] | None = None,
                          instance_provider: Callable | None = None,
                          client_ip_resolver: Callable | None = None) -> APIRouter:
    router = APIRouter(route_class=_AccountRoute, tags=["accounts"])

    def client_ip(request: Request) -> str:
        resolved = client_ip_resolver(request) if client_ip_resolver else (request.client.host if request.client else None)
        if resolved is None:
            raise accounts.AccountError("invalid_client_ip", "Cannot verify the sign-in client address.", 400)
        return str(resolved)

    def owner(request: Request):
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return JSONResponse({"error": {"code": "owner_required", "message": "Only the owner can manage people."}}, status_code=403)
        return ctx

    def options():
        result = signin_options(path_provider(), oidc_provider())
        instance = instance_provider() if instance_provider else instance_settings(path_provider())
        result["instance_name"] = instance["name"]
        return result

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise accounts.AccountError("origin_forbidden", "Use the sign-in page on this gateway.", 403)

    def issue(request, user, payload):
        path = path_provider()
        names = admin_store.effective_admin_roles("user", user["id"], path=path)
        role = "owner" if "owner" in names else "admin" if "admin" in names else "none"
        cookie, key = cookie_provider(), signing_key_provider()
        if not key:
            raise accounts.AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        browser_sessions.end_incoming(request, cookie=cookie, signing_key=key, path=path)
        session = browser_sessions.issue({"sub": user["id"], "email": user["email"]}, role=role, cookie=cookie, path=path)
        response = JSONResponse(payload)
        browser_sessions.set_cookie(response, session, cookie=cookie, signing_key=key)
        return response

    @router.get("/auth/api/options")
    def get_options(request: Request):
        result = options()
        result["oidc_url"] = "/login?" + urlencode({"return_to": safe_browser_return_to(request.query_params.get("return_to"))})
        session = session_reader(request)
        if session:
            user = admin_store.get_user(session.subject, path=path_provider())
            if user and user["issuer"] != "local":
                result["signed_in_oidc"] = {"email": user["email"], "name": user["display_name"],
                                            "csrf_token": csrf_token_for_session(session, signing_key=signing_key_provider())}
        return result

    @router.post("/auth/api/login")
    def login(request: Request, body: Login):
        same_origin(request)
        user = accounts.login(body.email, body.password, client_ip(request), path=path_provider())
        # The owner's own password always works. For everyone else the switch decides,
        # and only a correct password learns that it is off.
        if not options()["built_in"] and "owner" not in admin_store.effective_admin_roles(
                "user", user["id"], path=path_provider()):
            raise accounts.AccountError("builtin_disabled", "Use the identity provider to sign in.", 403)
        # A good password is a good sign-in: it lifts the pause a refused provider sign-in put on their keys.
        accounts.clear_signin_refusal(str(user["id"]), path=path_provider())
        return issue(request, user, {"next": safe_browser_return_to(body.return_to)})

    @router.post("/auth/api/invite")
    def inspect(request: Request, body: InspectInvite):
        same_origin(request)
        invite = accounts.inspect_invite(body.token, path=path_provider())
        return {"plan": _plan_choice(invite["plan_id"], path_provider()), "email": invite["email"], "expires_at": invite["expires_at"],
                "owner_name": "The gateway owner", "instance_name": options()["instance_name"]}

    @router.post("/auth/api/invite/accept")
    def accept(request: Request, body: Accept):
        same_origin(request)
        opts, path = options(), path_provider()
        accounts.throttle(client_ip(request), body.email, path=path)
        oidc_user_id = None
        if body.password is None:
            session = session_reader(request)
            if not opts["oidc"] or not session:
                raise accounts.AccountError("login_required", "Sign in with your identity provider first.", 401)
            expected = csrf_token_for_session(session, signing_key=signing_key_provider())
            if not hmac.compare_digest(expected, request.headers.get("x-azizam-csrf", "")):
                raise accounts.AccountError("csrf_required", "Refresh this page before accepting your invitation.", 403)
            user = admin_store.get_user(session.subject, path=path)
            if not user or user["issuer"] == "local":
                raise accounts.AccountError("oidc_required", "Sign in with your identity provider first.", 403)
            invite = accounts.inspect_invite(body.token, path=path)
            if invite["email"] and not accounts.oidc_email_verified(user["id"], path=path):
                raise accounts.AccountError("verified_email_required", "Verify your email with your identity provider first.", 403)
            oidc_user_id = user["id"]
        elif not opts["built_in"]:
            raise accounts.AccountError("builtin_disabled", "Use the identity provider to accept this invitation.", 403)
        if not signing_key_provider():
            raise accounts.AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        user, app, credential = accounts.accept_invite(body.token, body.email, body.name, body.password,
                                                       path=path, oidc_user_id=oidc_user_id)
        return issue(request, user, {"next": "/", "app": app, "credential": credential})

    @router.post("/auth/api/password-reset")
    def reset(request: Request, body: Reset):
        same_origin(request)
        # Second bucket is per reset link, not one shared bucket: a shared key
        # let anyone's failed attempts block every person's reset.
        link = hmac.new(b"reset-link", body.token.encode(), "sha256").hexdigest()
        accounts.throttle(client_ip(request), f"reset:{link}", path=path_provider())
        accounts.reset_password(body.token, body.password, path=path_provider())
        response = JSONResponse({"next": "/sign-in/"})
        cookie = cookie_provider()
        response.delete_cookie(cookie.name, path=cookie.path, domain=cookie.domain)
        return response

    def signed_in_with_csrf(request: Request):
        """The caller's own session, for an action on that session; a missing or wrong CSRF token is refused."""
        session = session_reader(request)
        if not session:
            raise accounts.AccountError("login_required", "Sign in first.", 401)
        key = signing_key_provider()
        expected = csrf_token_for_session(session, signing_key=key) if key else ""
        if not expected or not hmac.compare_digest(expected, request.headers.get("x-azizam-csrf", "")):
            raise accounts.AccountError("csrf_required", "Refresh this page and try again.", 403)
        return session, key

    @router.post("/auth/api/reauth")
    def reauth(request: Request, body: Reauth):
        """Confirm the signed-in person's password, so risky owner actions are allowed for a while."""
        same_origin(request)
        session, key = signed_in_with_csrf(request)
        path = path_provider()
        accounts.confirm_password(session.subject, body.password, client_ip(request), path=path)
        try:
            refreshed = browser_sessions.confirm(session, path=path)
        except SessionError:
            raise accounts.AccountError("login_required", "Sign in first.", 401) from None
        admin_store.record_audit("account.reauth", actor_kind="user", actor_id=session.subject,
                                 actor_label=session.email, target_kind="user", target_id=session.subject, path=path)
        response = JSONResponse({"ok": True, "authenticated_at": int(refreshed.last_authenticated)})
        browser_sessions.set_cookie(response, refreshed, cookie=cookie_provider(), signing_key=key)
        return response

    @router.post("/auth/api/sign-out-everywhere")
    def sign_out_everywhere(request: Request):
        """End every sign-in this person has, on every device, this one included."""
        same_origin(request)
        session, _ = signed_in_with_csrf(request)
        browser_sessions.end_everywhere(session, path=path_provider())
        response = JSONResponse({"next": "/sign-in/"})
        cookie = cookie_provider()
        response.delete_cookie(cookie.name, path=cookie.path, domain=cookie.domain)
        return response

    @router.get("/admin/api/people")
    def people(request: Request):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return {"people": accounts.people(path=path_provider()), "access_plans": _plan_choices(path_provider())}

    @router.patch("/admin/api/people/{user_id}")
    def update_person(request: Request, user_id: str, body: Person):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        fields = body.model_fields_set
        if "access_plan" not in fields and body.disabled is None:
            raise accounts.AccountError("empty_update", "Choose an access plan or account state.")
        plan = {"plan_id": body.access_plan} if "access_plan" in fields else {}
        accounts.change_person(user_id, ctx.audit_actor_id, disabled=body.disabled, path=path_provider(),
                               actor_kind=ctx.actor_kind, actor_label=ctx.actor_label, **plan)
        return {"ok": True}

    @router.delete("/admin/api/people/{user_id}")
    def remove_person(request: Request, user_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        accounts.change_person(user_id, ctx.audit_actor_id, remove=True, path=path_provider(), actor_kind=ctx.actor_kind, actor_label=ctx.actor_label)
        if on_person_removed:
            on_person_removed(user_id)
        result = on_person_prompt_purge(user_id) if on_person_prompt_purge else None
        return {"ok": True, **(result or {})}

    @router.post("/admin/api/people/{user_id}/reset")
    def reset_link(request: Request, user_id: str, body: Expiry):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        link, token = accounts.create_link("reset", ctx.audit_actor_id, user_id=user_id, expires_in=body.expires_in, path=path_provider(), actor_kind=ctx.actor_kind, actor_label=ctx.actor_label, session=ctx.browser_session)
        return {"url": f"/sign-in/#reset={token}", "expires_at": link["expires_at"]}

    @router.get("/admin/api/invites")
    def invites(request: Request):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return {"invites": accounts.list_invites(path=path_provider()), "access_plans": _plan_choices(path_provider()),
                "invite_access_plan": admin_store.invite_access_plan(path=path_provider())}

    @router.post("/admin/api/invites")
    def create_invite(request: Request, body: Invite):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        key = signing_key_provider()
        if not key:
            raise accounts.AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        invite, token = accounts.create_link("invite", ctx.audit_actor_id, token_factory=lambda link_id: accounts.invite_token(link_id, key), plan_id=body.access_plan,
                                             email=body.email, expires_in=body.expires_in, path=path_provider(), actor_kind=ctx.actor_kind, actor_label=ctx.actor_label, session=ctx.browser_session)
        return {"invite": invite, "url": f"/invite/#token={token}"}

    @router.get("/admin/api/invites/{link_id}/link")
    def copy_invite(request: Request, link_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        token = accounts.copy_invite(link_id, ctx.audit_actor_id, signing_key_provider(), path=path_provider(), actor_kind=ctx.actor_kind, actor_label=ctx.actor_label)
        return {"url": f"/invite/#token={token}"}

    @router.delete("/admin/api/invites/{link_id}")
    def revoke_invite(request: Request, link_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        accounts.revoke_invite(link_id, ctx.audit_actor_id, path=path_provider(), actor_kind=ctx.actor_kind, actor_label=ctx.actor_label)
        return {"ok": True}

    return router
