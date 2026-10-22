"""Owner routes for Settings > Sign-in: one snapshot, one preview, one save, a test sign-in.

Every decision about who gets in is made by ``gateway.signin_rules.evaluate``.
The preview, the test verdict and sign-in enforcement all call it, so the page
shows only what the server computed. An administrator may read the snapshot;
every other route is the owner's (403 ``owner_required``).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from starlette.responses import HTMLResponse, JSONResponse, Response

from gateway import accounts, admin_store, remembered_people, secret_store, signin_admin, signin_rules, signin_trial
from gateway.accounts_api import signin_options
from gateway.admin_api import AdminContext, _require_admin
from gateway.admin_oidc import OIDCConfig, OIDCConfigError, StateRecord
from gateway.oidc_browser_flow import begin_browser_login
from gateway.admin_store import SettingsConflictError
from gateway.signin_admin import SignInError, World, norm_issuer
from gateway.session_guard import SessionEnded

RECOVERY_COMMAND = "./venv/bin/python proxy.py generate-admin-key owner-cli"
DISCOVERY_PATH = "/.well-known/openid-configuration"
CALLBACK_PATH = "/admin/oidc/callback"
DEFAULT_SCOPES = "openid email profile"
DEFAULT_GROUPS_CLAIM = "groups"
DEFAULT_CLOCK_SKEW = 60


class _Reply(Exception):
    """A response a dependency decided on (for example 401), returned as is."""

    def __init__(self, response: Response):
        self.response = response


class _SignInRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                response = await handler(request)
            except _Reply as exc:
                response = exc.response
            except SignInError as exc:
                response = JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
            except signin_rules.RulesError as exc:
                response = JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=400)
            except OIDCConfigError:
                response = JSONResponse(
                    {"error": {"code": "invalid_connection",
                               "message": "The name must be plain text of at most 80 characters."}},
                    status_code=400)
            except accounts.AccountError as exc:
                response = JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
            except SettingsConflictError:
                response = JSONResponse(
                    {"error": {"code": "settings_conflict",
                               "message": "Sign-in settings changed elsewhere. Reload to see the latest."}},
                    status_code=409)
            response.headers["cache-control"] = "no-store"
            return response
        return guarded


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=4096)


class ConnectionBody(_Body):
    name: str = ""
    address: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: str = DEFAULT_SCOPES
    groups_claim: str = DEFAULT_GROUPS_CLAIM
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    clock_skew_seconds: StrictInt = DEFAULT_CLOCK_SKEW


class RuleBody(_Body):
    id: str = ""
    field: str
    values: list[str] = []
    admit: bool = False
    admin: bool = False
    plan: str | None = None


class RulesBody(_Body):
    fallback: str = "closed"
    rules: list[RuleBody] = []


class MethodsBody(_Body):
    password: StrictBool
    provider: StrictBool


class ProposalBody(_Body):
    methods: MethodsBody
    provider: ConnectionBody
    rules: RulesBody
    # "On shared computers". Omitted means keep what is saved, so a page that does not send it cannot change it.
    memory: Literal["remember", "nobody"] | None = None


class PreviewBody(_Body):
    proposal: ProposalBody
    test_id: str | None = None


class SaveBody(_Body):
    proposal: ProposalBody
    test_id: str | None = None
    keep_admin_keys: list[str] = []
    save_for_later: bool = False


class TestBody(_Body):
    provider: ConnectionBody


class LinkBody(_Body):
    test_id: str


class PasswordBody(_Body):
    password: str


class DiscoverBody(_Body):
    address: str


def _split_scopes(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.replace(",", " ").split() if part)


def _valid_address(address: str) -> bool:
    """An https address with a host; plain http only for a machine's own address."""
    try:
        parts = urlsplit(address.strip())
        host = parts.hostname or ""
    except ValueError:
        return False
    if not host or parts.username or parts.password or parts.fragment:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})


def _host(address: str) -> str:
    try:
        return urlsplit(address.strip()).netloc
    except ValueError:
        return ""


def default_fetch_json(url: str) -> Mapping[str, Any]:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("not a discovery document")
    return payload


