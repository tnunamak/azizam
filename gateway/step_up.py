"""Risky actions need a recent sign-in, not just a live session.

A session now lasts weeks, so a phone left unlocked or a stolen cookie must not
be enough to export the configuration, replace a provider key, hand out an
invitation, change who may sign in, or make a credential that outlives the
session. These routes answer ``401 reauth_required`` to a browser session whose
person has not proved who they are within the owner's step-up window
(gateway.session_policy). The web app shows a password (or provider) prompt and
retries. Keys (``opk_``, ``oak_``) are not browser sessions and are never asked:
a key is its own proof.

Every route that creates a credential, grants a role, changes where a stored
credential is sent, exports configuration or changes how people sign in is in
one of the two lists below (tests/test_step_up_coverage.py fails when a route
the app serves is in neither and has not been reviewed as safe).

Members minting their own App keys are asked too (decision D36): the key
outlives the session and carries the person's access plan, so the cookie alone
must not be enough.
"""
from __future__ import annotations

import re
import time

from gateway.egress import SafeErrorResponse

# "*" stands for one path segment. The middleware asks every browser session, whatever its role.
SENSITIVE_ROUTES: tuple[tuple[str, str], ...] = (
    # Everything the gateway would let a thief carry away. The inline view shows the same document.
    ("GET", "/admin/api/config/export"),
    ("GET", "/admin/api/settings/effective"),
    ("GET", "/admin/api/settings/effective/export"),
    # Provider and engine secrets, admin keys.
    ("POST", "/admin/api/providers"),
    ("PUT", "/admin/api/providers/*/key"),
    ("DELETE", "/admin/api/providers/*/key"),
    ("POST", "/admin/api/operator/litellm/keys"),
    ("POST", "/admin/api/operator/litellm/keys/delete"),
    ("POST", "/admin/api/admin-keys"),
    ("POST", "/admin/api/apps/*/keys"),
    # The same keys made for oneself, and the first key the setup page makes.
    ("POST", "/me/api/apps/*/keys"),
    ("POST", "/setup/first-app"),
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

# Routes that ask inside the handler, because only some requests to them are risky. Each is covered by
# a case in tests/test_step_up_coverage.py. The middleware does not ask for these.
HANDLER_GATED: tuple[tuple[str, str, str], ...] = (
    # Only a new Try key is risky; the page reuses the one it holds. Owners and administrators are asked.
    ("POST", "/me/api/try-key", "a new key for an administrator"),
    # Allowing a client makes a grant that outlives the session; refusing one is harmless.
    ("POST", "/me/api/mcp/requests/*", "an allow decision"),
    # A write is risky when it adds, replaces or moves a credential of a service (service_registry.credential_binding_changes).
    ("PUT", "/admin/api/operator/service-registry", "a changed credential binding"),
    ("POST", "/admin/api/operator/services", "a changed credential binding"),
    ("PATCH", "/admin/api/operator/services/*", "a changed credential binding"),
    ("POST", "/admin/api/operator/services/*/secret-entries", "a changed credential binding"),
    ("PUT", "/admin/api/operator/service-registry/excluded/*/*", "a changed credential binding"),
)


class StepUpRequired(Exception):
    """A risky change came from a browser session that has not confirmed it is the person lately.

    The app answers it with the same 401 as the route list above.
    """

    def __init__(self, subject: str) -> None:
        super().__init__("confirm it is you to do this")
        self.subject = subject


def require_recent(*, authenticated_at: float | None, subject: str, window_seconds: int, now: float | None = None) -> None:
    """Raise ``StepUpRequired`` unless the person proved who they are within the window.

    ``authenticated_at`` is None for a key, which is its own proof.
    """
    if authenticated_at is None:
        return
    if (time.time() if now is None else now) - authenticated_at > window_seconds:
        raise StepUpRequired(subject)


_PATTERNS = tuple(
    (method, re.compile("^" + "/".join("[^/]+" if part == "*" else re.escape(part) for part in path.split("/")) + "/?$"))
    for method, path in SENSITIVE_ROUTES
)


def is_sensitive(method: str, path: str) -> bool:
    method = method.upper()
    return any(m == method and pattern.match(path) for m, pattern in _PATTERNS)


def reauth_required_response(*, methods: list[str], window_seconds: int) -> SafeErrorResponse:
    """The typed 401 the web app answers with a prompt and a retry.

    ``methods`` lists how this person can confirm: ``password``, ``oidc`` (the
    provider, at ``/login?reauth=1``) or ``sign_in`` (neither is possible for
    this session: sign in again).
    """
    # An approved response, so it also passes the egress gate when a management handler raises StepUpRequired.
    return SafeErrorResponse(
        {"error": {
            "code": "reauth_required",
            "message": "Confirm it's you to do this.",
            "reauth": {"methods": methods, "window_seconds": window_seconds, "oidc_url": "/login?reauth=1"},
        }},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )
