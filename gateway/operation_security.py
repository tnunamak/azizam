"""Say, for each operation in the API reference, how its caller is checked.

Each operation has one mode. The mode names what the enforcement path does: the frontend mount checks an
App key before every operation that does not check its caller itself, and a self-authenticating operation
declares its own mode where it is mounted (``SelfAuthOp`` in proxy.py). A route outside those paths
declares its mode where it is registered. This module maps a mode to its OpenAPI security requirement.

It imports nothing from the gateway, so the mount and the schema can both use it.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Iterable

# Calls carry an App key: ``Authorization: Bearer``, or one of the key headers, or a trusted client address.
APP_KEY = "app_key"
# Calls carry the owner's or an admin's credential (OwnerBearer), or the admin session cookie. The owner and any
# admin role are admitted; App keys are refused. An operation only the owner may call says so in its tag.
OWNER = "owner"
# Calls carry the signed-in browser session cookie. A call without one is refused.
BROWSER_SESSION = "browser_session"
# Calls are admitted without a session. A signed-in session changes the answer, so the cookie is optional.
OPTIONAL_SESSION = "optional_session"
# Calls carry an HMAC signature and expiry in the query string. The signature is the credential.
SIGNED_URL = "signed_url"
# Calls need no credential. The operation takes its own token in the body, or none at all.
PUBLIC = "public"
# Calls carry an App key, or on SESSION_KEY_LAYER_PATHS the signed-in browser session of the person who made them.
APP_KEY_OR_SESSION = "app_key_or_session"

# The modes a self-authenticating operation may declare (SelfAuthOp). The rest are checked by the
# mount, or declared on the route.
SELF_AUTH_MODES = frozenset({APP_KEY, SIGNED_URL})

BROWSER_SESSION_SCHEME = "BrowserSession"
SIGNED_LINK_SCHEME = "SignedLink"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# The paths where a browser session passes the gateway key layer: the maker's own request history. The middleware
# (proxy._maker_browser_session_request) and the reference (fill_security) both read this list, so they cannot drift.
SESSION_KEY_LAYER_PATHS = ("/gateway/v1/requests",)

# The query parameters a signed link carries. augment_signed_links documents them on each operation a link reaches.
SIGNED_LINK_PARAMETERS = (
    {"name": "sig", "in": "query", "required": True, "schema": {"type": "string"},
     "description": "The HMAC signature the gateway issued with this link. A changed or missing signature is refused."},
    {"name": "expires", "in": "query", "required": True, "schema": {"type": "integer"},
     "description": "Unix time, in seconds, after which the link is refused. Send it with `sig`."},
)


def session_passes_key_layer(path: str) -> bool:
    """Whether a browser session passes the key layer on this path: a path in SESSION_KEY_LAYER_PATHS, or below one."""
    return any(path == base or path.startswith(base + "/") for base in SESSION_KEY_LAYER_PATHS)


def security_for(mode: str) -> list[dict[str, list]]:
    """The OpenAPI ``security`` list for a mode. Each requirement is an alternative; ``[]`` means no credential."""
    if mode == APP_KEY:
        # The three App key schemes gateway/app_key_openapi.py declares; any one carries the key.
        return [{"AppKeyBearer": []}, {"AppKeyApiKey": []}, {"AppKeyXApiKey": []}]
    if mode == OWNER:
        # The admin credential, or the admin session: AdminAuthenticator takes either.
        return [{"OwnerBearer": []}, {BROWSER_SESSION_SCHEME: []}]
    if mode == BROWSER_SESSION:
        return [{BROWSER_SESSION_SCHEME: []}]
    if mode == OPTIONAL_SESSION:
        return [{}, {BROWSER_SESSION_SCHEME: []}]
    if mode == SIGNED_URL:
        # The query signature is the credential. It is declared as a query scheme, and its parameters as well.
        return [{SIGNED_LINK_SCHEME: []}]
    if mode == APP_KEY_OR_SESSION:
        return [*security_for(APP_KEY), {BROWSER_SESSION_SCHEME: []}]
    if mode == PUBLIC:
        return []
    raise ValueError(f"unknown security mode: {mode!r}")


def openapi_extra(mode: str) -> dict[str, Any]:
    """The route option that puts a mode's ``security`` on the operation."""
    return {"security": security_for(mode)}


def augment_schemes(schema: dict, *, cookie_name: str) -> None:
    """Declare the schemes the modes name: the browser session cookie, and the query signature of a signed link."""
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes[BROWSER_SESSION_SCHEME] = {
        "type": "apiKey", "in": "cookie", "name": cookie_name,
        "description": "The signed-in browser session. The sign-in pages send it; API clients do not need it.",
    }
    schemes[SIGNED_LINK_SCHEME] = {
        "type": "apiKey", "in": "query", "name": "sig",
        "description": (
            "A signed link. Its `sig` query parameter is an HMAC the gateway issued with the link, and its `expires` "
            "query parameter must also be present. Links come from the gateway's own responses, such as `download_url` "
            "and `thumbnail_url`; a client cannot make one."
        ),
    }


def augment_signed_links(schema: dict) -> None:
    """Document the query parameters of each operation a signed link reaches, after its security is set."""
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or {SIGNED_LINK_SCHEME: []} not in operation.get("security", []):
                continue
            parameters = operation.setdefault("parameters", [])
            named = {p.get("name") for p in parameters if p.get("in") == "query"}
            parameters.extend(copy.deepcopy(p) for p in SIGNED_LINK_PARAMETERS if p["name"] not in named)


def fill_security(schema: dict, *, is_public: Callable[[str], bool], key_layer_prefixes: Iterable[str]) -> None:
    """Give an operation that declares no mode the mode its path implies: public paths need no credential,
    and every operation under the gateway key layer needs an App key, or a browser session on the paths that take one."""
    prefixes = tuple(key_layer_prefixes)
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS or "security" in operation:
                continue
            if is_public(path):
                operation["security"] = security_for(PUBLIC)
            elif path.startswith(prefixes):
                mode = APP_KEY_OR_SESSION if session_passes_key_layer(path) else APP_KEY
                operation["security"] = security_for(mode)
