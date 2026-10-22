"""Remembered people: the names a browser keeps for its sign-in list (D54, slice A3).

Real routes and a real database, with the identity provider replaced (see tests/test_signin_who_can_enter.py).
A browser holds one opaque device cookie and one tile per remembered person. These tests pin what a tile can and
cannot do: it signs no one in by itself, it resolves only on the device that minted it, and the list carries nothing
but approved metadata. The owner's routes never show a tile or a device.
"""
import hashlib
import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway import accounts, admin_store, avatars
from gateway.admin_oidc import CookieConfig
from gateway.session_policy import DAY
from tests.test_session_lifecycle import (  # noqa: F401  (fixtures and their dependencies)
    COOKIE, csrf, owner_client, person_id, put_cookie, session_cookies, set_password, sg, signed_in_state,
)
from tests.test_signin_settings_api import proposal
from tests.test_signin_who_can_enter import OWNER, gateway  # noqa: F401

DEVICE = "__Host-azizam-device"
HELPER = "helper@example.test"
MEMBER = "robin@example.test"
MEMBER_NAME = "Robin Okafor"
MEMBER_PASSWORD = "Robin's own password, long enough"
NEIGHBOUR = "lea@example.test"
NEIGHBOUR_NAME = "Léa Okafor"
NEIGHBOUR_PASSWORD = "Léa's own password, long enough"
OWNER_PASSWORD = "the owner's own password, long enough"
LOGIN = "/auth/api/login"


@pytest.fixture
def tls(sg):
    """Production cookie attributes: Secure, so the browser keeps the __Host- device cookie."""
    sg.set_config(replace(sg.config, cookie=CookieConfig(secure=True)))
    return sg


def make_member(g, email, name, password):
    """A person who signs in with a password: a user row and a verifier, as an accepted invitation leaves them."""
    user = admin_store.upsert_oidc_user("local", email, email=email, display_name=name, path=g.path)
    admin_store.set_local_password_verifier(user["id"], accounts.hash_password(password), path=g.path)
    return user["id"]


def sign_in_with_password(client, email, password, *, remember=True):
    return client.post(LOGIN, json={"email": email, "password": password, "remember": remember})


def listed(client):
    response = client.get("/auth/api/remembered")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def tile_for(client, name):
    [tile] = [person["tile"] for person in listed(client)["people"] if person["name"] == name]
    return tile


def same_device(g, client):
    """Another browser on this device: the device cookie comes with it, the person's session does not."""
    other = g.client()
    other.cookies.set(DEVICE, client.cookies.get(DEVICE), domain="ai.example", path="/")
    return other


def kept_rows(g):
    return g.rows("SELECT * FROM remembered_people ORDER BY person_id")


def device_cookies(response):
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(DEVICE + "=")]


