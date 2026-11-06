"""The config export belongs to the owner: it is the whole configuration, public settings and configured entries.

An administrator may manage engines but may not read the owner's export. The export holds references and public
settings, never a secret value (decision D26; a restorable encrypted backup of the secrets themselves is phase 2).
"""

from __future__ import annotations

import pytest

from tests.test_engine_view_secrets import SECRET, _seed
from tests.test_signin_who_can_enter import gateway, owner_and_admin, signed_in  # noqa: F401

EXPORTS = ["/admin/api/config/export", "/admin/api/settings/effective/export"]


@pytest.mark.parametrize("route", EXPORTS)
def test_an_administrator_cannot_read_an_export(gateway, route) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed(gateway, owner)
    refused = admin.get(route)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert SECRET not in refused.text


@pytest.mark.parametrize("route", EXPORTS)
def test_a_member_cannot_read_an_export(gateway, route) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    member, _ = signed_in(gateway, sub="member", email="member@example.test")
    refused = member.get(route)
    assert refused.status_code in (401, 403), refused.text
    assert SECRET not in refused.text


def test_the_owner_reads_the_config_export_with_every_secret_as_a_configured_entry_and_no_value(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    exported = owner.get("/admin/api/config/export")
    assert exported.status_code == 200, exported.text
    assert SECRET not in exported.text and "configured" in exported.text and "sref_" not in exported.text


def test_the_owner_reads_the_settings_export(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    assert owner.get("/admin/api/settings/effective/export").status_code == 200
