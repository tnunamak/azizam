"""Browser media is usable only during its sign-in; App links keep their contract."""
import hashlib
import importlib.util
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from starlette.requests import Request

import proxy
from gateway import accounts, browser_sessions, thumbnails, user_runtime
from gateway.owner_openapi import walk_routes
from tests.test_library_perf import _png
from tests.test_session_lifecycle import cookie_session, csrf, owner_client, put_cookie, forge, set_policy
from tests.test_signin_who_can_enter import OWNER, SIGNING_KEY, gateway  # noqa: F401
from gateway.admin_oidc import encode_session_cookie

ASSET = "img_" + "a" * 64


@pytest.fixture
def media(gateway, monkeypatch, tmp_path):
    cache = tmp_path / "images"
    cache.mkdir()
    (cache / ("a" * 64 + ".png")).write_bytes(_png(64, 64))
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"asset-test-signing-key")
    return gateway


def mint(client, mode="none", *, key=False, image=False):
    headers = [(b"host", b"ai.example")]
    if client.cookies:
        headers.append((b"cookie", ("opx_admin_session=" + client.cookies.get("opx_admin_session")).encode()))
    if key:
        headers.append((b"authorization", ("Bearer " + (key if isinstance(key, str) else "test-app-credential")).encode()))
    request = Request({"type": "http", "scheme": "https", "server": ("ai.example", 443),
                       "headers": headers, "path": "/me/api/media", "query_string": b""})
    builder = proxy.build_signed_image_url if image else proxy.build_signed_asset_url
    return builder(request, ASSET, mode)


@pytest.mark.parametrize("mode,image", [("none", False), ("none", True), (thumbnails.THUMB_MODE, False)])
def test_signed_asset_url_dies_with_its_session_family(media, mode, image):
    client = owner_client(media)
    url = mint(client, mode, image=image)
    anonymous = media.client()
    response = anonymous.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"].startswith('"') and not response.headers["etag"].startswith('W/')
    # A warm verdict and an ETag cannot authorize the first fetch after the end.
    assert client.post("/logout", headers=csrf(client), follow_redirects=False).status_code == 303
    ended = anonymous.get(url, headers={"if-none-match": response.headers["etag"]})
    assert ended.status_code == 403


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
def test_key_minted_asset_urls_are_unaffected(media, mode):
    client = owner_client(media)
    url = mint(client, mode, key=True)
    query = parse_qs(urlsplit(url).query)
    assert "family" not in query and "subject" not in query
    assert query["sig"][0] == proxy._sign_asset_token(ASSET, int(query["expires"][0]), mode)
    before = media.client().get(url)
    assert before.status_code == 200
    assert before.headers["cache-control"] == "private, max-age=3600"
    browser_sessions.end_everywhere(cookie_session(client), path=media.path)
    after = media.client().get(url)
    assert after.status_code == 200 and after.content == before.content
    if mode == thumbnails.THUMB_MODE:
        etag = before.headers["etag"]
        assert media.client().get(url, headers={"if-none-match": etag}).status_code == 304
        assert media.client().get(url, headers={"if-none-match": "W/" + etag}).status_code == 200


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
def test_browser_asset_revalidation_and_ranges(media, mode):
    client = owner_client(media)
    url = mint(client, mode)
    response = client.get(url)
    etag = response.headers["etag"]
    for validator in (etag, f'"unrelated", W/{etag}', "*"):
        again = client.get(url, headers={"if-none-match": validator})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["cache-control"] == "private, no-cache"
        assert again.headers["etag"] == etag
    if mode == "none":
        partial = client.get(url, headers={"range": "bytes=0-9"})
        assert partial.status_code == 206 and partial.content == response.content[:10]
        assert partial.headers["etag"] == etag