def keys_of(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_of(item)


# ---------------------------------------------------------------------------
# What the browser may learn
# ---------------------------------------------------------------------------

def test_remembered_returns_only_tile_name_avatar_method_provider_hint(tls):
    user_id = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    avatars.record_sign_in_picture(user_id, "https://pictures.example/robin.png", path=tls.path)
    client = tls.client()
    assert sign_in_with_password(client, MEMBER, MEMBER_PASSWORD).status_code == 200

    body = listed(client)
    assert set(body) == {"remember", "people"} and body["remember"] is True
    [person] = body["people"]
    assert set(person) == {"tile", "name", "avatar", "method", "provider", "hint"}
    assert set(person["avatar"]) == {"variant", "initial"}
    assert person["name"] == MEMBER_NAME and person["method"] == "password"
    assert person["provider"] is None and person["hint"] is None
    assert person["avatar"]["initial"] == "R" and re.fullmatch(r"v[1-8]", person["avatar"]["variant"])
    text = client.get("/auth/api/remembered").text
    for leaked in (MEMBER, user_id, "pictures.example", "@", "picture"):
        assert leaked not in text


def test_device_cookie_is_host_prefixed_httponly_secure_lax(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    response = sign_in_with_password(tls.client(), MEMBER, MEMBER_PASSWORD)

    [header] = device_cookies(response)
    attributes = header.split("; ")
    assert "HttpOnly" in attributes and "Secure" in attributes and "Path=/" in attributes
    assert "SameSite=lax" in attributes
    assert not [part for part in attributes if part.lower().startswith("domain=")]
    assert abs(int(re.search(r"Max-Age=(\d+)", header).group(1)) - 400 * DAY) <= 5
    value = header.split(";")[0].split("=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)  # 32 random bytes, base64url


def test_on_a_plain_http_gateway_the_device_cookie_drops_the_prefix_and_secure(sg):
    make_member(sg, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    response = sign_in_with_password(sg.client(), MEMBER, MEMBER_PASSWORD)

    [header] = [value for value in response.headers.get_list("set-cookie") if value.startswith("azizam-device=")]
    assert "HttpOnly" in header and "SameSite=lax" in header and "Path=/" in header
    assert "Secure" not in header


def test_sso_sign_in_is_remembered_only_when_asked(tls):
    browser = tls.client()
    tls.sign_in(browser, sub="helper", email=HELPER, groups=["/Trusted"])
    assert kept_rows(tls) == [] and not browser.cookies.get(DEVICE)

    tls.sign_in(browser, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    [person] = listed(browser)["people"]
    assert person["method"] == "sso" and person["provider"] == "single sign-on"
    assert HELPER not in browser.get("/auth/api/remembered").text


# ---------------------------------------------------------------------------
# A tile is a handle, never a sign-in
# ---------------------------------------------------------------------------

def test_a_tile_alone_never_signs_anyone_in(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(client, MEMBER_NAME)
    stranger = same_device(tls, client)  # the device cookie, and no session

    no_password = stranger.post(LOGIN, json={"tile": tile})
    empty_password = stranger.post(LOGIN, json={"tile": tile, "password": ""})

    assert no_password.status_code == 422
    assert empty_password.status_code == 401 and empty_password.json()["error"]["code"] == "invalid_login"
    assert session_cookies(no_password) == [] and session_cookies(empty_password) == []
    assert not signed_in_state(stranger)


def test_tile_login_resolves_the_person_from_the_device_not_the_request(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    here = tls.client()
    sign_in_with_password(here, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(here, MEMBER_NAME)

    elsewhere = tls.client()  # another browser: no device cookie, so no list
    unknown = elsewhere.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "tile_unknown"
    assert session_cookies(unknown) == []

    both = here.post(LOGIN, json={"tile": tile, "email": NEIGHBOUR, "password": MEMBER_PASSWORD})
    smuggled = here.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD,
                                      "user_id": person_id(tls, NEIGHBOUR)})
    assert both.status_code == 422 and smuggled.status_code == 422
    assert not signed_in_state(here)


def test_a_tile_sign_in_ends_the_session_the_browser_already_had(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(client, MEMBER_NAME)
    sign_in_with_password(client, NEIGHBOUR, NEIGHBOUR_PASSWORD)
    previous = tls.client()
    put_cookie(previous, client.cookies.get(COOKIE))
    assert signed_in_state(previous)

    response = client.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD})

    assert response.status_code == 200 and len(session_cookies(response)) == 1
    assert not signed_in_state(previous)
    assert signed_in_state(client)


def test_tile_for_removed_or_disabled_person_returns_tile_unknown(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    sign_in_with_password(client, NEIGHBOUR, NEIGHBOUR_PASSWORD)
    member_tile = tile_for(client, MEMBER_NAME)
    neighbour_tile = tile_for(client, NEIGHBOUR_NAME)
    accounts.change_person(person_id(tls, MEMBER), "test", disabled=True, path=tls.path)
    accounts.change_person(person_id(tls, NEIGHBOUR), "test", remove=True, path=tls.path)

    for tile, password in ((member_tile, MEMBER_PASSWORD), (neighbour_tile, NEIGHBOUR_PASSWORD)):
        response = same_device(tls, client).post(LOGIN, json={"tile": tile, "password": password})
        assert response.status_code == 404 and response.json()["error"]["code"] == "tile_unknown"
        assert session_cookies(response) == []


def test_wrong_password_on_tile_is_invalid_login_and_counts_toward_the_throttle(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)  # the first attempt on Robin's email
    tile = tile_for(client, MEMBER_NAME)
    stranger = same_device(tls, client)

    first = stranger.post(LOGIN, json={"tile": tile, "password": "not the password"})
    assert first.status_code == 401 and first.json()["error"]["code"] == "invalid_login"
    assert session_cookies(first) == []

    limited = None
    for _ in range(12):
        limited = stranger.post(LOGIN, json={"tile": tile, "password": "not the password"})
        if limited.status_code == 429:
            break
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "login_rate_limited"
    # The same per-email limit covers email sign-in, so the right password waits too.
    blocked = stranger.post(LOGIN, json={"email": MEMBER, "password": MEMBER_PASSWORD})
    assert blocked.status_code == 429


def test_tile_on_a_provider_sign_in_sends_the_email_as_login_hint_and_asks_again(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    [person] = listed(client)["people"]
    stranger = same_device(tls, client)

    start = stranger.get("/login", params={"tile": person["tile"]}, follow_redirects=False)

    assert start.status_code == 302
    location = urlsplit(start.headers["location"])
    query = parse_qs(location.query)
    assert query["login_hint"] == [HELPER] and query["prompt"] == ["login"] and query["max_age"] == ["0"]
    assert HELPER not in start.text
    assert not session_cookies(start)
    # The provider sign-in is the only way in: a tile with a password is refused, as it always is for a provider person.
    assert stranger.post(LOGIN, json={"tile": person["tile"], "password": MEMBER_PASSWORD}).status_code == 401


def test_rename_shows_in_the_list_without_a_new_sign_in(tls):
    user_id = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(client, MEMBER_NAME)

    with accounts.connection(tls.path) as conn:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", ("Robin Okafor-Reyes", user_id))

    [person] = listed(client)["people"]
    assert person["name"] == "Robin Okafor-Reyes" and person["tile"] == tile


def test_owner_cannot_sign_in_from_a_members_tile_without_the_members_password(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tls.sign_in(client, sub="owner", email=OWNER, start={"remember": "1"})
    set_password(tls, OWNER)
    admin_store.set_local_password_verifier(person_id(tls, OWNER), accounts.hash_password(OWNER_PASSWORD), path=tls.path)
    tile = tile_for(client, MEMBER_NAME)

    refused = client.post(LOGIN, json={"tile": tile, "password": OWNER_PASSWORD})

    assert refused.status_code == 401 and refused.json()["error"]["code"] == "invalid_login"
    assert session_cookies(refused) == []
    accepted = client.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD})
    assert accepted.status_code == 200


def test_owner_routes_expose_no_tile_or_device_data(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    browser = tls.client()
    sign_in_with_password(browser, MEMBER, MEMBER_PASSWORD)
    tls.sign_in(browser, sub="owner", email=OWNER, start={"remember": "1"})  # the owner, on the same device
    device = browser.cookies.get(DEVICE)
    secrets = [device, hashlib.sha256(device.encode()).hexdigest(), *[p["tile"] for p in listed(browser)["people"]]]
    assert len(secrets) == 3

    for path in ("/admin/api/people", "/admin/api/invites", "/admin/api/signin",
                 "/admin/api/settings/sessions", "/admin/api/settings/receipts"):
        response = browser.get(path)
        assert response.status_code == 200, (path, response.status_code)
        for secret in secrets:
            assert secret not in response.text, path
        keys = {str(key).lower() for key in keys_of(response.json())}
        assert not keys & {"tile", "tiles", "device", "device_hash", "remembered", "remembered_people"}, path


def test_remembered_list_is_rate_limited_per_ip(tls):
    client = tls.client()
    statuses = [client.get("/auth/api/remembered").status_code for _ in range(61)]

    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429


# ---------------------------------------------------------------------------
# Who is kept, how many, and for how long
# ---------------------------------------------------------------------------

def test_remembered_skips_removed_disabled_and_90_day_old_rows(tls):
    people = {name: make_member(tls, f"{name.lower()}@example.test", name, MEMBER_PASSWORD)
              for name in ("Ada", "Ben", "Cleo", "Dev", "Eli")}
    client = tls.client()
    for name in people:
        assert sign_in_with_password(client, f"{name.lower()}@example.test", MEMBER_PASSWORD).status_code == 200
    accounts.change_person(people["Ben"], "test", remove=True, path=tls.path)
    accounts.change_person(people["Cleo"], "test", disabled=True, path=tls.path)
    now_ms = int(time.time() * 1000)
    with accounts.connection(tls.path) as conn:
        conn.execute("UPDATE remembered_people SET last_signed_in_at=? WHERE person_id=?",
                     (now_ms - 91 * DAY * 1000, people["Dev"]))
        conn.execute("UPDATE remembered_people SET last_signed_in_at=? WHERE person_id=?",
                     (now_ms - 89 * DAY * 1000, people["Eli"]))

    assert sorted(person["name"] for person in listed(client)["people"]) == ["Ada", "Eli"]
    assert sorted(row["person_id"] for row in kept_rows(tls)) == sorted([people["Ada"], people["Eli"]])


def test_remember_writes_only_when_asked_and_keeps_at_most_ten_per_browser(tls):
    browser = tls.client()
    names = [(f"person{index}@example.test", f"Person {index:02d}") for index in range(11)]
    for email, name in names:
        make_member(tls, email, name, MEMBER_PASSWORD)

    quiet = sign_in_with_password(browser, names[0][0], MEMBER_PASSWORD, remember=False)

    assert quiet.status_code == 200 and device_cookies(quiet) == []
    assert kept_rows(tls) == []
    for email, _ in names:
        assert sign_in_with_password(browser, email, MEMBER_PASSWORD, remember=True).status_code == 200
    kept = [person["name"] for person in listed(browser)["people"]]
    assert len(kept) == 10
    assert "Person 00" not in kept
    assert len(kept_rows(tls)) == 10


def test_a_name_remembered_for_another_provider_is_hidden_not_deleted(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    assert [person["method"] for person in listed(client)["people"]] == ["sso"]
    original = tls.config

    tls.set_config(replace(original, issuer="https://id.example/realms/elsewhere"))
    assert listed(client)["people"] == []
    assert len(kept_rows(tls)) == 1

    tls.set_config(original)
    assert [person["method"] for person in listed(client)["people"]] == ["sso"]


# ---------------------------------------------------------------------------
# Forget, and the owner's setting
# ---------------------------------------------------------------------------

def test_forget_removes_the_name_from_this_browser_only(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    first, second = tls.client(), tls.client()
    sign_in_with_password(first, MEMBER, MEMBER_PASSWORD)
    sign_in_with_password(second, MEMBER, MEMBER_PASSWORD)
    tile_first, tile_second = tile_for(first, MEMBER_NAME), tile_for(second, MEMBER_NAME)
    assert tile_first != tile_second

    other_device = first.delete(f"/auth/api/remembered/{tile_second}")
    assert other_device.status_code == 200
    assert [person["tile"] for person in listed(first)["people"]] == [tile_first]

    forgotten = first.delete(f"/auth/api/remembered/{tile_first}")
    assert forgotten.status_code == 200 and listed(first)["people"] == []
    assert [person["tile"] for person in listed(second)["people"]] == [tile_second]
    assert signed_in_state(second)


def test_remember_nobody_deletes_every_row_and_stops_writing(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    browser = tls.client()
    sign_in_with_password(browser, MEMBER, MEMBER_PASSWORD)
    assert kept_rows(tls) != []
    owner = owner_client(tls)
    snapshot = owner.get("/admin/api/signin").json()
    assert snapshot["memory"] == "remember"

    def save(body):
        return owner.put("/admin/api/signin", json={"proposal": body, "test_id": None, "keep_admin_keys": [],
                                                  "save_for_later": False},
                         headers={**csrf(owner), "If-Match": snapshot["revision"]})

    nobody = save({**proposal(snapshot), "memory": "nobody"})
    assert nobody.status_code == 200, nobody.text
    assert nobody.json()["snapshot"]["memory"] == "nobody"
    assert kept_rows(tls) == []
    assert listed(browser) == {"remember": False, "people": []}

    again = sign_in_with_password(browser, MEMBER, MEMBER_PASSWORD, remember=True)
    assert again.status_code == 200 and session_cookies(again) and device_cookies(again) == []
    assert kept_rows(tls) == []

    snapshot = owner.get("/admin/api/signin").json()
    unchanged = owner.put("/admin/api/signin", json={"proposal": proposal(snapshot), "test_id": None,
                                                     "keep_admin_keys": [], "save_for_later": False},
                          headers={**csrf(owner), "If-Match": snapshot["revision"]})
    assert unchanged.status_code == 200 and unchanged.json()["snapshot"]["memory"] == "nobody"


def test_owner_setting_can_be_saved_back_to_remember(tls):
    owner = owner_client(tls)
    snapshot = owner.get("/admin/api/signin").json()
    nobody = owner.put("/admin/api/signin", json={"proposal": {**proposal(snapshot), "memory": "nobody"},
                                                  "test_id": None, "keep_admin_keys": [], "save_for_later": False},
                       headers={**csrf(owner), "If-Match": snapshot["revision"]})
    assert nobody.status_code == 200 and nobody.json()["snapshot"]["memory"] == "nobody"

    stale = owner.put("/admin/api/signin", json={"proposal": {**proposal(snapshot), "memory": "remember"},
                                                 "test_id": None, "keep_admin_keys": [], "save_for_later": False},
                      headers={**csrf(owner), "If-Match": snapshot["revision"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"

    fresh = owner.get("/admin/api/signin").json()
    back = owner.put("/admin/api/signin", json={"proposal": {**proposal(fresh), "memory": "remember"},
                                                "test_id": None, "keep_admin_keys": [], "save_for_later": False},
                     headers={**csrf(owner), "If-Match": fresh["revision"]})
    assert back.status_code == 200 and back.json()["snapshot"]["memory"] == "remember"
