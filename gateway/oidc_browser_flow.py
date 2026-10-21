"""Bind pending OIDC sign-ins to their initiating browser and callback host."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import MutableMapping
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from gateway.admin_oidc import (
    OIDCConfig, SessionError, StateRecord, authorization_url, consume_state,
    generate_nonce, generate_pkce_pair, generate_state, record_state,
)


def callback_host_matches(request: Request, redirect_uri: str) -> bool:
    """Compare callback authorities without trusting the TLS proxy's scheme."""
    callback = urlsplit(redirect_uri)
    default_port = 443 if callback.scheme == "https" else 80
    incoming = urlsplit(str(request.url))
    return (
        incoming.hostname == callback.hostname
        and (incoming.port or default_port) == (callback.port or default_port)
    )


def begin_browser_login(
    config: OIDCConfig,
    store: MutableMapping[str, StateRecord],
    *,
    return_to: str,
    test_id: str = "",
    reauth: bool = False,
) -> Response:
    """Issue a separate host-only cookie for each ten-minute sign-in flow.

    ``reauth`` asks the provider to sign the person in again (step-up) rather
    than reuse its own session.
    """
    state_value = generate_state()
    binding = secrets.token_urlsafe(32)
    prefix = "__Host-gateway-oidc-" if config.cookie.secure else "gateway-oidc-"
    cookie_name = prefix + hashlib.sha256(state_value.encode()).hexdigest()[:32]
    nonce = generate_nonce()
    pkce = generate_pkce_pair()
    state = record_state(
        store, state=state_value, nonce=nonce, pkce_verifier=pkce.verifier,
        return_to=return_to, redirect_uri=config.redirect_uri,
        browser_cookie_name=cookie_name,
        browser_binding_hash=hashlib.sha256(binding.encode()).hexdigest(),
        test_id=test_id, reauth=reauth,
    )
    response = RedirectResponse(
        authorization_url(config, state=state.state, nonce=nonce, pkce=pkce,
                          extra_params={"prompt": "login", "max_age": "0"} if reauth else None),
        status_code=302, headers={"cache-control": "no-store"},
    )
    response.set_cookie(
        cookie_name, binding, max_age=600, secure=config.cookie.secure,
        httponly=True, samesite="lax", path="/",
    )
    return response


def consume_browser_state(
    request: Request, store: MutableMapping[str, StateRecord], state_value: str,
) -> StateRecord:
    """Validate browser possession before consuming state or exchanging a code."""
    state = store.get(state_value)
    if state is None:
        raise SessionError("unknown or already-used state")
    binding = request.cookies.get(state.browser_cookie_name, "")
    if (
        not state.browser_binding_hash or not binding
        or not hmac.compare_digest(
            hashlib.sha256(binding.encode()).hexdigest(), state.browser_binding_hash,
        )
        or not callback_host_matches(request, state.redirect_uri)
    ):
        raise SessionError("sign-in browser binding missing or invalid")
    return consume_state(store, state_value)


def clear_browser_binding(response: Response, state: StateRecord) -> Response:
    response.delete_cookie(
        state.browser_cookie_name, path="/",
        secure=state.browser_cookie_name.startswith("__Host-"),
        httponly=True, samesite="lax",
    )
    response.headers["cache-control"] = "no-store"
    return response
