"""Test sign-in: try a provider connection before it is saved, without signing anyone in.

The owner starts a test from Settings with the connection as typed (saved or
not). A pop-up window runs the normal provider sign-in for that connection, and
the callback hands the result to :func:`finish` instead of creating a person, a
personal App or a session. What the provider sent is kept in memory for a few
minutes, so the page can show it, preview the rules against it, and (for the
owner's own identity) link it to the owner. Only the values the provider sent
(groups, roles, email) are remembered, for the rule pickers.

A failed test names one cause in plain words. The causes are data keyed by the
standard OAuth error, never by which provider the owner uses.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from gateway import log_scrub
from gateway.admin_oidc import (
    IDTokenClaimsError, OIDCConfig, SignInRefused, provider_signed_in_again, validate_id_token_claims,
)
from gateway.signin_admin import norm_issuer
from gateway.signin_rules import Claims

TEST_TTL_SECONDS = 900
MAX_TESTS = 20
GROUPS_WARNING = "no_groups"

# Plain words per cause. {name} is the provider as the owner named it (else its
# host); {host} is its address; {detail} is what the provider said, shortened.
CAUSES: dict[str, str] = {
    "redirect_not_allowed": (
        "{name} refused the redirect address. Add the address at the top of this form to the "
        "allowed redirect addresses of your client in {name}, then test again."
    ),
    "wrong_secret": (
        "{name} did not accept the client secret. Copy it again from your client’s settings in {name}."
    ),
    "unverified_email": (
        "{name} signed you in, but says your email is not verified. Azizam turns away unverified emails."
    ),
    "unreachable": "Azizam could not reach {host}. Check the address and that this host can reach it.",
    "refused": "{name} refused the sign-in: {detail}.",
}


def connection_signature(key: bytes, cfg: OIDCConfig, secret: str) -> str:
    """Names the connection a test covers. The secret is in it, so it is keyed."""
    payload = json.dumps(
        [
            norm_issuer(cfg.issuer), cfg.client_id, secret, list(cfg.scopes), cfg.groups_claim,
            cfg.authorization_endpoint, cfg.token_endpoint, cfg.jwks_uri, cfg.clock_skew_seconds,
        ],
        sort_keys=True,
    )
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


@dataclass
class TestRun:
    __test__ = False  # not a pytest class

    id: str
    user_id: str
    cfg: OIDCConfig
    signature: str
    created_at: float
    # waiting | passed | failed
    status: str = "waiting"
    cause: str = ""
    message: str = ""
    details: str = ""
    issuer: str = ""
    subject: str = ""
    claims: Claims = field(default_factory=Claims)
    name: str = ""
    email_verified: bool = False
    claim_names: tuple[str, ...] = ()
    passed_at: int = 0
    groups_seen: bool = True
    # Whether the provider signed the person in just now, as a sign-in from the sign-in page must be (A2): the answer
    # Settings shows as "asks for a password every time". The test passes either way.
    fresh_login_supported: bool = False
    error_hint: str = ""

    @property
    def provider_name(self) -> str:
        return self.cfg.display_name or _host(self.cfg.issuer) or "your provider"


_RUNS: dict[str, TestRun] = {}


def _host(address: str) -> str:
    try:
        return urlsplit(address).netloc
    except ValueError:
        return ""


def _prune(now: float) -> None:
    for key, run in tuple(_RUNS.items()):
        if now - run.created_at > TEST_TTL_SECONDS:
            del _RUNS[key]
    while len(_RUNS) >= MAX_TESTS:
        del _RUNS[min(_RUNS, key=lambda k: _RUNS[k].created_at)]


def create(user_id: str, cfg: OIDCConfig, signature: str, *, now: float | None = None) -> TestRun:
    current = time.time() if now is None else now
    _prune(current)
    # One test per owner at a time: starting another cancels the first.
    for key, run in tuple(_RUNS.items()):
        if run.user_id == user_id:
            del _RUNS[key]
    run = TestRun(id="test_" + secrets.token_urlsafe(18), user_id=user_id, cfg=cfg, signature=signature,
                  created_at=current)
    _RUNS[run.id] = run
    return run


def get(test_id: str, user_id: str, *, now: float | None = None) -> TestRun | None:
    """The owner's own test, if it is still around."""
    current = time.time() if now is None else now
    run = _RUNS.get(test_id)
    if run is None or run.user_id != user_id or current - run.created_at > TEST_TTL_SECONDS:
        return None
    return run


def peek(test_id: str, *, now: float | None = None) -> TestRun | None:
    """A run by id alone, for the callback; it checks the browser session itself."""
    current = time.time() if now is None else now
    run = _RUNS.get(test_id)
    if run is None or current - run.created_at > TEST_TTL_SECONDS:
        return None
    return run


def cancel(test_id: str, user_id: str) -> bool:
    run = _RUNS.get(test_id)
    if run is None or run.user_id != user_id:
        return False
    del _RUNS[test_id]
    return True


def clear() -> None:
    _RUNS.clear()


