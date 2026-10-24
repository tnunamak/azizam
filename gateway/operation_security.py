"""Say, for each operation in the API reference, how its caller is checked.

Each operation has one mode. The mode names what the enforcement path does: the frontend mount checks an
App key before every operation that does not check its caller itself, and a self-authenticating operation
declares its own mode where it is mounted (``SelfAuthOp`` in proxy.py). A route outside those paths
declares its mode where it is registered. This module maps a mode to its OpenAPI security requirement.

It imports nothing from the gateway, so the mount and the schema can both use it.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

# Calls carry an App key: ``Authorization: Bearer``, or one of the key headers, or a trusted client address.
APP_KEY = "app_key"
# Calls carry the owner's admin credential (OwnerBearer). App keys are refused.
OWNER = "owner"
# Calls carry the signed-in browser session cookie. A call without one is refused.
BROWSER_SESSION = "browser_session"
# Calls are admitted without a session. A signed-in session changes the answer, so the cookie is optional.
OPTIONAL_SESSION = "optional_session"
# Calls carry an HMAC signature and expiry in the query string. The signature is the credential.
SIGNED_URL = "signed_url"
# Calls need no credential. The operation takes its own token in the body, or none at all.
PUBLIC = "public"

# The modes a self-authenticating operation may declare (SelfAuthOp). The rest are checked by the
# mount, or declared on the route.
SELF_AUTH_MODES = frozenset({APP_KEY, SIGNED_URL})

BROWSER_SESSION_SCHEME = "BrowserSession"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def security_for(mode: str) -> list[dict[str, list]]:
    """The OpenAPI ``security`` list for a mode. Each requirement is an alternative; ``[]`` means no credential."""
    if mode == APP_KEY:
        # The three App key schemes gateway/app_key_openapi.py declares; any one carries the key.
        return [{"AppKeyBearer": []}, {"AppKeyApiKey": []}, {"AppKeyXApiKey": []}]
    if mode == OWNER:
        return [{"OwnerBearer": []}]
    if mode == BROWSER_SESSION:
        return [{BROWSER_SESSION_SCHEME: []}]
    if mode == OPTIONAL_SESSION:
        return [{}, {BROWSER_SESSION_SCHEME: []}]
    if mode in (SIGNED_URL, PUBLIC):
        # The query signature is not a header scheme, and no header credential is needed.
        return []
    raise ValueError(f"unknown security mode: {mode!r}")


def openapi_extra(mode: str) -> dict[str, Any]:
    """The route option that puts a mode's ``security`` on the operation."""
    return {"security": security_for(mode)}


def augment_schemes(schema: dict, *, cookie_name: str) -> None:
    """Declare the browser session scheme that the session modes name."""
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes[BROWSER_SESSION_SCHEME] = {
        "type": "apiKey", "in": "cookie", "name": cookie_name,
        "description": "The signed-in browser session. The sign-in pages send it; API clients do not need it.",
    }


def fill_security(schema: dict, *, is_public: Callable[[str], bool], key_layer_prefixes: Iterable[str]) -> None:
    """Give an operation that declares no mode the mode its path implies: public paths need no credential,
    and every operation under the gateway key layer needs an App key."""
    prefixes = tuple(key_layer_prefixes)
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS or "security" in operation:
                continue
            if is_public(path):
                operation["security"] = security_for(PUBLIC)
            elif path.startswith(prefixes):
                operation["security"] = security_for(APP_KEY)
