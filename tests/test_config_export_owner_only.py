"""The config export is a backup that round-trips, so it keeps every stored value and belongs to the owner.

An administrator may manage engines but may not read a stored start command or environment value
(the engine views hide them behind a marker). The export holds those values whole, so an
administrator who could read it would read what every other view hides.
"""

from __future__ import annotations

import pytest

from tests.test_engine_redaction import SECRET, _seed
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


def test_the_owner_reads_the_config_export_with_the_stored_values_intact(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    exported = owner.get("/admin/api/config/export")
    assert exported.status_code == 200, exported.text
    assert SECRET in exported.text  # a backup that cannot restore the command is not a backup


def test_the_owner_reads_the_settings_export(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    assert owner.get("/admin/api/settings/effective/export").status_code == 200