def test_sign_out_everywhere_from_device_a_kills_device_b_asset_urls(media):
    a, b = owner_client(media), owner_client(media)
    urls = [mint(client, thumbnails.THUMB_MODE) for client in (a, b)]
    assert all(media.client().get(url).status_code == 200 for url in urls)
    ended = a.post("/auth/api/sign-out-everywhere", headers=csrf(a))
    assert ended.status_code == 200
    assert ended.headers["clear-site-data"] == '"cache"'
    assert ended.headers["cache-control"] == "no-store"
    assert all(media.client().get(url).status_code == 403 for url in urls)


def test_inflight_mint_cannot_drop_an_ended_session_binding(media):
    client = owner_client(media)
    session = cookie_session(client)
    browser_sessions.end(session, path=media.path)
    # The request still carries its original cookie after another request ended it.
    url = mint(client)
    assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
    assert media.client().get(url).status_code == 403


def test_unregistered_service_session_urls_end_with_their_session(media):
    client = media.client()
    session = browser_sessions.issue({"sub": "service-account", "email": "service@example.test"},
                                     role="owner", cookie=media.config.cookie, path=media.path,
                                     register=False, renewable=False)
    put_cookie(client, encode_session_cookie(session, signing_key=SIGNING_KEY))
    url = mint(client)
    assert int(parse_qs(urlsplit(url).query)["expires"][0]) <= session.expires_at
    assert media.client().get(url).status_code == 200
    browser_sessions.end(session, path=media.path)
    assert media.client().get(url).status_code == 403


