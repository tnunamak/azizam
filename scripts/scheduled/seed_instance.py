"""Seed a fresh disposable gateway the way web/e2e/global-setup.ts does, over HTTP.

`seed(base_url, upstream_port, models)` claims the local owner, registers one
openai-compatible service at the upstream, grants it to a profile and app, and
returns an API key for that app. Models are (public, kind) pairs; kind is
`chat` or `embeddings`.
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

PASSWORD = "Portable UI test password 2026"


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect())
        self.csrf: str | None = None

    def request(self, method: str, path: str, *, json_body=None, form=None, headers=None):
        data, sent = None, dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            sent["content-type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
        if self.csrf:
            sent["x-azizam-csrf"] = self.csrf
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=sent)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()

    def call(self, method: str, path: str, *, expect=(200, 201), **kwargs):
        status, headers, body = self.request(method, path, **kwargs)
        if status not in expect:
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        return headers, (json.loads(body) if body else None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def seed(base_url: str, upstream_port: int, models: list[tuple[str, str]]) -> str:
    api = Client(base_url)
    api.call("POST", "/setup/local-owner", expect=(303,), form={
        "email": "owner@example.test", "password": PASSWORD, "password_confirm": PASSWORD})
    _, session = api.call("GET", "/admin/api/session")
    api.csrf = session["actor"]["csrf_token"]
    headers, _ = api.call("GET", "/admin/api/operator/service-registry")
    api.call("POST", "/admin/api/operator/services", headers={"if-match": headers["etag"]}, json_body={
        "id": "probe-upstream", "name": "Probe upstream", "base_url": f"http://127.0.0.1:{upstream_port}",
        "protocol": "openai-compatible", "capabilities": sorted({"chat", "completions", "embeddings"}),
        "health_endpoint": "/health", "models": [{"public": name, "upstream": name} for name, _ in models],
        "model_policy_schema_version": 1, "enabled": True, "note": "Scheduled-run fake upstream."})
    api.call("POST", "/admin/api/config/reload", json_body={})
    _, created = api.call("POST", "/admin/api/profiles", json_body={
        "name": "Probe profile", "description": "SDK probes through the fake upstream",
        "profile": {"id": "probe-profile", "protocols": ["openai", "anthropic", "ollama"],
                    "backends": ["probe-upstream"], "models": [name for name, _ in models],
                    "status_visibility": {"level": "full"}}})
    _, listing = api.call("GET", "/admin/api/users")
    _, app = api.call("POST", "/admin/api/apps", json_body={
        "name": "Probe app", "kind": "personal", "owner_user_id": listing["users"][0]["id"],
        "profile_id": created["profile"]["id"]})
    _, key = api.call("POST", f"/admin/api/apps/{app['app']['id']}/keys", json_body={"label": "probe key"})
    return key["credential"]["api_key"]
