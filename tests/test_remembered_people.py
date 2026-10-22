"""Remembered people: the names a browser keeps for its sign-in list (D54, slice A3).

Real routes and a real database, with the identity provider replaced (see tests/test_signin_who_can_enter.py).
A browser holds one opaque device cookie and one tile per remembered person. These tests pin what a tile can and
cannot do: it signs no one in by itself, it resolves only on the device that minted it, and the list carries nothing
but approved metadata. The owner's routes never show a tile or a device.
"""
import asyncio
import hashlib
import re
import time
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway import accounts, admin_store, avatars, remembered_people
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
TWIN = "robin.two@vivid.fish"
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
    # The provider sent no name, so the account's address stands in: the list never shows the address itself.
    assert person["name"] == "helper" and person["avatar"]["initial"] == "H"
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

    # A tile for a provider sign-in starts the provider's own sign-in, asked again, and signs no one in here.
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    provider_tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    start = same_device(tls, client).get("/login", params={"tile": provider_tile}, follow_redirects=False)
    assert start.status_code == 302 and "prompt=login" in start.headers["location"]
    assert session_cookies(start) == []


def test_an_unknown_tile_is_refused_before_any_provider_redirect(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})

    unknown = same_device(tls, client).get("/login", params={"tile": "t_bogus"}, follow_redirects=False)

    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "tile_unknown"
    assert "location" not in unknown.headers and session_cookies(unknown) == []


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
    assert session_cookies(both) == [] and session_cookies(smuggled) == []


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


def test_tile_on_a_provider_sign_in_sends_no_email_and_asks_again(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    [person] = listed(client)["people"]
    stranger = same_device(tls, client)

    start = stranger.get("/login", params={"tile": person["tile"]}, follow_redirects=False)

    assert start.status_code == 302
    location = urlsplit(start.headers["location"])
    query = parse_qs(location.query)
    assert "login_hint" not in query and query["prompt"] == ["login"] and query["max_age"] == ["0"]
    # No email in any URL: neither the address nor its percent-encoded form.
    assert HELPER not in start.headers["location"] and HELPER.replace("@", "%40") not in start.headers["location"]
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
    tiles = [person["tile"] for person in listed(browser)["people"]]
    assert len(tiles) == 2  # Robin and the owner, both on this device's list
    secrets = [device, hashlib.sha256(device.encode()).hexdigest(), *tiles]

    for path in ("/admin/api/people", "/admin/api/invites", "/admin/api/signin",
                 "/admin/api/settings/sessions", "/admin/api/settings/receipts"):
        response = browser.get(path)
        assert response.status_code == 200, (path, response.status_code)
        for secret in secrets:
            assert secret not in response.text, path
        keys = {str(key).lower() for key in keys_of(response.json())}
        assert not keys & {"tile", "tiles", "device", "device_hash", "remembered", "remembered_people"}, path


def test_the_auto_avatar_and_initial_match_the_web_for_the_same_person():
    # Vectors from web/src/components/product/person-avatar.tsx, run as written (node), so the disc is the same
    # in the sign-in list and everywhere else the person's avatar shows.
    assert remembered_people._auto_variant("user_6f1c0a9e2d4b7a1c3e5f7a9b") == "v7"
    assert remembered_people._auto_variant("user_0000") == "v8"
    assert remembered_people._auto_variant("owner-id") == "v7"
    assert remembered_people._auto_variant("a") == "v5"
    assert remembered_people._initial("Léa Okafor") == "L"
    assert remembered_people._initial("3 cats") == "3"
    assert remembered_people._initial("  ") == "?"


def test_two_people_with_one_name_are_told_apart_by_the_start_of_their_address(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, TWIN, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    client = tls.client()
    for email, password in ((MEMBER, MEMBER_PASSWORD), (TWIN, MEMBER_PASSWORD), (NEIGHBOUR, NEIGHBOUR_PASSWORD)):
        sign_in_with_password(client, email, password)

    people = listed(client)["people"]
    assert sorted(person["hint"] for person in people if person["name"] == MEMBER_NAME) == [
        "r…@example.test", "r…@vivid.fish"]
    assert [person["hint"] for person in people if person["name"] == NEIGHBOUR_NAME] == [None]


def test_switching_off_password_sign_in_hides_its_names_and_refuses_their_tiles(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(client, MEMBER_NAME)
    admin_store.put_operator_setting("sign_in", {"mode": "oidc"}, path=tls.path)

    assert listed(client)["people"] == []
    refused = same_device(tls, client).post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "builtin_disabled"
    assert session_cookies(refused) == []
    assert len(kept_rows(tls)) == 1  # hidden, not deleted: the name returns when password sign-in does


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


# ---------------------------------------------------------------------------
# The box: keep, forget, or leave the list alone
# ---------------------------------------------------------------------------

def device_rows(g, client):
    """The people this browser's device keeps on its list, by person id: the stored rows, not what the page shows."""
    device = remembered_people.device_key(client.cookies.get(DEVICE))
    return {row["person_id"] for row in kept_rows(g) if row["device_hash"] == device}


def test_unticking_the_box_on_an_email_sign_in_forgets_that_person_on_this_device_only(tls):
    robin = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    neighbour = make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    here, elsewhere = tls.client(), tls.client()
    sign_in_with_password(here, MEMBER, MEMBER_PASSWORD)
    sign_in_with_password(here, NEIGHBOUR, NEIGHBOUR_PASSWORD)
    sign_in_with_password(elsewhere, MEMBER, MEMBER_PASSWORD)
    assert device_rows(tls, here) == {robin, neighbour}

    quiet = sign_in_with_password(here, MEMBER, MEMBER_PASSWORD, remember=False)

    assert quiet.status_code == 200 and session_cookies(quiet)
    assert device_rows(tls, here) == {neighbour}
    assert device_rows(tls, elsewhere) == {robin}


def test_unticking_the_box_on_a_tile_sign_in_forgets_that_person_only(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    neighbour = make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    sign_in_with_password(client, NEIGHBOUR, NEIGHBOUR_PASSWORD)
    tile = tile_for(client, MEMBER_NAME)

    quiet = same_device(tls, client).post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD, "remember": False})

    assert quiet.status_code == 200 and session_cookies(quiet)
    assert device_rows(tls, client) == {neighbour}


def test_unticking_the_box_on_a_provider_sign_in_forgets_that_person_only(tls):
    robin = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    helper = person_id(tls, HELPER)
    assert device_rows(tls, client) == {robin, helper}

    response = tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "0"})

    assert response.status_code == 302 and session_cookies(response)
    assert device_rows(tls, client) == {robin}