def test_no_store_includes_replaced_and_unhandled_server_errors():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.browser_cache import BrowserCacheMiddleware
    from gateway.egress import EgressGate

    app = FastAPI()

    @app.get("/me/api/fail")
    def fail():
        raise RuntimeError("synthetic handler failure")

    app.add_api_route("/admin/api/fail", fail)
    app.add_api_route("/auth/api/fail", fail)
    app.add_middleware(EgressGate)
    app.add_middleware(BrowserCacheMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        for prefix in ("/me/api", "/admin/api", "/auth/api"):
            response = client.get(prefix + "/fail")
            assert response.status_code == 500
            assert response.headers["cache-control"] == "no-store"


def test_rotation_keeps_family_urls_and_invalidates_the_memo(media):
    client = owner_client(media)
    old = cookie_session(client)
    url = mint(client)
    assert client.get(url).status_code == 200
    renewed = browser_sessions.confirm(old, path=media.path)
    # The next request must resolve the new live id, not use the warmed answer.
    assert not accounts._asset_families
    accounts.session_verdict(renewed.subject, renewed.session_id, renewed.family_id,
                             path=media.path, note_use=True)
    # After the retired-cookie grace period, the asset still follows the family.
    with accounts.connection(media.path) as conn:
        conn.execute("UPDATE account_session_lineage SET retired_at=? WHERE session_id=?",
                     (int(time.time()) - 120, old.session_id))
    assert media.client().get(url).status_code == 200
    browser_sessions.end(renewed, path=media.path)
    assert media.client().get(url).status_code == 403


def test_family_memo_uses_the_session_verdict_and_lasts_at_most_five_seconds(media, monkeypatch):
    client = owner_client(media)
    session = cookie_session(client)
    url = mint(client)
    calls = []
    original = accounts.session_verdict_in

    def verdict(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "session_verdict_in", verdict)
    clock = [100.0]
    monkeypatch.setattr(accounts, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    assert media.client().get(url).status_code == 200
    count = len(calls)
    clock[0] = 104.99
    assert media.client().get(url).status_code == 200
    assert len(calls) == count
    clock[0] = 105.0
    assert media.client().get(url).status_code == 200
    assert len(calls) > count
    accounts.revoke_person_sessions(session.subject, path=media.path)
    assert media.client().get(url).status_code == 403


@pytest.mark.parametrize("change", ["family", "subject", "remove_binding", "session", "remove_timing"])
def test_family_binding_cannot_be_changed_or_removed(media, change):
    client = owner_client(media)
    parts = urlsplit(mint(client))
    query = parse_qs(parts.query)
    if change == "remove_binding":
        query.pop("family")
        query.pop("subject")
    elif change == "remove_timing":
        query.pop("session")
    else:
        query[change] = ["another-session"]
    url = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    assert media.client().get(url).status_code == 403


@pytest.mark.parametrize("state", ["disabled_at", "removed_at", "expired"])
def test_family_urls_obey_account_state_and_session_expiry(media, state):
    client = owner_client(media)
    url = mint(client)
    session = cookie_session(client)
    assert media.client().get(url).status_code == 200
    with accounts.connection(media.path) as conn:
        accounts.ensure_access_row(conn, session.subject)
        if state == "expired":
            conn.execute("UPDATE account_sessions SET expires_at=0 WHERE session_id=?", (session.session_id,))
        else:
            conn.execute(f"UPDATE account_access SET {state}=1 WHERE user_id=?", (session.subject,))
    assert media.client().get(url).status_code == 403


def test_logout_sends_clear_site_data_cache_and_no_store(media):
    client = owner_client(media)
    for headers, status in (({}, 403), (csrf(client), 303)):
        response = client.post("/logout", headers=headers, follow_redirects=False)
        assert response.status_code == status
        assert response.headers["clear-site-data"] == '"cache"'
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/logout").headers["cache-control"] == "no-store"


def test_every_me_admin_auth_api_response_is_no_store(media):
    client = media.client()
    routes = [(path, route) for path, route in walk_routes(proxy.app.routes)
              if path.startswith(("/me/api/", "/admin/api/", "/auth/api/")) and getattr(route, "methods", None)]
    assert len(routes) > 100
    for path, route in routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = client.request(method, path, follow_redirects=False)
            assert response.headers.get("cache-control") == "no-store", (method, path, response.status_code)
    for prefix in ("/me/api", "/admin/api", "/auth/api"):
        assert client.get(prefix + "/does-not-exist").headers["cache-control"] == "no-store"


def test_session_view_id_is_public_stable_across_rotation_and_changes_on_new_signin(media):
    client = owner_client(media)
    old = cookie_session(client)
    view = client.get("/me/api/session").json()["session_view_id"]
    assert len(view) == 64 and view not in {old.session_id, old.family_id}
    renewed = browser_sessions.confirm(old, path=media.path)
    put_cookie(client, encode_session_cookie(renewed, signing_key=SIGNING_KEY))
    assert client.get("/me/api/session").json()["session_view_id"] == view
    assert owner_client(media).get("/me/api/session").json()["session_view_id"] != view
    assert "session_view_id" not in media.client().get("/me/api/session").json()


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
@pytest.mark.parametrize("setting,age", [("absolute_lifetime_days", {"issued_ago": 3 * 86400, "renewed_ago": 0}),
                                          ("idle_timeout_days", {"issued_ago": 3 * 86400, "renewed_ago": 2 * 86400})])
def test_current_lifetime_policy_rejects_previously_minted_urls(media, mode, setting, age):
    client = owner_client(media)
    forge(media, client, OWNER, **age)
    url = mint(client, mode)
    response = media.client().get(url)
    assert response.status_code == 200
    policy = {setting: 1}
    if setting == "absolute_lifetime_days":
        policy["idle_timeout_days"] = 1
    set_policy(media, **policy)
    # The family memo is warm; policy changes must apply on the very next fetch, even a 304.
    refused = media.client().get(url, headers={"if-none-match": response.headers["etag"]})
    assert refused.status_code == 403
    assert client.get("/me/api/session").json()["authenticated"] is False


def test_renewed_cookie_mints_urls_with_its_renewed_lifetime(media, monkeypatch):
    client = owner_client(media)
    forge(media, client, OWNER, issued_ago=10 * 86400, renewed_ago=8 * 86400)
    old = cookie_session(client)
    monkeypatch.setattr(proxy, "IMAGE_SIGNED_URL_TTL", 90 * 86400)
    old_url = mint(client)
    assert client.get("/me/api/session").json()["authenticated"] is True
    renewed = cookie_session(client)
    assert renewed.last_renewed > old.last_renewed
    url = mint(client)
    assert int(parse_qs(urlsplit(url).query)["expires"][0]) == int(renewed.expires_at)
    monkeypatch.setattr(time, "time", lambda: old.expires_at + 1)
    assert media.client().get(old_url).status_code == 403
    assert media.client().get(url).status_code == 200


def test_session_view_id_survives_module_restart_with_persistent_secret(media):
    client = owner_client(media)
    session = cookie_session(client)
    before = user_runtime.build_user_portal_snapshot(session, path=media.path)["session_view_id"]
    spec = importlib.util.spec_from_file_location("restarted_user_runtime", user_runtime.__file__)
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    assert restarted.build_user_portal_snapshot(session, path=media.path)["session_view_id"] == before
    other = cookie_session(owner_client(media))
    assert restarted.build_user_portal_snapshot(other, path=media.path)["session_view_id"] != before


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
@pytest.mark.parametrize("with_cookie", [False, True])
def test_try_key_urls_end_on_signout_but_app_urls_survive(media, mode, with_cookie):
    client = owner_client(media)
    session = cookie_session(client)
    key = user_runtime.ensure_try_session_key(session, path=media.path)["api_key"]
    url = mint(client if with_cookie else media.client(), mode, key=key)
    app_url = mint(client, mode, key=True)
    assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
    response = media.client().get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert client.post("/logout", headers=csrf(client), follow_redirects=False).status_code == 303
    assert media.client().get(url).status_code == 403
    assert media.client().get(app_url).status_code == 200


@pytest.mark.parametrize("validator,expected", [("matching", 206), ("stale", 200), ("weak", 200),
                                                ("Wed, 21 Oct 2015 07:28:00 GMT", 200)])
def test_browser_asset_if_range_requires_the_strong_current_validator(media, validator, expected):
    client = owner_client(media)
    url = mint(client)
    full = client.get(url)
    etag = full.headers["etag"]
    supplied = etag if validator == "matching" else "W/" + etag if validator == "weak" else validator
    response = client.get(url, headers={"range": "bytes=0-9", "if-range": supplied})
    assert response.status_code == expected
    assert response.content == (full.content[:10] if expected == 206 else full.content)
    assert response.headers["etag"] == etag


def test_session_etags_hash_only_variant_identity_not_delivered_bytes(media, monkeypatch):
    client = owner_client(media)
    urls = [mint(client, mode) for mode in ("none", thumbnails.THUMB_MODE)]
    responses = [client.get(url) for url in urls]
    delivered = {response.content for response in responses}
    original = hashlib.sha256
    def bounded_hash(data=b"", **kwargs):
        assert data not in delivered, "ETag must not hash delivered media"
        return original(data, **kwargs)
    monkeypatch.setattr(hashlib, "sha256", bounded_hash)
    for url, response in zip(urls, responses):
        again = client.get(url)
        assert again.headers["etag"] == response.headers["etag"]
        assert client.get(url, headers={"if-none-match": response.headers["etag"]}).status_code == 304
    assert responses[0].headers["etag"] != responses[1].headers["etag"]


@pytest.mark.parametrize("setting,age", [("absolute_lifetime_days", {"issued_ago": 3 * 86400, "renewed_ago": 0}),
                                          ("idle_timeout_days", {"issued_ago": 3 * 86400, "renewed_ago": 2 * 86400})])
def test_cookie_free_try_url_checks_its_minting_cookie_against_current_policy(media, setting, age):
    client = owner_client(media)
    forge(media, client, OWNER, **age)
    key = user_runtime.ensure_try_session_key(cookie_session(client), path=media.path)["api_key"]
    url = mint(media.client(), key=key)
    assert media.client().get(url).status_code == 200
    set_policy(media, **{setting: 1, "idle_timeout_days": 1})
    assert media.client().get(url).status_code == 403


def test_try_key_upgrade_keeps_binding_without_inventing_cookie_timestamps(media):
    from gateway import admin_runtime, admin_store
    client = owner_client(media)
    key = user_runtime.ensure_try_session_key(cookie_session(client), path=media.path)
    stored = admin_store.get_credential(key["id"], path=media.path)
    assert "session_timing_json" not in key
    assert "session_timing_json" not in admin_runtime._sanitize_credential(stored)
    with accounts.connection(media.path) as conn:
        conn.execute("ALTER TABLE credentials DROP COLUMN session_timing_json")
    admin_store.init_db(media.path)
    assert admin_store.get_credential(key["id"], path=media.path)["session_timing_json"] is None
    url = mint(media.client(), key=key["api_key"])
    assert parse_qs(urlsplit(url).query)["family"] == [cookie_session(client).family_id]
    assert media.client().get(url).status_code == 403
    # A matching signed cookie can still provide the real lifetime facts after upgrade.
    assert media.client().get(mint(client, key=key["api_key"])).status_code == 200


def test_try_identity_background_job_mints_family_bound_urls(media, monkeypatch, tmp_path):
    import asyncio
    from gateway import admin_runtime, media_jobs
    client = owner_client(media)
    session = cookie_session(client)
    key = user_runtime.ensure_try_session_key(session, path=media.path)["api_key"]
    auth = proxy.AuthContext(identity=admin_runtime.runtime_identity_for_app_credential_hash(
        proxy.hash_api_key(key), path=media.path))
    original = Request({"type": "http", "method": "POST", "scheme": "https", "server": ("ai.example", 443),
                        "headers": [(b"host", b"ai.example")], "path": "/v1/images/identity", "query_string": b"",
                        "auth_context": auth})
    shim = proxy._identity_request_shim(original)
    assert not proxy._resolve_client_api_key(shim) and not getattr(shim, "cookies", None)
    jobs = tmp_path / "jobs.db"
    media_jobs.init_db(jobs)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(proxy, "_media_job_admission_response", lambda *a, **kw: None)
    async def execute(*args):
        return SimpleNamespace(image=_png(64, 64))
    monkeypatch.setattr(proxy.app.state, "identity_execute", execute)
    monkeypatch.setattr(proxy.identity_api, "stored_scene_extras", lambda *a: {})
    job = media_jobs.MediaJob(intent="image.generate", workflow="test", backend="test", request_id="try-identity")
    media_jobs.create(job, path=jobs)
    asyncio.run(proxy._run_identity_job(job.job_id, shim, auth, SimpleNamespace(), "url"))
    finished = media_jobs.get(job.job_id, audience="client", path=jobs)
    assert finished["status"] == media_jobs.STATUS_SUCCEEDED, finished.get("error")
    urls = [finished["assets"][0][field] for field in ("asset_url", "url")]
    for url in urls:
        assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
        assert media.client().get(url).headers["cache-control"] == "private, no-cache"
    browser_sessions.end(session, path=media.path)
    assert all(media.client().get(url).status_code == 403 for url in urls)


def test_browser_etag_changes_after_an_in_place_asset_rewrite(media):
    client = owner_client(media)
    url = mint(client)
    response = client.get(url)
    path = proxy.asset_cache_path(ASSET)
    path.write_bytes(_png(80, 80))
    changed = client.get(url, headers={"if-none-match": response.headers["etag"]})
    assert changed.status_code == 200 and changed.content != response.content
    assert changed.headers["etag"] != response.headers["etag"]
    assert client.get(url, headers={"range": "bytes=0-9", "if-range": response.headers["etag"]}).status_code == 200



def test_resumed_try_image_job_keeps_binding_without_the_original_request(media, monkeypatch, tmp_path):
    import asyncio
    from gateway import admin_runtime, media_jobs, prompt_memory
    from starlette.responses import JSONResponse
    client = owner_client(media)
    session = cookie_session(client)
    key = user_runtime.ensure_try_session_key(session, path=media.path)["api_key"]
    auth = proxy.AuthContext(identity=admin_runtime.runtime_identity_for_app_credential_hash(
        proxy.hash_api_key(key), path=media.path))
    request = Request({"type": "http", "method": "POST", "scheme": "https", "server": ("ai.example", 443),
                       "headers": [(b"host", b"ai.example"), (b"authorization", ("Bearer " + key).encode()),
                                   (b"cookie", ("opx_admin_session=" + client.cookies.get("opx_admin_session")).encode())],
                       "path": "/gateway/v1/media/jobs", "query_string": b"",
                       "auth_context": auth})
    jobs = tmp_path / "resumed-jobs.db"
    media_jobs.init_db(jobs)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_media_job_admission_response", lambda *a, **kw: None)
    job = media_jobs.MediaJob(intent="image.generate", workflow="test", backend="test", request_id="try-resume")
    media_jobs.create(job, path=jobs)
    proxy._persist_image_media_job_payload(job.job_id, body={"prompt": "a fox"}, request=request, auth_context=auth)
    # Recreate only the serialized payload, as after a process restart: no scope, raw key or cookie.
    payload = json.loads(json.dumps(proxy._load_image_media_job_payload(job.job_id)))
    assert key not in json.dumps(payload)
    assert client.cookies.get("opx_admin_session") not in json.dumps(payload)
    monkeypatch.setattr(proxy, "_load_image_media_job_payload", lambda _: payload)
    async def backend(shim, _auth, **kwargs):
        return JSONResponse({"data": [{"asset_id": ASSET, "asset_url": proxy.build_signed_asset_url(shim, ASSET),
                                        "url": proxy.build_signed_image_url(shim, ASSET)}]})
    monkeypatch.setattr(proxy, "proxy_image_request", backend)
    asyncio.run(proxy._run_image_media_job(job.job_id))
    finished = media_jobs.get(job.job_id, audience="client", path=jobs)
    assert finished["status"] == media_jobs.STATUS_SUCCEEDED, finished.get("error")
    url = finished["assets"][0]["asset_url"]
    assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
    assert media.client().get(url).status_code == 200
    browser_sessions.end(session, path=media.path)
    assert media.client().get(url).status_code == 403


def test_deleting_a_try_credential_in_flight_cannot_drop_its_asset_binding(media):
    from gateway import admin_runtime
    client = owner_client(media)
    session = cookie_session(client)
    key = user_runtime.ensure_try_session_key(session, path=media.path)
    auth = proxy.AuthContext(identity=admin_runtime.runtime_identity_for_app_credential_hash(
        proxy.hash_api_key(key["api_key"]), path=media.path))
    shim = proxy._JsonBodyRequestShim(body={}, headers={"host": "ai.example"}, scheme="https", netloc="ai.example",
                                      scope={"auth_context": auth})
    browser_sessions.end(session, path=media.path)
    with accounts.connection(media.path) as conn:
        conn.execute("DELETE FROM credentials WHERE id=?", (key["id"],))
    url = proxy.build_signed_asset_url(shim, ASSET)
    assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
    assert media.client().get(url).status_code == 403


def test_full_metadata_etag_changes_with_metadata_and_is_separate_from_clean_delivery(media, monkeypatch, tmp_path):
    from gateway import prompt_memory
    client = owner_client(media)
    session = cookie_session(client)
    monkeypatch.setattr(prompt_memory, "DB_PATH", tmp_path / "prompts.db")
    def keep(text):
        assert prompt_memory.put(ASSET, session.subject, text, kept_until=None, metadata={"prompt": text},
                                 admin_path=media.path)
    keep("red fox")
    full_url, clean_url = mint(client, "full"), mint(client)
    first = client.get(full_url)
    clean = client.get(clean_url)
    assert first.status_code == 200 and clean.status_code == 200
    assert first.content != clean.content and first.headers["etag"] != clean.headers["etag"]
    keep("blue fox")
    changed = client.get(full_url, headers={"if-none-match": first.headers["etag"]})
    assert changed.status_code == 200 and changed.content != first.content
    assert changed.headers["etag"] != first.headers["etag"]
    assert client.get(clean_url).headers["etag"] == clean.headers["etag"]
