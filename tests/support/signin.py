"""Seed sign-in rules in tests the way the old allow-lists were written."""
from __future__ import annotations

from typing import Iterable

from gateway import admin_store, signin_rules_migration


def seed_rules(
    path,
    *,
    domains: Iterable[str] = (),
    emails: Iterable[str] = (),
    groups: Iterable[str] = (),
    roles: Iterable[str] = (),
    fallback: str | None = None,
) -> dict:
    """Write the ``signin_rules`` setting for a test database.

    ``domains`` and ``emails`` let people in; ``groups`` and ``roles`` make them
    administrators. With no domain or email and no ``fallback`` the door is open,
    like the old empty allow-lists. Pass ``fallback="closed"`` to close it.
    """
    document, _ = signin_rules_migration._rules_from_lists(
        {
            "enabled": True,
            "allowed_email_domains": list(domains),
            "admin_emails": list(emails),
            "admin_groups": list(groups),
            "admin_roles": list(roles),
        },
        set(),
    )
    if fallback is not None:
        document["fallback"] = fallback
    admin_store.put_operator_setting("signin_rules", document, path=path)
    return document
