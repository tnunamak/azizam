"""JSON account and People endpoints over the gateway's browser sessions."""
from __future__ import annotations

import hmac
from typing import Callable, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from starlette.responses import JSONResponse

from gateway import accounts, admin_store
from gateway.settings_api import instance_settings
from gateway.admin_api import _require_admin
from gateway.admin_oidc import encode_session_cookie, session_from_claims, csrf_token_for_session, safe_browser_return_to


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


class Login(_Body):
    email: str
    password: str
    return_to: str | None = None


class Accept(_Body):
    token: str
    email: str
    name: str
    password: str | None = None


class Reset(_Body):
    token: str
    password: str


class Invite(_Body):
    profile_id: str
    email: str | None = None
    expires_in: StrictInt = 604800


class Expiry(_Body):
    expires_in: StrictInt = 3600


class Person(_Body):
    profile_id: str | None = None
    disabled: StrictBool | None = None


class SignInMode(_Body):
    mode: Literal["built-in", "oidc", "both"]


def signin_options(path, oidc: dict) -> dict:
    setting = admin_store.get_operator_setting("sign_in", path=path)
    mode = setting["value"].get("mode", "both") if setting else "both"
    return {"mode": mode, "built_in": mode in {"built-in", "both"},
            "oidc": mode in {"oidc", "both"} and oidc["enabled"],
            "oidc_label": oidc.get("label") or "your identity provider",
            "oidc_url": "/admin/login?return_to=/ui/invite/", "instance_name": "AI Gateway",
            "owner_name": "The gateway owner"}


