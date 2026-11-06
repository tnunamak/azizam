"""A client secret goes only to the host it was entered for.

Saving a new provider address without typing the secret again would carry the old secret over to the new host,
and the next test sign-in would send it there. Every save refuses that, whether the provider is on, off or being
saved for later, and whether the saved secret is stored or came from the bootstrap file.
"""
from gateway import admin_store
from tests.test_signin_settings_api import (  # noqa: F401  (sg is a fixture)
    get_snapshot, owner_client, preview, proposal, provider_off, save, sg,
)
from tests.test_signin_who_can_enter import gateway  # noqa: F401  (a fixture sg builds on)

EVIL = "https://evil.example/realms/home"
SAME_HOST_OTHER_REALM = "https://id.example/realms/other"
REFUSAL = "Enter the client secret again for the new address."


def stored_connection(g):
    return admin_store.get_operator_setting("oidc_provider_config", path=g.path)


def test_save_for_later_at_a_new_host_with_no_secret_typed_is_refused_for_a_bootstrap_secret(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)
    assert stored_connection(sg) is None                       # the saved secret comes from the bootstrap file

    refused = save(owner, snapshot, proposal(snapshot, address=EVIL, client_id="stolen"), save_for_later=True)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == {"code": "connection_incomplete", "message": REFUSAL}
    assert stored_connection(sg) is None
    assert get_snapshot(owner)["provider"]["address"] == snapshot["provider"]["address"]


def test_save_for_later_at_a_new_host_with_no_secret_typed_is_refused_for_a_stored_secret(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)
    first = save(owner, snapshot, proposal(snapshot, client_secret="the-secret-for-id"), save_for_later=True)
    assert first.status_code == 200, first.text
    snapshot = first.json()["snapshot"]
    before = stored_connection(sg)
    assert sg.proxy.ADMIN_STORE.get_oidc_config_setting_raw()["client_secret"] == "the-secret-for-id"

    refused = save(owner, snapshot, proposal(snapshot, address=EVIL), save_for_later=True)

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connection_incomplete"
    assert stored_connection(sg) == before
    assert sg.proxy.ADMIN_STORE.get_oidc_config_setting_raw()["client_secret"] == "the-secret-for-id"


def test_an_ordinary_save_with_the_provider_off_and_a_new_address_typed_is_refused_too(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, address=EVIL))

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connection_incomplete"
    assert stored_connection(sg) is None


def test_a_new_endpoint_on_another_host_needs_the_secret_typed_again_too(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)

    refused = save(owner, snapshot, proposal(snapshot, token_endpoint="https://evil.example/token"),
                   save_for_later=True)

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "connection_incomplete"
    assert stored_connection(sg) is None


def test_typing_the_secret_again_saves_the_new_address_and_replaces_the_secret(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, address=EVIL, client_secret="the-secret-for-evil"),
                 save_for_later=True)

    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["provider"]["address"] == EVIL
    assert sg.proxy.ADMIN_STORE.get_oidc_config_setting_raw()["client_secret"] == "the-secret-for-evil"


def test_the_same_host_with_another_path_keeps_the_saved_secret(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)

    saved = save(owner, snapshot, proposal(snapshot, address=SAME_HOST_OTHER_REALM), save_for_later=True)

    assert saved.status_code == 200, saved.text
    assert saved.json()["snapshot"]["provider"]["client_secret_saved"] is True


def test_the_preview_says_the_secret_is_needed_before_the_owner_presses_save(sg):
    owner = owner_client(sg)
    provider_off(sg)
    snapshot = get_snapshot(owner)

    without = preview(owner, proposal(snapshot, address=EVIL))
    typed = preview(owner, proposal(snapshot, address=EVIL, client_secret="typed"))
    same = preview(owner, proposal(snapshot))

    assert {"code": "secret_needed", "message": REFUSAL} in without["blockers"]
    assert "secret_needed" not in [b["code"] for b in typed["blockers"]]
    assert "secret_needed" not in [b["code"] for b in same["blockers"]]
