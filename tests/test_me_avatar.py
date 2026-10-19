"""A person's avatar: the stored choice, the sign-in picture and the gateway route that serves it."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, avatars, media_fetch
from gateway.admin_oidc import AdminSession
from gateway.me_avatar import PictureCache, create_me_avatar_router, image_type
from tests.support.fake_upstream import HandlerTransport

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
PICTURE = "https://lh3.example.test/a/photo.png"


def _session_for(who: dict[str, str]):
    def inner(_request: Request) -> AdminSession | None:
        subject = who.get("subject")
        if not subject:
            return None
        return AdminSession(subject=subject, email=f"{subject}@example.test", role="none",
                            issued_at=time.time(), expires_at=time.time() + 3600)
    return inner


def _validator(request: Request, _session: AdminSession):
    if request.headers.get("x-azizam-user-action") != "1":
        return JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    return None


class Rig:
    def __init__(self, tmp_path: Path, fetched=None):
        self.db = tmp_path / "admin.db"
        admin_store.init_db(self.db)
        self.who: dict[str, str] = {}
        self.fetches: list[str] = []
        self.fetched = fetched if fetched is not None else (lambda url: media_fetch.FetchedMedia(PNG, "p.png", "image/png"))

        async def fetcher(url: str) -> media_fetch.FetchedMedia:
            self.fetches.append(url)
            result = self.fetched(url)
            if isinstance(result, BaseException):
                raise result
            return result

        app = FastAPI()
        app.include_router(create_me_avatar_router(
            session_provider=_session_for(self.who), mutation_validator=_validator,
            admin_db_path=self.db, picture_fetcher=fetcher, cache=PictureCache()))
        self.client = TestClient(app)

    def person(self, name: str, *, picture: str | None = None) -> str:
        user = admin_store.upsert_oidc_user("https://idp.example", name, email=f"{name}@example.test",
                                            display_name=name.title(), path=self.db)
        if picture is not None:
            avatars.record_sign_in_picture(user["id"], picture, path=self.db)
        return user["id"]

    def as_(self, person_id: str | None) -> "Rig":
        self.who.clear()
        if person_id:
            self.who["subject"] = person_id
        return self


ACTION = {"x-azizam-user-action": "1"}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_default_is_auto_with_the_person_id_and_no_picture(rig: Rig) -> None:
    alice = rig.person("alice")
    body = rig.as_(alice).client.get("/me/api/profile/avatar").json()
    assert body == {"choice": "auto", "person_id": alice, "picture_url": None}


def test_signed_out_is_401(rig: Rig) -> None:
    assert rig.client.get("/me/api/profile/avatar").status_code == 401
    assert rig.client.put("/me/api/profile/avatar", json={"choice": "v1"}, headers=ACTION).status_code == 401
    assert rig.client.get("/me/api/profile/avatar/picture").status_code == 401
    assert rig.client.get("/me/api/profile/avatars").status_code == 401


def test_put_needs_the_user_action_header_like_other_member_writes(rig: Rig) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    assert client.put("/me/api/profile/avatar", json={"choice": "v3"}).status_code == 403
    assert avatars.get(alice, path=rig.db)["choice"] == "auto"
    saved = client.put("/me/api/profile/avatar", json={"choice": "v3"}, headers=ACTION)
    assert saved.status_code == 200
    assert saved.json() == {"choice": "v3", "person_id": alice, "picture_url": None}
    assert client.get("/me/api/profile/avatar").json()["choice"] == "v3"


@pytest.mark.parametrize("choice", ["auto", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"])
def test_every_generated_choice_and_auto_is_accepted(rig: Rig, choice: str) -> None:
    alice = rig.person("alice")
    assert rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": choice}, headers=ACTION).status_code == 200


@pytest.mark.parametrize("body", [
    {"choice": "v0"}, {"choice": "v9"}, {"choice": "V1"}, {"choice": ""}, {"choice": None}, {"choice": 3},
    {"choice": ["v1"]}, {"choice": "https://evil.example/x.png"}, {"choice": "/me/api/profile/avatar/picture"},
    {}, {"url": "https://evil.example/x.png"}, [], "v1",
    # Anything beside the word is refused, not ignored: no way to smuggle a URL in.
    {"choice": "v1", "picture_url": "https://evil.example/x.png"},
    {"choice": "picture", "url": "https://evil.example/x.png"},
])
def test_put_validates_the_enum_and_stores_no_url(rig: Rig, body) -> None:
    alice = rig.person("alice", picture=PICTURE)
    response = rig.as_(alice).client.put("/me/api/profile/avatar", json=body, headers=ACTION)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_avatar_choice"
    stored = avatars.get(alice, path=rig.db)
    assert stored["choice"] == "auto"
    assert stored["picture_url"] == PICTURE  # the sign-in picture is untouched


def test_put_with_a_body_that_is_not_json_is_400(rig: Rig) -> None:
    alice = rig.person("alice")
    response = rig.as_(alice).client.put("/me/api/profile/avatar", content=b"not json",
                                         headers={**ACTION, "content-type": "application/json"})
    assert response.status_code == 400


def test_picture_choice_needs_a_stored_sign_in_picture(rig: Rig) -> None:
    alice = rig.person("alice")
    response = rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=ACTION)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "picture_unavailable"
    assert avatars.get(alice, path=rig.db)["choice"] == "auto"


def test_the_response_names_a_gateway_path_never_the_providers_url(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    client = rig.as_(alice).client
    got = client.get("/me/api/profile/avatar").json()
    assert got["choice"] == "auto"
    assert got["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    saved = client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=ACTION).json()
    assert saved["choice"] == "picture"
    assert saved["picture_url"] == got["picture_url"]
    assert "example.test" not in str(saved)


def test_unknown_person_cannot_save(rig: Rig) -> None:
    response = rig.as_("usr_missing").client.put("/me/api/profile/avatar", json={"choice": "v1"}, headers=ACTION)
    assert response.status_code == 404
    with sqlite3.connect(rig.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_avatars").fetchone()[0] == 0


# --- the claim --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("claim,kept", [
    ("https://lh3.example.test/a/photo.png", True),
    ("https://example.test/p?x=1&y=2#frag", True),
    ("https://example.test/" + "a" * (2048 - len("https://example.test/")), True),   # exactly 2048
    ("https://example.test/" + "a" * (2049 - len("https://example.test/")), False),  # 2049
    ("http://example.test/p.png", False),
    ("HTTP://example.test/p.png", False),
    ("//example.test/p.png", False),
    ("/relative/p.png", False),
    ("data:image/png;base64,AAAA", False),
    ("javascript:alert(1)", False),
    ("file:///etc/passwd", False),
    ("ftp://example.test/p.png", False),
    ("https://user:pass@example.test/p.png", False),
    ("https://user@example.test/p.png", False),
    ("https://example.test/with space.png", False),
    ("https://example.test/new\nline.png", False),
    ("https://example.test\\@evil.example/p.png", False),
    ("https://", False),
    ("https:///p.png", False),
    ("https://example.test:notaport/p.png", False),
    ("", False), (None, False), (7, False), (["https://example.test/p.png"], False),
])
def test_only_a_short_https_claim_is_kept(rig: Rig, claim, kept: bool) -> None:
    alice = rig.person("alice")
    avatars.record_sign_in_picture(alice, claim, path=rig.db)
    assert avatars.get(alice, path=rig.db)["picture_url"] == (claim if kept else None)


def test_a_missing_claim_keeps_what_is_stored_and_a_bad_claim_clears_it(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    client = rig.as_(alice).client
    assert client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=ACTION).status_code == 200
    avatars.record_sign_in_picture(alice, None, path=rig.db)
    assert avatars.get(alice, path=rig.db)["picture_url"] == PICTURE
    avatars.record_sign_in_picture(alice, "http://insecure.example/p.png", path=rig.db)
    stored = avatars.get(alice, path=rig.db)
    assert stored["picture_url"] is None
    assert stored["choice"] == "auto"  # the picture is gone, so the choice falls back
    assert client.get("/me/api/profile/avatar").json()["picture_url"] is None


def test_a_new_picture_url_does_not_change_the_choice(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=ACTION)
    avatars.record_sign_in_picture(alice, "https://other.example.test/new.png", path=rig.db)
    stored = avatars.get(alice, path=rig.db)
    assert stored["choice"] == "picture"
    assert stored["picture_url"] == "https://other.example.test/new.png"


# --- the schema --------------------------------------------------------------------------------------------------

def test_schema_is_idempotent_and_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.example", "a", email="a@example.test", path=db)
    avatars.record_sign_in_picture(user["id"], PICTURE, path=db)
    avatars.set_choice(user["id"], "v5", path=db)
    before = admin_store.schema_version(db)
    admin_store.init_db(db)
    admin_store.init_db(db)
    assert admin_store.schema_version(db) == before
    stored = avatars.get(user["id"], path=db)
    assert (stored["choice"], stored["picture_url"]) == ("v5", PICTURE)


def test_an_older_database_without_the_table_gets_it_and_every_person_is_auto(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.example", "a", email="a@example.test", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE user_avatars")
    admin_store.init_db(db)
    assert avatars.get(user["id"], path=db) == {"choice": "auto", "picture_url": None, "picture_set_at": None}


def test_the_table_refuses_a_word_or_url_the_code_never_writes(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.example", "a", email="a@example.test", path=db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO user_avatars(user_id, choice, updated_at) VALUES(?,?,0)", (user["id"], "v9"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO user_avatars(user_id, picture_url, updated_at) VALUES(?,?,0)",
                         (user["id"], "http://insecure.example/p.png"))


def test_the_picture_url_is_not_on_the_users_row_the_people_list_returns(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.example", "a", email="a@example.test", path=db)
    avatars.record_sign_in_picture(user["id"], PICTURE, path=db)
    assert PICTURE not in str(admin_store.list_users(path=db))
    assert PICTURE not in str(admin_store.get_user(user["id"], path=db))


def test_removing_a_person_deletes_their_avatar_row_and_the_provider_url(tmp_path: Path) -> None:
    from gateway import accounts
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    owner = admin_store.upsert_oidc_user("https://idp.example", "owner", email="o@example.test", path=db)
    member = admin_store.upsert_oidc_user("https://idp.example", "m", email="m@example.test", path=db)
    avatars.record_sign_in_picture(member["id"], PICTURE, path=db)
    avatars.set_choice(member["id"], "picture", path=db)
    accounts.change_person(member["id"], owner["id"], remove=True, path=db)
    assert avatars.get(member["id"], path=db) == {"choice": "auto", "picture_url": None, "picture_set_at": None}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_avatars WHERE user_id=?", (member["id"],)).fetchone()[0] == 0


# --- serving the picture -----------------------------------------------------------------------------------------

def test_the_picture_is_fetched_on_the_server_and_served_from_the_gateway(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    response = rig.as_(alice).client.get("/me/api/profile/avatar/picture")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"].startswith("private, max-age=")
    assert "set-cookie" not in response.headers
    assert rig.fetches == [PICTURE]


def test_the_picture_is_kept_so_the_provider_is_asked_once(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    client = rig.as_(alice).client
    first = client.get("/me/api/profile/avatar/picture")
    second = client.get("/me/api/profile/avatar/picture")
    assert first.content == second.content == PNG
    assert rig.fetches == [PICTURE]
    revalidated = client.get("/me/api/profile/avatar/picture", headers={"if-none-match": first.headers["etag"]})
    assert revalidated.status_code == 304


@pytest.mark.parametrize("data,declared,served", [
    (PNG, "image/png", "image/png"), (JPEG, "image/jpeg", "image/jpeg"), (JPEG, "image/jpg", "image/jpeg"),
    (WEBP, "image/webp", "image/webp"), (GIF, "image/gif", "image/gif"),
    (PNG, "image/png; charset=binary", "image/png"), (PNG, "IMAGE/PNG", "image/png"),
])
def test_png_jpeg_webp_and_gif_are_served(tmp_path: Path, data, declared, served) -> None:
    rig = Rig(tmp_path, fetched=lambda url: media_fetch.FetchedMedia(data, "p", declared))
    alice = rig.person("alice", picture=PICTURE)
    response = rig.as_(alice).client.get("/me/api/profile/avatar/picture")
    assert response.status_code == 200
    assert response.headers["content-type"] == served


@pytest.mark.parametrize("data,declared", [
    (b"<html><script>alert(1)</script></html>", "text/html"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml"),
    (b"<html></html>", "image/png"),                       # says image, is a page
    (PNG, "text/html"),                                    # is an image, says page
    (PNG, "application/octet-stream"), (PNG, ""), (JPEG, "image/png"),
    (PNG, "image/bmp"), (b"BM" + b"\x00" * 40, "image/bmp"),
    (PNG + b"\x00" * (1024 * 1024), "image/png"),          # more than 1 MB
])
def test_anything_else_is_refused_and_the_page_shows_the_generated_avatar(tmp_path: Path, data, declared) -> None:
    rig = Rig(tmp_path, fetched=lambda url: media_fetch.FetchedMedia(data, "p", declared))
    alice = rig.person("alice", picture=PICTURE)
    response = rig.as_(alice).client.get("/me/api/profile/avatar/picture")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "avatar_picture_unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_exactly_one_megabyte_is_the_limit(tmp_path: Path) -> None:
    ok = PNG + b"\x00" * (1024 * 1024 - len(PNG))
    assert len(ok) == 1024 * 1024
    rig = Rig(tmp_path, fetched=lambda url: media_fetch.FetchedMedia(ok, "p", "image/png"))
    alice = rig.person("alice", picture=PICTURE)
    assert rig.as_(alice).client.get("/me/api/profile/avatar/picture").status_code == 200


def test_a_failed_fetch_is_404_and_is_not_retried_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fetched=lambda url: media_fetch.MediaFetchError("blocked network address"))
    alice = rig.person("alice", picture=PICTURE)
    client = rig.as_(alice).client
    assert client.get("/me/api/profile/avatar/picture").status_code == 404
    assert client.get("/me/api/profile/avatar/picture").status_code == 404
    assert rig.fetches == [PICTURE]  # the failure is remembered for a few minutes


def test_a_timeout_is_404_too(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fetched=lambda url: TimeoutError())
    alice = rig.person("alice", picture=PICTURE)
    assert rig.as_(alice).client.get("/me/api/profile/avatar/picture").status_code == 404


def test_no_stored_picture_is_404(rig: Rig) -> None:
    alice = rig.person("alice")
    assert rig.as_(alice).client.get("/me/api/profile/avatar/picture").status_code == 404
    assert rig.fetches == []


def test_a_member_cannot_fetch_another_persons_picture(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    bob = rig.person("bob")
    avatars.set_choice(alice, "picture", path=rig.db)
    response = rig.as_(bob).client.get("/me/api/profile/avatar/picture", params={"person": alice})
    assert response.status_code == 404
    assert rig.fetches == []
    # Also not through the plural list.
    assert alice not in rig.as_(bob).client.get("/me/api/profile/avatars").json()["people"]


def test_an_admin_sees_the_picture_only_when_the_person_chose_it(rig: Rig) -> None:
    alice = rig.person("alice", picture=PICTURE)
    owner = rig.person("owner")
    _make_admin(rig, owner)
    client = rig.as_(owner).client
    assert client.get("/me/api/profile/avatar/picture", params={"person": alice}).status_code == 404
    listed = client.get("/me/api/profile/avatars").json()["people"]
    assert listed[alice]["picture_url"] is None
    avatars.set_choice(alice, "picture", path=rig.db)
    shown = client.get("/me/api/profile/avatar/picture", params={"person": alice})
    assert shown.status_code == 200 and shown.content == PNG
    listed = client.get("/me/api/profile/avatars").json()["people"]
    assert listed[alice]["choice"] == "picture"
    assert listed[alice]["picture_url"].startswith(f"/me/api/profile/avatar/picture?person={alice}&v=")


def _make_admin(rig: Rig, person_id: str) -> None:
    admin_store.claim_first_owner(person_id, granted_by="test", path=rig.db)


def test_a_member_sees_only_their_own_choice_in_the_list(rig: Rig) -> None:
    alice = rig.person("alice")
    bob = rig.person("bob")
    avatars.set_choice(bob, "v4", path=rig.db)
    avatars.set_choice(alice, "v2", path=rig.db)
    people = rig.as_(alice).client.get("/me/api/profile/avatars").json()["people"]
    assert set(people) == {alice}
    assert people[alice]["choice"] == "v2"


def test_the_provider_request_carries_no_cookie_or_credential(tmp_path: Path) -> None:
    """The real fetcher, with the network replaced: only the checked headers go out."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(PNG))

    transport = HandlerTransport(handler)

    async def fetcher(url: str) -> media_fetch.FetchedMedia:
        return await media_fetch.safe_fetch_media(
            url, limit=1024 * 1024, max_redirects=2, schemes=frozenset({"https"}), transport=transport)

    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    who: dict[str, str] = {}
    app = FastAPI()
    app.include_router(create_me_avatar_router(
        session_provider=_session_for(who), mutation_validator=_validator, admin_db_path=db,
        picture_fetcher=fetcher, cache=PictureCache()))
    user = admin_store.upsert_oidc_user("https://idp.example", "a", email="a@example.test", path=db)
    who["subject"] = user["id"]
    avatars.record_sign_in_picture(user["id"], "https://93.184.216.34/photo.png", path=db)
    client = TestClient(app, cookies={"azizam_session": "secret-session", "other": "x"})
    response = client.get("/me/api/profile/avatar/picture",
                          headers={"authorization": "Bearer opk_secret", "x-azizam-csrf": "tok",
                                   "cookie": "azizam_session=secret-session", "referer": "https://ai.example/"})
    assert response.status_code == 200
    assert len(seen) == 1
    sent = {key.lower() for key in seen[0].headers}
    assert not sent & {"cookie", "authorization", "x-azizam-csrf", "x-azizam-user-action", "referer", "origin",
                       "proxy-authorization", "x-forwarded-for"}
    assert str(seen[0].url).startswith("https://93.184.216.34/")


@pytest.mark.parametrize("url", ["http://93.184.216.34/p.png", "https://127.0.0.1/p.png", "https://10.0.0.5/p.png",
                                 "https://169.254.169.254/latest/meta-data"])
def test_the_real_fetcher_refuses_http_and_private_addresses(tmp_path: Path, url: str) -> None:
    from gateway.me_avatar import fetch_picture
    with pytest.raises(media_fetch.MediaFetchError):
        asyncio.run(fetch_picture(url))


def test_signature_sniffing() -> None:
    assert image_type(PNG) == "image/png" and image_type(JPEG) == "image/jpeg"
    assert image_type(GIF) == "image/gif" and image_type(WEBP) == "image/webp"
    assert image_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert image_type(b"<svg") is None and image_type(b"") is None


def test_the_cache_is_bounded_and_expires() -> None:
    now = [0.0]
    cache = PictureCache(clock=lambda: now[0])
    cache.put(("a", "u"), b"x" * 10, "image/png")
    assert cache.get(("a", "u")) is not None
    now[0] += 6 * 3600 + 1
    assert cache.get(("a", "u")) is None
    for index in range(300):
        cache.put((str(index), "u"), b"x", "image/png")
    assert len(cache._items) <= 256