def fail(run: TestRun, cause: str, detail: str = "", raw: str = "") -> None:
    """End the test with one named cause. ``detail`` is a short phrase; ``raw`` is for Show details."""
    detail = " ".join(detail.split())[:200] or "no reason given"
    run.status, run.cause = "failed", cause
    run.message = CAUSES[cause].format(name=run.provider_name, host=_host(run.cfg.issuer) or run.cfg.issuer,
                                       detail=detail.rstrip("."))
    run.details = raw or detail


def _oauth_code(value: Any) -> str:
    """The OAuth ``error`` code of a provider's reply when it is one of the short lower-case codes the standard names."""
    text = str(value or "")
    return text if re.fullmatch(r"[a-z][a-z_]{0,39}", text) else ""


def _says_redirect(*texts: str) -> bool:
    return any("redirect" in (text or "").lower() for text in texts)


def _describe_http_error(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    """Cause and detail for a failed token request, from the standard OAuth error body."""
    try:
        body = exc.response.json()
    except ValueError:
        body = {}
    error = _oauth_code(body.get("error") if isinstance(body, Mapping) else None)
    description = str(body.get("error_description") or "") if isinstance(body, Mapping) else ""
    # The provider's own words (``error_description``) are read to tell the causes apart and never shown: a provider
    # echoes what it was sent, and this request carried the client secret (decision D44).
    detail = f"{exc.response.status_code} {error}".strip()
    if _says_redirect(error, description):
        return "redirect_not_allowed", detail
    if exc.response.status_code == 401 or error in {"invalid_client", "unauthorized_client"}:
        return "wrong_secret", detail
    return "refused", error or f"the provider answered {exc.response.status_code}"


async def finish(
    run: TestRun,
    *,
    code: str | None,
    error: str | None,
    error_description: str | None,
    nonce: str,
    pkce_verifier: str,
    exchange_code: Callable[..., Awaitable[dict[str, Any]]],
    verify_id_token: Callable[..., Awaitable[Mapping[str, Any]]],
    flow_started_at: float,
    now: float | None = None,
) -> None:
    """Record the outcome of the provider's redirect to the callback. Never signs anyone in."""
    from gateway.admin_oidc import PKCEPair

    cfg = run.cfg
    if error:
        # The provider sent these through the owner's browser on the sign-in redirect, which carried no secret; they are
        # shown to the owner with the registered secrets removed. (The token request's reply is not shown: see below.)
        raw = log_scrub.scrub(f"error={error}" + (f"\nerror_description={error_description}" if error_description else ""), limit=600)
        cause = "redirect_not_allowed" if _says_redirect(error, error_description or "") else "refused"
        fail(run, cause, log_scrub.scrub(error_description or error, limit=300), raw)
        return
    try:
        tokens = await exchange_code(code, config=cfg, pkce=PKCEPair(verifier=pkce_verifier, challenge="already-sent"))
    except httpx.HTTPStatusError as exc:
        cause, detail = _describe_http_error(exc)
        fail(run, cause, detail)
        return
    except httpx.RequestError as exc:
        fail(run, "unreachable", type(exc).__name__)
        return
    except ValueError as exc:
        fail(run, "refused", str(exc))
        return
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        fail(run, "refused", "the provider did not send an ID token (is the openid scope on?)")
        return
    try:
        claims = await verify_id_token(id_token, config=cfg)
    except httpx.RequestError as exc:
        fail(run, "unreachable", type(exc).__name__)
        return
    except Exception as exc:  # noqa: BLE001 - any verification failure is a refusal, with its class only
        fail(run, "refused", f"the ID token could not be verified ({type(exc).__name__})")
        return
    try:
        validate_id_token_claims(claims, config=cfg, expected_nonce=nonce, now=now)
    except SignInRefused:
        fail(run, "unverified_email", "email_verified is false")
        return
    except IDTokenClaimsError as exc:
        fail(run, "refused", str(exc))
        return
    from gateway import signin_admin

    view = signin_admin.claim_view(claims, client_id=cfg.client_id, groups_claim=cfg.groups_claim)
    run.status = "passed"
    run.issuer = str(claims.get("iss") or cfg.issuer)
    run.subject = str(claims.get("sub") or "")
    run.claims = view
    run.name = str(claims.get("name") or claims.get("email") or "")
    run.email_verified = claims.get("email_verified") is True
    run.claim_names = tuple(sorted(str(name) for name in claims.keys()))
    run.groups_seen = cfg.groups_claim in claims
    run.fresh_login_supported = provider_signed_in_again(claims, since=flow_started_at)
    run.passed_at = int(time.time() if now is None else now)
    run.details = (
        f"Claims received: {', '.join(run.claim_names)}\n"
        + (f'Groups arrived under the name "{cfg.groups_claim}".' if run.groups_seen
           else f'No claim named "{cfg.groups_claim}".')
        + f"\nRoles: {', '.join(view.roles) or 'none'}"
    )


def passed_for(test_id: str | None, user_id: str, signature: str) -> TestRun | None:
    """The owner's passed test, only if it covered exactly this connection."""
    if not test_id:
        return None
    run = get(test_id, user_id)
    if run is None or run.status != "passed" or not hmac.compare_digest(run.signature, signature):
        return None
    return run