def create_signin_api(
    *,
    path_provider: Callable[[], Path],
    auth: Any,
    store: Any,
    oidc_config_provider: Callable[[], OIDCConfig],
    states: Callable[[], dict[str, StateRecord]],
    public_base_provider: Callable[[Request], str],
    signing_key_provider: Callable[[], bytes | None],
    end_provider_access: Callable[[admin_store.Transaction, str], None],
    fetch_json: Callable[[str], Mapping[str, Any]] | None = None,
) -> "SignInApi":
    router = APIRouter(route_class=_SignInRoute, tags=["sign-in"])
    get_json = fetch_json or (lambda url: default_fetch_json(url))

    def owner_ctx(request: Request) -> AdminContext:
        """The owner, decided before the request body is looked at."""
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            raise _Reply(ctx)
        if "owner" not in ctx.roles:
            raise SignInError("owner_required", "Only the owner can change sign-in.", 403)
        return ctx

    def callback_url(request: Request, cfg: OIDCConfig) -> str:
        base = public_base_provider(request).rstrip("/")
        if cfg.redirect_uri:
            parts = urlsplit(cfg.redirect_uri)
            path = parts.path or CALLBACK_PATH
            if base:
                return base + path
            return cfg.redirect_uri
        return (base or str(request.base_url).rstrip("/")) + CALLBACK_PATH

    def saved_world(path: Path, cfg: OIDCConfig) -> World:
        options = signin_options(path, {"enabled": cfg.enabled})
        return World(rules=signin_admin.load_rules(path), password=bool(options["built_in"]),
                     provider=bool(options["oidc"]), issuer=norm_issuer(cfg.issuer))

    def proposed_world(path: Path, body: ProposalBody) -> World:
        known = plan_map(path)
        rules = signin_rules.parse_rules(body.rules.model_dump(), plan_exists=lambda plan: plan in known)
        return World(rules=rules, password=body.methods.password, provider=body.methods.provider,
                     issuer=norm_issuer(body.provider.address))

    def plan_map(path: Path) -> dict[str, str]:
        return signin_admin.plan_names(path)

    def same_destination(saved: OIDCConfig, body: ConnectionBody) -> bool:
        """The saved secret may go only where it was already going: the same provider host and endpoints."""
        if not saved.issuer or _host(norm_issuer(body.address)) != _host(norm_issuer(saved.issuer)):
            return False
        pairs = ((body.authorization_endpoint, saved.authorization_endpoint),
                 (body.token_endpoint, saved.token_endpoint), (body.jwks_uri, saved.jwks_uri))
        return all(not typed.strip() or typed.strip() == kept for typed, kept in pairs)

    def _secret_would_travel(saved: OIDCConfig, body: ConnectionBody) -> bool:
        """A save that keeps the saved secret (none typed) while the address moves to another host or endpoints."""
        wants = bool(body.address.strip() or body.client_id.strip())
        return bool(wants and saved.client_secret and not body.client_secret.strip()
                    and not same_destination(saved, body))

    def typed_config(request: Request, saved: OIDCConfig, body: ConnectionBody, *, enabled: bool) -> tuple[OIDCConfig, str]:
        """The connection as typed (overrides only, no discovery), and its secret.

        A blank secret means the saved one, but only for the same destination: a new
        address needs the secret typed again, so a typed address cannot collect it.
        """
        secret = body.client_secret.strip() or (saved.client_secret if same_destination(saved, body) else "")
        key = signature_key()
        scopes = _split_scopes(body.scopes) or tuple(DEFAULT_SCOPES.split())
        return OIDCConfig(
            enabled=enabled, display_name=body.name.strip(), issuer=body.address.strip(), client_id=body.client_id.strip(),
            client_secret=secret, redirect_uri=callback_url(request, saved), scopes=scopes, admin_emails=frozenset(),
            signing_key=key, cookie=saved.cookie,
            authorization_endpoint=body.authorization_endpoint.strip(), token_endpoint=body.token_endpoint.strip(),
            jwks_uri=body.jwks_uri.strip(), clock_skew_seconds=int(body.clock_skew_seconds),
            groups_claim=body.groups_claim.strip() or DEFAULT_GROUPS_CLAIM,
        ), secret

    def signature_key() -> bytes:
        key = signing_key_provider()
        if not key or len(key) < 32:
            raise SignInError("sessions_unavailable", "Browser sessions are unavailable.", 503)
        return key

    def saved_signature(saved: OIDCConfig) -> str:
        return signin_trial.connection_signature(signature_key(), saved, saved.client_secret)

    def instance_owner(path: Path) -> "signin_admin.PersonFacts | None":
        """The person who is the owner. Keys and passwords answer to a person, not to a key's account."""
        return next((p for p in signin_admin.people_facts(path) if p.is_owner), None)

    def tested_before(path: Path, signature: str) -> bool:
        """This exact connection already passed a test (the saved one, or one saved for later)."""
        last = signin_admin.load_status(path).get("last_test")
        return isinstance(last, dict) and last.get("sig") == signature

    def discover(address: str) -> dict[str, Any]:
        address = address.strip()
        if not _valid_address(address):
            return {"ok": False, "host": _host(address), "issuer": "", "authorization_endpoint": "",
                    "token_endpoint": "", "jwks_uri": ""}
        url = address.rstrip("/") + DISCOVERY_PATH
        try:
            doc = get_json(url)
            issuer = str(doc.get("issuer") or "")
            found = {key: str(doc.get(key) or "") for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")}
            if norm_issuer(issuer) != norm_issuer(address) or not all(found.values()):
                raise ValueError("discovery document does not describe this address")
        except (httpx.HTTPError, ValueError, OSError):
            return {"ok": False, "host": _host(address), "issuer": "", "authorization_endpoint": "",
                    "token_endpoint": "", "jwks_uri": ""}
        return {"ok": True, "host": _host(address), "issuer": issuer, **found}

    # -- snapshot -----------------------------------------------------------

    def build_snapshot(request: Request, ctx: AdminContext) -> dict[str, Any]:
        path = path_provider()
        cfg = oidc_config_provider()
        options = signin_options(path, {"enabled": cfg.enabled})
        rules = signin_admin.load_rules(path)
        plans = plan_map(path)
        rules_json = rules.as_dict()
        for rule in rules_json["rules"]:
            if rule["plan"] and rule["plan"] not in plans:
                rule["plan"] = None
        status = signin_admin.load_status(path)
        last = status.get("last_test") if isinstance(status.get("last_test"), dict) else {}
        tested_at = last.get("at") if last.get("sig") == saved_signature(cfg) else None
        is_owner = "owner" in ctx.roles
        owner = next((p for p in signin_admin.people_facts(path) if p.is_owner), None)
        host = _host(cfg.issuer)
        return {
            "revision": signin_admin.revisions(path),
            "viewer": {"role": "owner" if is_owner else "admin", "can_edit": is_owner},
            "methods": {"password": bool(options["built_in"]), "provider": bool(options["oidc"])},
            "memory": remembered_people.memory_mode(path),
            "provider": {
                "name": cfg.display_name,
                "address": cfg.issuer,
                "client_id": cfg.client_id,
                "client_secret_saved": bool(cfg.client_secret),
                "scopes": " ".join(cfg.scopes),
                "groups_claim": cfg.groups_claim,
                "authorization_endpoint": cfg.authorization_endpoint,
                "token_endpoint": cfg.token_endpoint,
                "jwks_uri": cfg.jwks_uri,
                "clock_skew_seconds": cfg.clock_skew_seconds,
                "host": host,
                "effective_name": cfg.display_name or host,
                "redirect_uri": callback_url(request, cfg),
                "tested_at": tested_at,
                "groups_missing": bool(tested_at and last.get("groups_missing")),
            },
            "rules": rules_json,
            "rule_matches": signin_admin.rule_matches(path, rules, with_names=is_owner),
            "seen": signin_admin.seen_values(path),
            "plans": [{"id": plan_id, "name": name} for plan_id, name in sorted(plans.items(), key=lambda kv: kv[1])],
            "owner": {
                "name": (owner.name if owner else ""),
                "email": (owner.email if owner else ""),
                "has_password": bool(owner and owner.has_password),
            },
            "recovery_command": RECOVERY_COMMAND,
            "notice": status.get("notice"),
        }

    @router.get("/admin/api/signin")
    def get_snapshot(request: Request):
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return build_snapshot(request, ctx)

    # -- preview ------------------------------------------------------------

    def blockers_for(request: Request, path: Path, saved_cfg: OIDCConfig, saved: World, after: World,
                     body: ProposalBody, rules: signin_rules.Rules, test_id: str | None,
                     user_id: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        provider = body.provider
        if not after.password and not after.provider:
            found.append({"code": "both_methods_off",
                          "message": "Keep at least one way to sign in turned on."})
        if not after.provider and saved.provider:
            owner = instance_owner(path)
            if owner is not None and not owner.has_password:
                found.append({"code": "owner_needs_password",
                              "message": "Set a password for yourself first, so you can still get in."})
        if (after.provider or provider.address.strip()) and not _valid_address(provider.address):
            found.append({"code": "address_invalid", "message": "Fix the provider address first."})
        if rules.incomplete():
            found.append({"code": "incomplete_rule", "message": "Finish or remove the new rule first."})
        if _secret_would_travel(saved_cfg, provider):
            found.append({"code": "secret_needed", "message": "Enter the client secret again for the new address."})
        if after.provider and _valid_address(provider.address) and provider.client_id.strip():
            typed, secret = typed_config(request, saved_cfg, provider, enabled=False)
            sig = signin_trial.connection_signature(signature_key(), typed, secret)
            unchanged = (saved.provider and sig == saved_signature(saved_cfg)) or tested_before(path, sig)
            if not unchanged and signin_trial.passed_for(test_id, user_id, sig) is None:
                found.append({"code": "test_required", "message": "Test sign-in first."})
        elif after.provider:
            found.append({"code": "test_required", "message": "Test sign-in first."})
        return found

    def preview_json(request: Request, ctx: AdminContext, body: PreviewBody) -> dict[str, Any]:
        path = path_provider()
        cfg = oidc_config_provider()
        saved = saved_world(path, cfg)
        plans = plan_map(path)
        after = proposed_world(path, body.proposal)
        result = signin_admin.preview(path, saved, after, plans)
        settings: list[str] = []
        if saved.password != after.password:
            settings.append("password_on" if after.password else "password_off")
        if saved.provider != after.provider:
            settings.append("provider_on" if after.provider else "provider_off")
        if saved.rules.fallback != after.rules.fallback:
            settings.append("fallback_closed" if after.rules.fallback == "closed" else "fallback_open")
        proposed = body.proposal.provider
        if saved.provider and proposed.name.strip() != cfg.display_name.strip():
            settings.append("name_changed")
        if saved.provider and after.provider and not result["issuer_change"]:
            typed, secret = typed_config(request, cfg, proposed, enabled=False)
            if signin_trial.connection_signature(signature_key(), typed, secret) != saved_signature(cfg):
                settings.append("connection_changed")
        verdict = None
        if body.test_id:
            run = signin_trial.get(body.test_id, ctx.audit_actor_id)
            if run is not None and run.status == "passed":
                verdict = signin_admin.test_verdict(path, run, after, saved, plans)
        return {
            **result,
            "settings": settings,
            "blockers": blockers_for(request, path, cfg, saved, after, body.proposal, after.rules,
                                     body.test_id, ctx.audit_actor_id),
            "test_verdict": verdict,
            "rule_matches": signin_admin.rule_matches(path, after.rules, with_names=True),
        }

    @router.post("/admin/api/signin/preview")
    def preview(request: Request, body: PreviewBody, ctx: AdminContext = Depends(owner_ctx)):
        return preview_json(request, ctx, body)

    # -- discover and test --------------------------------------------------

    @router.post("/admin/api/signin/discover")
    def discover_route(request: Request, body: DiscoverBody, ctx: AdminContext = Depends(owner_ctx)):
        return discover(body.address)

    @router.post("/admin/api/signin/test")
    def start_test(request: Request, body: TestBody, ctx: AdminContext = Depends(owner_ctx)):
        saved = oidc_config_provider()
        provider = body.provider
        if not _valid_address(provider.address):
            raise SignInError("address_invalid", "Fix the provider address first.")
        if not provider.client_id.strip():
            raise SignInError("client_id_required", "Enter the client ID first.")
        typed, secret = typed_config(request, saved, provider, enabled=False)
        if not secret:
            raise SignInError("client_secret_required", "Enter the client secret first.")
        signature = signin_trial.connection_signature(signature_key(), typed, secret)
        found = discover(provider.address)
        cfg = replace(
            typed,
            enabled=True,
            authorization_endpoint=typed.authorization_endpoint or found["authorization_endpoint"],
            token_endpoint=typed.token_endpoint or found["token_endpoint"],
            jwks_uri=typed.jwks_uri or found["jwks_uri"],
            issuer=found["issuer"] or typed.issuer,
        )
        run = signin_trial.create(ctx.audit_actor_id, cfg, signature)
        if not (cfg.authorization_endpoint and cfg.token_endpoint and cfg.jwks_uri):
            signin_trial.fail(run, "unreachable", "no sign-in endpoints were found")
        return {"test_id": run.id, "start_url": f"/admin/api/signin/test/{run.id}/start"}

    @router.get("/admin/api/signin/test/{test_id}/start")
    def begin_test(request: Request, test_id: str, ctx: AdminContext = Depends(owner_ctx)):
        run = signin_trial.get(test_id, ctx.audit_actor_id)
        if run is None:
            raise SignInError("test_not_found", "That test is over. Start a new one.", 404)
        if run.status != "waiting":
            return HTMLResponse(_CLOSE_PAGE, headers={"cache-control": "no-store"})
        return begin_browser_login(run.cfg, states(), return_to="/settings/", test_id=run.id)

    def test_status(path: Path, ctx: AdminContext, run: signin_trial.TestRun) -> dict[str, Any]:
        if run.status == "waiting":
            return {"status": "waiting"}
        if run.status == "failed":
            return {"status": "failed", "cause": run.cause, "message": run.message, "details": run.details}
        cfg = oidc_config_provider()
        saved = saved_world(path, cfg)
        verdict = signin_admin.test_verdict(path, run, saved, saved, plan_map(path))
        return {
            "status": "passed",
            "tested_at": run.passed_at,
            "identity": {
                "name": run.name, "email": run.claims.email, "email_verified": run.email_verified,
                "groups": list(run.claims.groups), "roles": list(run.claims.roles),
                "claims": list(run.claim_names), "groups_claim": run.cfg.groups_claim,
            },
            "warnings": [] if run.groups_seen else [signin_trial.GROUPS_WARNING],
            "can_link": verdict["kind"] == "owner_unlinked",
            "details": run.details,
        }

    @router.get("/admin/api/signin/test/{test_id}")
    def get_test(request: Request, test_id: str, ctx: AdminContext = Depends(owner_ctx)):
        run = signin_trial.get(test_id, ctx.audit_actor_id)
        if run is None:
            raise SignInError("test_not_found", "That test is over. Start a new one.", 404)
        return test_status(path_provider(), ctx, run)

    @router.delete("/admin/api/signin/test/{test_id}")
    def cancel_test(request: Request, test_id: str, ctx: AdminContext = Depends(owner_ctx)):
        return {"cancelled": signin_trial.cancel(test_id, ctx.audit_actor_id)}

    @router.post("/admin/api/signin/link-owner")
    def link_owner(request: Request, body: LinkBody, ctx: AdminContext = Depends(owner_ctx)):
        path = path_provider()
        run = signin_trial.get(body.test_id, ctx.audit_actor_id)
        if run is None or run.status != "passed":
            raise SignInError("test_not_passed", "Run a passing test sign-in first.", 409)
        saved = saved_world(path, oidc_config_provider())
        verdict = signin_admin.test_verdict(path, run, saved, saved, plan_map(path))
        if verdict["kind"] != "owner_unlinked":
            raise SignInError("not_linkable", "This sign-in cannot be linked to your account.", 409)
        signin_admin.link_identity(path, run.issuer or run.cfg.issuer, run.subject, ctx.audit_actor_id,
                                   linked_by=ctx.audit_actor_id, session=ctx.browser_session)
        return build_snapshot(request, ctx)

    # -- save ---------------------------------------------------------------

    def connection_payload(request: Request, saved: OIDCConfig, provider: ConnectionBody, *, enabled: bool,
                           issuer: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": enabled, "issuer": issuer, "client_id": provider.client_id.strip(),
            "redirect_uri": callback_url(request, saved),
            "scopes": list(_split_scopes(provider.scopes) or DEFAULT_SCOPES.split()),
            "authorization_endpoint": provider.authorization_endpoint.strip(),
            "token_endpoint": provider.token_endpoint.strip(), "jwks_uri": provider.jwks_uri.strip(),
            "clock_skew_seconds": int(provider.clock_skew_seconds),
            "groups_claim": provider.groups_claim.strip() or DEFAULT_GROUPS_CLAIM,
            "display_name": provider.name.strip(),
        }
        if provider.client_secret.strip():
            payload["client_secret"] = provider.client_secret.strip()
        elif saved.client_secret and not (store.get_oidc_config_setting_raw() or {}).get("client_secret"):
            # A secret that came from the bootstrap file moves into the encrypted store on the first save.
            payload["client_secret"] = saved.client_secret
        return payload

    @router.put("/admin/api/signin")
    def save(request: Request, body: SaveBody, ctx: AdminContext = Depends(owner_ctx)):
        revision = (request.headers.get("if-match") or "").strip('"')
        if not revision:
            raise SignInError("settings_revision_required",
                              "Reload the page and send the current revision in If-Match.", 428)
        path = path_provider()
        if revision != signin_admin.revisions(path):
            raise SettingsConflictError()
        cfg = oidc_config_provider()
        saved = saved_world(path, cfg)
        plans = plan_map(path)
        proposal = body.proposal
        provider_on = proposal.methods.provider and not body.save_for_later
        proposal = proposal.model_copy(update={
            "methods": MethodsBody(password=proposal.methods.password, provider=provider_on)})
        after = proposed_world(path, proposal)
        if after.rules.incomplete():
            raise SignInError("incomplete_rule", "Finish or remove the new rule first.", 409)
        if not after.password and not after.provider:
            raise SignInError("last_sign_in_method", "Keep at least one way to sign in turned on.", 409)
        owner = instance_owner(path)
        if not after.provider and saved.provider and owner is not None and not owner.has_password:
            raise SignInError("owner_needs_password", "Set a password for yourself first, so you can still get in.", 409)
        if body.keep_admin_keys and owner is None:
            raise SignInError("owner_required", "Admin keys can be kept only under a person who is the owner.", 409)
        provider = proposal.provider
        wants_connection = bool(provider.address.strip() or provider.client_id.strip() or after.provider)
        if wants_connection and not _valid_address(provider.address):
            raise SignInError("address_invalid", "Fix the provider address first.", 409)
        if _secret_would_travel(cfg, provider):
            # Whatever the provider switch says: a saved secret goes only to the host it was entered for.
            raise SignInError("connection_incomplete", "Enter the client secret again for the new address.", 409)
        typed, secret = typed_config(request, cfg, provider, enabled=False)
        sig = signin_trial.connection_signature(signature_key(), typed, secret)
        run = signin_trial.passed_for(body.test_id, ctx.audit_actor_id, sig)
        last = signin_admin.load_status(path).get("last_test")
        remembered = last.get("issuer") if isinstance(last, dict) and last.get("sig") == sig else None
        # The exact issuer the provider names (it may end in a slash); a sign-in checks it character for character.
        # An address that only differs in a slash or in the case of the scheme and host is the same provider:
        # keep the issuer already stored, or its sign-ins would all be turned away.
        same_provider = bool(cfg.issuer) and norm_issuer(provider.address) == norm_issuer(cfg.issuer)
        issuer = run.cfg.issuer if run is not None else (remembered or (cfg.issuer if same_provider else provider.address.strip()))
        if after.provider:
            if not provider.client_id.strip() or not secret:
                raise SignInError("connection_incomplete", "Enter the client ID and secret first.", 409)
            unchanged = (saved.provider and sig == saved_signature(cfg)) or tested_before(path, sig)
            if not unchanged and run is None:
                raise SignInError("test_required", "Test sign-in first.", 409)
        elif body.save_for_later and not (provider.address.strip() and provider.client_id.strip()):
            raise SignInError("connection_incomplete", "Enter the address and client ID first.", 409)

        rules_doc = after.rules.as_dict()
        audit = dict(audit_actor_kind=ctx.actor_kind, audit_actor_id=ctx.audit_actor_id, audit_actor_label=ctx.actor_label)
        memory_before = remembered_people.memory_mode(path)
        memory_after = proposal.memory or memory_before
        mode = "both" if after.password and after.provider else "oidc" if after.provider else "built-in"
        payload = connection_payload(request, cfg, provider, enabled=after.provider, issuer=issuer)

        def write_rules(conn) -> None:
            admin_store.put_operator_setting_with_audit_in(
                conn, signin_admin.RULES_KEY, rules_doc, updated_by=ctx.audit_actor_id,
                audit_action="operator.signin_rules.update", audit_target_kind="operator_setting",
                audit_target_id=signin_admin.RULES_KEY,
                audit_metadata={"rules": len(rules_doc["rules"]), "fallback": rules_doc["fallback"]}, **audit)

        def write_mode(conn) -> None:
            def validate(inner):
                if mode in {"oidc", "both"}:
                    stored = admin_store.get_operator_setting_in(inner, "oidc_provider_config")
                    if not (stored and stored["value"].get("enabled")):
                        raise SignInError("oidc_unavailable", "Turn on the provider before using it to sign in.", 409)
            admin_store.put_operator_setting_with_audit_in(
                conn, "sign_in", {"mode": mode}, updated_by=ctx.audit_actor_id, audit_action="operator.sign_in.update",
                audit_target_kind="operator_setting", audit_target_id="sign_in", validate_in_transaction=validate, **audit)

        def write_memory(conn) -> None:
            if memory_after == remembered_people.NOBODY:
                # Remember nobody: every name on every browser goes now, and nothing is written until the owner changes it.
                remembered_people.forget_everyone_in(conn)
            if proposal.memory is not None and proposal.memory != memory_before:
                admin_store.put_operator_setting_with_audit_in(
                    conn, remembered_people.SETTING_KEY, {"mode": proposal.memory}, updated_by=ctx.audit_actor_id,
                    audit_action="operator.sign_in_memory.update", audit_target_kind="operator_setting",
                    audit_target_id=remembered_people.SETTING_KEY, **audit)

        def write_connection(tx) -> None:
            try:
                store.update_oidc_config_setting_in(tx, data=payload, actor=ctx)
            except ValueError as exc:
                raise SignInError("connection_invalid", str(exc), 400) from exc

        def write_status(conn) -> None:
            status = signin_admin.load_status(path)
            if run is not None:
                status["last_test"] = {"at": run.passed_at, "sig": sig, "groups_missing": not run.groups_seen,
                                       "issuer": run.cfg.issuer}
            elif not tested_before(path, sig):
                status.pop("last_test", None)
            signin_admin.write_status_in(conn, status, updated_by=ctx.audit_actor_id)

        secret_store.ensure_provider_secret_schema(path)
        # One transaction: the settings, the rules, and every grant removed, session ended and key revoked
        # for the people they touch commit together, or none of them happen. What a rollback cannot undo
        # (the person's MCP connections) is registered on `tx` and runs only after the commit.
        with admin_store.transaction(path) as tx:
            if revision != signin_admin.revisions_in(tx.conn):
                raise SettingsConflictError()
            write_rules(tx.conn)
            if not wants_connection:
                write_mode(tx.conn)
            elif after.provider:
                write_connection(tx)
                write_mode(tx.conn)
            else:
                write_mode(tx.conn)
                write_connection(tx)
            write_memory(tx.conn)
            write_status(tx.conn)
            removed = signin_admin.apply_changes(
                tx, path, saved, after, plans,
                keep_admin_keys=set(body.keep_admin_keys), owner_id=owner.id if owner else "",
                end_provider_access=end_provider_access,
                actor_label=ctx.actor_label, actor_id=ctx.audit_actor_id, actor_kind=ctx.actor_kind)
        return {
            "snapshot": build_snapshot(request, ctx),
            "applied": {
                "provider_turned_on": bool(after.provider and not saved.provider),
                "admin_removed": [{"name": r["name"], "keys_revoked": r["keys_revoked"], "keys_kept": r["keys_kept"]}
                                  for r in removed],
            },
        }

    # -- small actions ------------------------------------------------------

    @router.post("/admin/api/signin/notice/dismiss")
    def dismiss_notice(request: Request, ctx: AdminContext = Depends(owner_ctx)):
        path = path_provider()
        status = signin_admin.load_status(path)
        if status.get("notice") is not None:
            status["notice"] = None
            signin_admin.write_status(path, status, updated_by=ctx.audit_actor_id)
        return build_snapshot(request, ctx)

    @router.post("/admin/api/signin/owner-password")
    def owner_password(request: Request, body: PasswordBody, ctx: AdminContext = Depends(owner_ctx)):
        path = path_provider()
        if admin_store.get_user(ctx.audit_actor_id, path=path) is None:
            raise SignInError("owner_person_required", "Sign in as the owner to set a password.", 409)
        verifier = accounts.hash_password(body.password)
        try:
            admin_store.set_local_password_verifier(
                ctx.audit_actor_id, verifier, session=ctx.browser_session, path=path)
        except SessionEnded as ended:
            raise SignInError(ended.code, ended.message, ended.status_code) from None
        # A changed password ends every other sign-in; the one making the change stays.
        keep = (ctx.session_id, ctx.session_expires_at) if ctx.session_id and ctx.session_expires_at else None
        accounts.revoke_person_sessions(ctx.audit_actor_id, path=path, keep=keep)
        admin_store.record_audit(
            "signin.owner_password.set", actor_kind=ctx.actor_kind, actor_id=ctx.audit_actor_id,
            actor_label=ctx.actor_label, target_kind="user", target_id=ctx.audit_actor_id, path=path)
        return build_snapshot(request, ctx)

    async def finish_test(
        state: StateRecord,
        *,
        session_user_id: str | None,
        code: str | None,
        error: str | None,
        error_description: str | None,
        exchange_code: Callable[..., Any],
        verify_id_token: Callable[..., Any],
    ) -> Response:
        """The provider's redirect for a test sign-in. Signs no one in; shows a closing page."""
        run = signin_trial.peek(state.test_id)
        if run is not None and run.status == "waiting":
            if session_user_id != run.user_id:
                signin_trial.fail(run, "refused", "the test was opened in another browser session")
            else:
                await signin_trial.finish(
                    run, code=code, error=error, error_description=error_description, nonce=state.nonce,
                    pkce_verifier=state.pkce_verifier, exchange_code=exchange_code, verify_id_token=verify_id_token)
            if run.status == "passed":
                path = path_provider()
                signin_admin.remember_test_values(path, run.claims)
                if run.signature == saved_signature(oidc_config_provider()):
                    status = signin_admin.load_status(path)
                    status["last_test"] = {"at": run.passed_at, "sig": run.signature,
                                           "groups_missing": not run.groups_seen, "issuer": run.cfg.issuer}
                    signin_admin.write_status(path, status)
        return HTMLResponse(_CLOSE_PAGE, headers={"cache-control": "no-store"})

    return SignInApi(router=router, finish_test=finish_test)


class SignInApi:
    """The router, and the callback half of a test sign-in (the OIDC callback route lives in proxy.py)."""

    def __init__(self, *, router: APIRouter, finish_test: Callable[..., Any]):
        self.router = router
        self.finish_test = finish_test


_CLOSE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Sign-in test</title>"
    "<p style=\"font:16px system-ui;margin:2rem\">The test finished. You can close this window.</p>"
)