def create_account_router(*, path_provider: Callable, auth, session_reader, cookie_provider,
                          signing_key_provider, oidc_provider: Callable,
                          instance_provider: Callable | None = None) -> APIRouter:
    router = APIRouter(route_class=_AccountRoute, tags=["accounts"])

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

    def issue(user, payload):
        path = path_provider()
        roles = admin_store.list_admin_roles(subject_kind="user", subject_id=user["id"], path=path)
        names = {row["role"] for row in roles}
        role = "owner" if "owner" in names else "admin" if "admin" in names else "none"
        cookie, key = cookie_provider(), signing_key_provider()
        if not key:
            raise accounts.AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        session = session_from_claims({"sub": user["id"], "email": user["email"]}, role=role, cookie=cookie)
        accounts.register_session(user["id"], session.session_id, int(session.expires_at), path=path)
        response = JSONResponse(payload)
        response.set_cookie(cookie.name, encode_session_cookie(session, signing_key=key),
                            max_age=cookie.max_age_seconds, secure=cookie.secure,
                            httponly=cookie.http_only, samesite=cookie.same_site,
                            path=cookie.path, domain=cookie.domain)
        return response

    @router.get("/auth/api/options")
    def get_options(request: Request):
        result = options()
        result["oidc_url"] = "/admin/login?" + urlencode({"return_to": safe_browser_return_to(request.query_params.get("return_to"))})
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
        if not options()["built_in"]:
            raise accounts.AccountError("builtin_disabled", "Use the identity provider to sign in.", 403)
        user = accounts.login(body.email, body.password, request.client.host if request.client else "unknown", path=path_provider())
        return issue(user, {"next": safe_browser_return_to(body.return_to)})

    @router.get("/auth/api/invite")
    def inspect(token: str = ""):
        invite = accounts.inspect_invite(token, path=path_provider())
        plan = admin_store.get_profile(invite["profile_id"], path=path_provider())
        return {"plan": plan, "email": invite["email"], "expires_at": invite["expires_at"],
                "owner_name": "The gateway owner", "instance_name": options()["instance_name"]}

    @router.post("/auth/api/invite/accept")
    def accept(request: Request, body: Accept):
        same_origin(request)
        opts, path = options(), path_provider()
        accounts.throttle(request.client.host if request.client else "unknown", body.email, path=path)
        oidc_user_id = None
        if body.password is None:
            session = session_reader(request)
            if not opts["oidc"] or not session:
                raise accounts.AccountError("login_required", "Sign in with your identity provider first.", 401)
            expected = csrf_token_for_session(session, signing_key=signing_key_provider())
            if not hmac.compare_digest(expected, request.headers.get("x-ai-gateway-csrf", "")):
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
        return issue(user, {"next": "/ui/", "app": app, "credential": credential})

    @router.post("/auth/api/password-reset")
    def reset(request: Request, body: Reset):
        same_origin(request)
        accounts.throttle(request.client.host if request.client else "unknown", "reset", path=path_provider())
        accounts.reset_password(body.token, body.password, path=path_provider())
        response = JSONResponse({"next": "/ui/sign-in/"})
        cookie = cookie_provider()
        response.delete_cookie(cookie.name, path=cookie.path, domain=cookie.domain)
        return response

    @router.get("/admin/api/people")
    def people(request: Request):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return {"people": accounts.people(path=path_provider()), "profiles": admin_store.list_profiles(path=path_provider())}

    @router.patch("/admin/api/people/{user_id}")
    def update_person(request: Request, user_id: str, body: Person):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if body.profile_id is None and body.disabled is None:
            raise accounts.AccountError("empty_update", "Choose an access plan or account state.")
        accounts.change_person(user_id, ctx.subject, profile_id=body.profile_id, disabled=body.disabled, path=path_provider())
        return {"ok": True}

    @router.delete("/admin/api/people/{user_id}")
    def remove_person(request: Request, user_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        accounts.change_person(user_id, ctx.subject, remove=True, path=path_provider())
        return {"ok": True}

    @router.post("/admin/api/people/{user_id}/reset")
    def reset_link(request: Request, user_id: str, body: Expiry):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        link, token = accounts.create_link("reset", ctx.subject, user_id=user_id, expires_in=body.expires_in, path=path_provider())
        return {"url": f"/ui/sign-in/#reset={token}", "expires_at": link["expires_at"]}

    @router.get("/admin/api/invites")
    def invites(request: Request):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return {"invites": accounts.list_invites(path=path_provider()), "profiles": admin_store.list_profiles(path=path_provider())}

    @router.post("/admin/api/invites")
    def create_invite(request: Request, body: Invite):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        key = signing_key_provider()
        if not key:
            raise accounts.AccountError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        invite, token = accounts.create_link("invite", ctx.subject, token_factory=lambda link_id: accounts.invite_token(link_id, key), profile_id=body.profile_id,
                                             email=body.email, expires_in=body.expires_in, path=path_provider())
        return {"invite": invite, "url": f"/ui/invite/#token={token}"}

    @router.get("/admin/api/invites/{link_id}/link")
    def copy_invite(request: Request, link_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        token = accounts.copy_invite(link_id, ctx.subject, signing_key_provider(), path=path_provider())
        return {"url": f"/ui/invite/#token={token}"}

    @router.delete("/admin/api/invites/{link_id}")
    def revoke_invite(request: Request, link_id: str):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        accounts.revoke_invite(link_id, ctx.subject, path=path_provider())
        return {"ok": True}

    @router.get("/admin/api/operator/sign-in")
    def get_mode(request: Request):
        ctx = owner(request)
        return ctx if isinstance(ctx, JSONResponse) else options()

    @router.put("/admin/api/operator/sign-in")
    def set_mode(request: Request, body: SignInMode):
        ctx = owner(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if body.mode in {"oidc", "both"} and not oidc_provider()["enabled"]:
            raise accounts.AccountError("oidc_unavailable", "Configure an identity provider before enabling this sign-in option.", 409)
        admin_store.put_operator_setting_with_audit("sign_in", {"mode": body.mode}, updated_by=ctx.subject, audit_action="operator.sign_in.update",
                                                   audit_actor_kind=ctx.actor_kind, audit_actor_id=ctx.subject,
                                                   audit_actor_label=ctx.actor_label, audit_target_kind="operator_setting",
                                                   audit_target_id="sign_in", path=path_provider())
        return options()

    return router
