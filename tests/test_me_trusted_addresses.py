"""Owner visibility step 2: a person trusts an address for their own App from their signed-in session.

This is the signed-in twin of the admin route. Only the App's accountable person may add an address, the
address records that person as its creator, and another person's App is refused with not_your_app.
"""

from __future__ import annotations

from tests.test_signin_who_can_enter import csrf_for, gateway, signed_in  # noqa: F401  (gateway is a fixture)

ACT = {"x-azizam-user-action": "1"}


def _member(g, sub):
    g.set_rules([], fallback="open")
    client, response = signed_in(g, sub=sub, email=f"{sub}@example.test")
    assert response.status_code == 302, response.text
    return client


def _headers(client):
    return {**csrf_for(client), **ACT}


def _own_app(client, name="Desk"):
    created = client.post("/me/api/apps", headers=_headers(client), json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["app"]["id"]


def test_a_person_trusts_an_address_for_their_own_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)

    added = desk.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(desk), json={"address": "10.0.0.1"})

    assert added.status_code == 201, added.text
    entry = added.json()["trusted_address"]
    assert entry["address"] == "10.0.0.1"
    assert entry["created_by"] == gateway.user("desk@example.test")["id"]


def test_a_person_cannot_trust_an_address_for_someone_elses_app(gateway):
    desk = _member(gateway, "desk")
    app_id = _own_app(desk)
    other = _member(gateway, "other")

    refused = other.post(f"/me/api/apps/{app_id}/trusted-addresses", headers=_headers(other), json={"address": "10.0.0.2"})

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_your_app"
    assert gateway.rows("SELECT COUNT(*) AS n FROM app_trusted_addresses")[0]["n"] == 0
