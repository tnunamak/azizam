"""Fakes for subscription connection tests: a scripted upstream, a fixed identity proof, a store in a temp directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx

from gateway import subscription_kinds, subscription_oauth, subscription_store
from gateway.connections.oauth_pkce import OAuthPkceMechanism
from tests.support.fake_upstream.scripted import ScriptedUpstream as Upstream  # noqa: F401  (tests import it from here)

KIND = subscription_kinds.load_kinds()["chatgpt"]


class FixedJwks:
    """Stands in for the signature check: returns the claims a test sets."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] = {}

    async def verify(self, token: str, *, uri: str, leeway: int) -> dict[str, Any]:
        if token != "id-token":
            raise ValueError("bad token")
        return dict(self.claims)


def token_body(access: str = "access-1", refresh: str = "refresh-1", **extra: Any) -> dict[str, Any]:
    return {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "id_token": "id-token",
            "token_type": "Bearer", "scope": KIND.oauth["scope"], **extra}


def json_route(body: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


def make_service(tmp_path: Path, upstream: Upstream, *, now: Callable[[], float] | None = None):
    clock = now or (lambda: 1_000_000.0)
    store = subscription_store.SubscriptionStore(lambda: tmp_path / "subscriptions.db", clock=clock)
    jwks = FixedJwks()
    jwks.claims = {"iss": KIND.oauth["issuer"], "aud": ["dynamic-issued"], "sub": "acct-1", "email": "owner@example.test",
                   "exp": clock() + 600, "nonce": "set-per-test"}
    service = subscription_oauth.SubscriptionService(
        store, {"chatgpt": KIND}, http=upstream.client, mechanisms={KIND.mechanism: OAuthPkceMechanism(jwks)}, clock=clock)
    up = urlparts(KIND.oauth["token_url"])
    upstream.routes[("GET", urlparts(KIND.oauth["discovery_url"]))] = json_route(
        {"jwks_uri": "https://issuer.test/jwks", "revocation_endpoint": "https://issuer.test/revoke"})
    upstream.routes[("POST", "/revoke")] = json_route({})
    upstream.routes[("GET", "/v1/models")] = json_route({"models": [
        {"slug": "gpt-test", "visibility": "list",
         "supported_reasoning_levels": [{"effort": "low", "description": "x"}, {"effort": "medium", "description": "x"},
                                        {"effort": "high", "description": "x"}]},
        {"slug": "hidden-one", "visibility": "hide"}]})
    del up
    return service, store, jwks


def urlparts(url: str) -> str:
    return httpx.URL(str(url)).path


def callback_for(started: dict[str, Any], *, code: str = "the-code", client_id: str = "dynamic-issued") -> str:
    query = httpx.URL(started["authorize_url"]).params
    return f"{started['redirect_uri']}?code={code}&state={query['state']}&client_id={client_id}"


async def connect(service, jwks, upstream, *, owner: str = "owner_1", token: dict[str, Any] | None = None) -> dict[str, Any]:
    upstream.routes[("POST", urlparts(KIND.oauth["token_url"]))] = json_route(token or token_body())
    started = await service.start(owner, "chatgpt", session=None)
    jwks.claims["nonce"] = httpx.URL(started["authorize_url"]).params["nonce"]
    return await service.finish(owner, started["attempt_id"], callback_for(started), session=None)


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)