def test_a_sign_in_without_the_box_leaves_the_list_alone(tls):
    robin = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    helper = person_id(tls, HELPER)
    tile = tile_for(client, MEMBER_NAME)

    by_email = same_device(tls, client).post(LOGIN, json={"email": MEMBER, "password": MEMBER_PASSWORD})
    by_tile = same_device(tls, client).post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD, "remember": None})
    by_provider = tls.sign_in(same_device(tls, client), sub="helper", email=HELPER, groups=["/Trusted"])

    assert by_email.status_code == 200 and by_tile.status_code == 200 and by_provider.status_code == 302
    assert device_rows(tls, client) == {robin, helper}


def test_a_failed_sign_in_with_the_box_unticked_forgets_nobody(tls):
    robin = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)

    failed = same_device(tls, client).post(LOGIN, json={"email": MEMBER, "password": "not the password",
                                                        "remember": False})

    assert failed.status_code == 401 and session_cookies(failed) == []
    assert device_rows(tls, client) == {robin}


# ---------------------------------------------------------------------------
# A tile through the provider: fresh, and the same person
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("auth_time", [None, 1_700_000_000], ids=["no-auth-time", "auth-time-long-ago"])
def test_a_tile_sso_sign_in_is_refused_unless_the_provider_signed_the_person_in_again(tls, auth_time):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    helper = person_id(tls, HELPER)
    tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    stranger = same_device(tls, client)

    refused = tls.sign_in(stranger, sub="helper", email=HELPER, groups=["/Trusted"], auth_time=auth_time,
                          start={"tile": tile})

    # A2: a tile sign-in is a fresh sign-in first, so a stale or missing sign-in time is refused as not_fresh.
    assert refused.status_code == 303 and refused.headers["location"].startswith("/sign-in/?error=not_fresh")
    assert session_cookies(refused) == [] and not signed_in_state(stranger)
    assert device_rows(tls, client) == {helper}


