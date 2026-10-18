"""Risky owner actions need a recent sign-in, not just a live session.

A session now lasts weeks, so a phone left unlocked or a stolen cookie must not
be enough to export the configuration, replace a provider key, hand out an
invitation or change who may sign in. These routes answer ``401
reauth_required`` to a browser session whose person has not proved who they are
within the owner's step-up window (gateway.session_policy). The web app shows a
password (or provider) prompt and retries. Keys (``opk_``, ``oak_``) are
not browser sessions and are never asked: a key is its own proof.

This is the one list of those routes. Add a route here, not an inline check.
"""
from __future__ import annotations

import re

from starlette.responses import JSONResponse

# "*" stands for one path segment.
SENSITIVE_ROUTES: tuple[tuple[str, str], ...] = (
    # Everything the gateway would let a thief carry away.
    ("GET", "/admin/api/config/export"),
    ("GET", "/admin/api/settings/effective/export"),
    # Provider and engine secrets, admin keys.
    ("POST", "/admin/api/providers"),
    ("PUT", "/admin/api/providers/*/key"),
    ("DELETE", "/admin/api/providers/*/key"),
    ("POST", "/admin/api/operator/litellm/keys"),
    ("POST", "/admin/api/operator/litellm/keys/delete"),
    ("POST", "/admin/api/admin-keys"),
    ("POST", "/admin/api/apps/*/keys"),
    # A trusted address lets a device in with no key; an extension package can read this computer.
    ("POST", "/admin/api/apps/*/trusted-addresses"),
    ("POST", "/admin/api/extensions/enable"),
    # Whether Azizam may run commands on this computer.
    ("PUT", "/admin/api/operator/host-commands"),
    # Who is in, and with which role.
    ("POST", "/admin/api/invites"),
    ("GET", "/admin/api/invites/*/link"),
    ("PATCH", "/admin/api/people/*"),
    ("DELETE", "/admin/api/people/*"),
    ("POST", "/admin/api/people/*/reset"),
    # How people sign in (rules decide who becomes an administrator).
    ("PUT", "/admin/api/signin"),
    ("POST", "/admin/api/signin/link-owner"),
    ("POST", "/admin/api/signin/owner-password"),
    # How long a sign-in lasts.
    ("PUT", "/admin/api/settings/sessions"),
)

_PATTERNS = tuple(
    (method, re.compile("^" + "/".join("[^/]+" if part == "*" else re.escape(part) for part in path.split("/")) + "/?$"))
    for method, path in SENSITIVE_ROUTES
)


def is_sensitive(method: str, path: str) -> bool:
    method = method.upper()
    return any(m == method and pattern.match(path) for m, pattern in _PATTERNS)


def reauth_required_response(*, methods: list[str], window_seconds: int) -> JSONResponse:
    """The typed 401 the web app answers with a prompt and a retry.

    ``methods`` lists how this person can confirm: ``password``, ``oidc`` (the
    provider, at ``/login?reauth=1``) or ``sign_in`` (neither is possible for
    this session: sign in again).
    """
    return JSONResponse(
        {"error": {
            "code": "reauth_required",
            "message": "Confirm it's you to do this.",
            "reauth": {"methods": methods, "window_seconds": window_seconds, "oidc_url": "/login?reauth=1"},
        }},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )
