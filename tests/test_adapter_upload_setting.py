"""The owner's setting for member adapter uploads: off by default, owner-only, audited, revision-checked."""
from __future__ import annotations

import pytest

from gateway import adapter_upload, admin_store
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401  (gateway is a fixture)

PATH = "/admin/api/operator/adapter-uploads"


def test_the_setting_is_off_by_default_with_the_default_cap(gateway):
    owner, *_ = owner_and_admin(gateway)
    reply = owner.get(PATH)
    assert reply.status_code == 200
    assert reply.json() == {"enabled": False, "max_bytes": adapter_upload.DEFAULT_MAX_BYTES, "revision": "0",
                            "label": "Let members upload their own adapters"}
    assert adapter_upload.policy(None).enabled is False


def test_the_owner_turns_it_on_and_only_the_owner_can(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    denied = admin.put(PATH, headers={**csrf_for(admin), "If-Match": "0"}, json={"enabled": True})
    assert denied.status_code == 403
    saved = owner.put(PATH, headers={**csrf_for(owner), "If-Match": "0"}, json={"enabled": True, "max_bytes": 1 << 20})
    assert saved.status_code == 200, saved.text
    assert (saved.json()["enabled"], saved.json()["max_bytes"]) == (True, 1 << 20)
    stored = admin_store.get_operator_setting(adapter_upload.SETTING_KEY, path=gateway.path)
    assert adapter_upload.policy(stored["value"]) == adapter_upload.Policy(True, 1 << 20)
    assert admin_store.list_audit(path=gateway.path, action_prefix="operator.adapter_uploads.")


def test_a_write_without_the_current_revision_is_refused(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert owner.put(PATH, headers=csrf_for(owner), json={"enabled": True}).status_code == 428
    assert admin_store.get_operator_setting(adapter_upload.SETTING_KEY, path=gateway.path) is None


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": True, "max_bytes": 0}, {"enabled": True, "max_bytes": "9"},
                                  {"enabled": True, "max_bytes": True}, {"enabled": True, "other": 1}])
def test_a_setting_that_is_not_shaped_as_expected_is_refused_and_nothing_is_stored(gateway, body):
    owner, *_ = owner_and_admin(gateway)
    refused = owner.put(PATH, headers={**csrf_for(owner), "If-Match": "0"}, json=body)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_adapter_uploads"
    assert admin_store.get_operator_setting(adapter_upload.SETTING_KEY, path=gateway.path) is None