def test_a_tile_sso_sign_in_that_returns_someone_else_is_refused_and_adds_no_name(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    helper = person_id(tls, HELPER)
    tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    stranger = same_device(tls, client)

    other = tls.sign_in(stranger, sub="robin-sso", email="robin.sso@example.test", groups=["/Trusted"],
                        start={"tile": tile, "remember": "1"})

    assert other.status_code == 303 and other.headers["location"].startswith("/sign-in/?error=provider")
    assert session_cookies(other) == [] and not signed_in_state(stranger)
    assert tls.user("robin.sso@example.test") is None
    assert device_rows(tls, client) == {helper}


def test_a_tile_flow_stored_without_the_fresh_flag_still_refuses_an_old_sign_in(tls, monkeypatch):
    """A tile flow is fresh by itself (A2 review L2): the callback holds it to a new sign-in whatever the flag says."""
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    stranger = same_device(tls, client)
    stranger.get("/login", params={"tile": tile}, follow_redirects=False)
    state, record = list(tls.proxy.ADMIN_OIDC_STATES.items())[-1]
    tls.proxy.ADMIN_OIDC_STATES[state] = replace(record, fresh=False)
    now_s = int(time.time())

    async def exchange(code, *, config, pkce):
        return {"id_token": "fake.id.token"}

    async def verify(token, *, config):
        return {"iss": config.issuer, "aud": config.client_id, "sub": "helper", "email": HELPER,
                "email_verified": True, "nonce": record.nonce, "exp": 4_000_000_000, "iat": now_s,
                "auth_time": now_s - 3600, "groups": ["/Trusted"]}

    monkeypatch.setattr(tls.proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(tls.proxy, "_oidc_verify_id_token", verify)
    refused = stranger.get("/admin/oidc/callback", params={"state": state, "code": "c"},
                           headers={"accept": "text/html"}, follow_redirects=False)

    assert refused.status_code == 303 and refused.headers["location"].startswith("/sign-in/?error=not_fresh")
    assert session_cookies(refused) == [] and not signed_in_state(stranger)


def test_a_tile_sso_sign_in_as_an_existing_other_person_is_refused_and_changes_nothing(tls):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    helper = person_id(tls, HELPER)
    tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    tls.sign_in(tls.client(), sub="robin-sso", email="robin.sso@example.test", groups=["/Trusted"])
    assert tls.user("robin.sso@example.test") is not None  # Robin already has an account: not a new person
    rows_before = kept_rows(tls)
    stranger = same_device(tls, client)

    other = tls.sign_in(stranger, sub="robin-sso", email="robin.sso@example.test", groups=["/Trusted"],
                        start={"tile": tile, "remember": "1"})

    assert other.status_code == 303 and other.headers["location"].startswith("/sign-in/?error=provider")
    assert session_cookies(other) == [] and not signed_in_state(stranger)
    assert kept_rows(tls) == rows_before and device_rows(tls, client) == {helper}


# ---------------------------------------------------------------------------
# Names that come from an address
# ---------------------------------------------------------------------------

def test_two_names_from_one_address_local_part_on_different_domains_get_no_hints(tls):
    make_member(tls, "robin@alpha.example.test", "", MEMBER_PASSWORD)
    make_member(tls, "robin@beta.example.test", "", MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, "robin@alpha.example.test", MEMBER_PASSWORD)
    sign_in_with_password(client, "robin@beta.example.test", MEMBER_PASSWORD)

    people = listed(client)["people"]
    assert [person["name"] for person in people] == ["robin", "robin"]
    assert [person["hint"] for person in people] == [None, None]
    text = client.get("/auth/api/remembered").text
    assert "alpha.example.test" not in text and "beta.example.test" not in text


def test_a_provider_name_that_is_the_address_local_part_gets_no_hint(tls):
    make_member(tls, "alice@corp.test", "alice", MEMBER_PASSWORD)
    make_member(tls, "alice@other.test", "alice", MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, "alice@corp.test", MEMBER_PASSWORD)
    sign_in_with_password(client, "alice@other.test", MEMBER_PASSWORD)

    people = listed(client)["people"]
    assert [person["name"] for person in people] == ["alice", "alice"]
    # "alice" beside a hint would give the whole address away: the hint adds the domain.
    assert [person["hint"] for person in people] == [None, None]
    text = client.get("/auth/api/remembered").text
    assert "corp.test" not in text and "other.test" not in text


# ---------------------------------------------------------------------------
# What a sign-in may write
# ---------------------------------------------------------------------------

def test_keep_reads_the_owner_setting_inside_its_write_transaction(tls, monkeypatch):
    person = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    statements = []
    opened = accounts.connection

    class Traced:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            statements.append((" ".join(sql.split()), tuple(parameters)))
            return self.conn.execute(sql, parameters)

    @contextmanager
    def traced(path):
        with opened(path) as conn:
            yield Traced(conn)

    monkeypatch.setattr(accounts, "connection", traced)
    remembered_people.keep(path=tls.path, token=remembered_people.new_device_token(), person_id=person,
                           method="password", issuer=None, remember=True, from_tile=False)

    begin = statements.index(("BEGIN IMMEDIATE", ()))
    reads = [index for index, (sql, parameters) in enumerate(statements)
             if "FROM operator_settings" in sql and parameters == (remembered_people.SETTING_KEY,)]
    assert reads and reads[0] > begin


def test_a_tile_sign_in_adds_no_name_the_list_does_not_hold(tls):
    person = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    token = remembered_people.new_device_token()

    minted = remembered_people.keep(path=tls.path, token=token, person_id=person, method="password", issuer=None,
                                    remember=True, from_tile=True)

    assert minted is None and kept_rows(tls) == []
    # The same box from the sign-in form keeps the name, so the empty list is down to the tile.
    remembered_people.keep(path=tls.path, token=token, person_id=person, method="password", issuer=None,
                           remember=True, from_tile=False)
    assert [row["person_id"] for row in kept_rows(tls)] == [person]


def test_remember_sets_no_device_cookie_for_a_password_tile_or_under_nobody(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    browser = tls.client()
    sign_in_with_password(browser, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(browser, MEMBER_NAME)

    # The name is already on this browser: the tile sign-in refreshes it and mints no cookie.
    again = browser.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD, "remember": True})
    assert again.status_code == 200 and session_cookies(again) and device_cookies(again) == []

    owner = owner_client(tls)
    snapshot = owner.get("/admin/api/signin").json()
    nobody = owner.put("/admin/api/signin", json={"proposal": {**proposal(snapshot), "memory": "nobody"},
                                                  "test_id": None, "keep_admin_keys": [], "save_for_later": False},
                       headers={**csrf(owner), "If-Match": snapshot["revision"]})
    assert nobody.status_code == 200, nobody.text

    # A browser with no device cookie yet: under "nobody" nothing is kept, so no cookie is set.
    newcomer = sign_in_with_password(tls.client(), MEMBER, MEMBER_PASSWORD, remember=True)
    assert newcomer.status_code == 200 and session_cookies(newcomer) and device_cookies(newcomer) == []
    assert kept_rows(tls) == []


def test_removing_or_disabling_a_person_forgets_their_names_on_every_browser(tls):
    robin = make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    neighbour = make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    make_member(tls, "ada@example.test", "Ada", MEMBER_PASSWORD)
    ada = person_id(tls, "ada@example.test")
    here, elsewhere = tls.client(), tls.client()
    for browser in (here, elsewhere):
        for email, password in ((MEMBER, MEMBER_PASSWORD), (NEIGHBOUR, NEIGHBOUR_PASSWORD),
                                ("ada@example.test", MEMBER_PASSWORD)):
            sign_in_with_password(browser, email, password)

    accounts.change_person(robin, "test", disabled=True, path=tls.path)
    accounts.change_person(neighbour, "test", remove=True, path=tls.path)

    # Their names go at once, on every browser, not when a list is next read.
    assert {row["person_id"] for row in kept_rows(tls)} == {ada}


def test_a_sign_in_sweeps_names_unused_for_ninety_days_from_every_browser(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    old, current = tls.client(), tls.client()
    sign_in_with_password(old, MEMBER, MEMBER_PASSWORD)
    with accounts.connection(tls.path) as conn:
        conn.execute("UPDATE remembered_people SET last_signed_in_at=?",
                     (int(time.time() * 1000) - 91 * DAY * 1000,))

    sign_in_with_password(current, NEIGHBOUR, NEIGHBOUR_PASSWORD)

    # The old browser never reads its list again, so nothing but the sign-in itself can remove the row.
    assert [row["person_id"] for row in kept_rows(tls)] == [person_id(tls, NEIGHBOUR)]


# ---------------------------------------------------------------------------
# The owner's password switch and the list
# ---------------------------------------------------------------------------

def test_with_password_sign_in_off_no_password_name_is_listed_not_even_the_owners(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    client = tls.client()
    sign_in_with_password(client, MEMBER, MEMBER_PASSWORD)
    tls.sign_in(client, sub="owner", email=OWNER, start={"remember": "1"})
    admin_store.set_local_password_verifier(person_id(tls, OWNER), accounts.hash_password(OWNER_PASSWORD),
                                            path=tls.path)
    assert sign_in_with_password(client, OWNER, OWNER_PASSWORD).status_code == 200
    admin_store.put_operator_setting("sign_in", {"mode": "oidc"}, path=tls.path)

    # The owner can still sign in with a password, but their name is not a way in this browser shows.
    assert listed(client) == {"remember": True, "people": []}


# ---------------------------------------------------------------------------
# The tile start looks the name up off the event loop
# ---------------------------------------------------------------------------

def test_the_tile_sign_in_start_looks_the_name_up_off_the_event_loop(tls, monkeypatch):
    client = tls.client()
    tls.sign_in(client, sub="helper", email=HELPER, groups=["/Trusted"], start={"remember": "1"})
    tile = next(person["tile"] for person in listed(client)["people"] if person["method"] == "sso")
    on_the_loop = []
    real_resolve = remembered_people.resolve

    def resolve_and_note(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_the_loop.append(False)
        else:
            on_the_loop.append(True)
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(remembered_people, "resolve", resolve_and_note)
    start = same_device(tls, client).get("/login", params={"tile": tile}, follow_redirects=False)

    assert start.status_code == 302 and on_the_loop == [False]


# ---------------------------------------------------------------------------
# Guards that pass on the fixed code too (mutation-check gaps)
# ---------------------------------------------------------------------------

def test_a_tile_from_another_device_is_refused_even_when_the_request_has_its_own_device_cookie(tls):
    make_member(tls, MEMBER, MEMBER_NAME, MEMBER_PASSWORD)
    make_member(tls, NEIGHBOUR, NEIGHBOUR_NAME, NEIGHBOUR_PASSWORD)
    here = tls.client()
    sign_in_with_password(here, MEMBER, MEMBER_PASSWORD)
    tile = tile_for(here, MEMBER_NAME)
    elsewhere = tls.client()  # another browser on another device, with a list of its own
    sign_in_with_password(elsewhere, NEIGHBOUR, NEIGHBOUR_PASSWORD)
    assert elsewhere.cookies.get(DEVICE) != here.cookies.get(DEVICE)

    refused = elsewhere.post(LOGIN, json={"tile": tile, "password": MEMBER_PASSWORD})

    assert refused.status_code == 404 and refused.json()["error"]["code"] == "tile_unknown"
    assert session_cookies(refused) == []
    assert tile not in [person["tile"] for person in listed(elsewhere)["people"]]


def test_sixty_one_unknown_tiles_from_one_address_are_refused_with_429(tls):
    client = tls.client()
    responses = [client.post(LOGIN, json={"tile": f"t_guess{index:02d}", "password": "a guess"}) for index in range(61)]

    assert [response.status_code for response in responses[:60]] == [404] * 60
    assert responses[60].status_code == 429 and responses[60].json()["error"]["code"] == "login_rate_limited"
