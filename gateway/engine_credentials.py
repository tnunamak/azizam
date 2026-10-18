"""Resolve one engine credential only for its owner-approved origin."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

from gateway import admin_store, secret_store


def credential_headers(service: Mapping[str, Any], operation_class: str) -> dict[str, str]:
    connection = service.get("connection") or (service.get("config") or {}).get("connection") or {}
    if not isinstance(connection, Mapping):
        raise ValueError("engine connection is invalid")
    credentials = connection.get("credentials") or {}
    credential = credentials.get(operation_class) if isinstance(credentials, Mapping) else None
    if credential is None:
        return {}
    if not isinstance(credential, Mapping):
        raise ValueError("engine credential declaration is invalid")
    reference = credential.get("secret_ref")
    if reference is None:
        return {}
    if not isinstance(reference, str) or not reference:
        raise ValueError("engine credential reference is invalid")
    actual = secret_store.credential_origin_from_url(str(connection.get("address") or ""))
    approved = secret_store.credential_origin_from_url(str(credential.get("origin") or ""))
    if actual != approved:
        raise ValueError("engine credential origin does not match connection origin")
    header = credential.get("header") or "Authorization"
    scheme = credential.get("scheme") or ""
    if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]+", header):
        raise ValueError("engine credential header is invalid")
    if not isinstance(scheme, str) or (scheme and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", scheme)):
        raise ValueError("engine credential scheme is invalid")
    if reference.startswith("os.environ/"):
        value = os.environ.get(reference.removeprefix("os.environ/"))
        if not value:
            raise ValueError("engine credential reference is unavailable")
    else:
        value = secret_store.get_provider_secret(
            reference, db_path=admin_store.DB_PATH, expected_origin=actual
        )
    if "\r" in value or "\n" in value:
        raise ValueError("engine credential value is invalid")
    return {header: f"{scheme} {value}" if scheme else value}
