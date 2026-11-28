"""Declare in the OpenAPI schema how an App key is sent.

The header names come from ``gateway.runtime_auth.CLIENT_KEY_HEADERS``, the same tuple the proxy
reads, so the schema cannot name a header the gateway does not accept. This adds schemes only.
Which operations need a key is declared where the operations are described.
"""
from __future__ import annotations

from gateway.runtime_auth import CLIENT_KEY_HEADERS


def augment_app_key_openapi(schema: dict) -> None:
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["AppKeyBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "An App key from Apps, sent as `Authorization: Bearer <key>`. "
            "A request that sends no key is admitted only from a client address the owner trusted for an App."
        ),
    }
    for header in CLIENT_KEY_HEADERS:
        name = "AppKey" + "".join(part.capitalize() for part in header.split("-"))
        schemes[name] = {
            "type": "apiKey",
            "in": "header",
            "name": header,
            "description": f"The same App key, sent in the `{header}` header instead of Authorization.",
        }
