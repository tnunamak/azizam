"""Owner visibility slice 1: every Library reader stays inside its maker's scope."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from gateway import admin_store, content_access, media_jobs, mcp_oauth, mcp_oauth_store
from gateway.runtime_auth import hash_api_key
from tests.support.app_keys import issue_app_key


SESSION_ACTORS = ("owner", "admin", "member_a", "member_b")
ACTORS = (*SESSION_ACTORS, "app_key", "mcp_grant")


def _new_media_case():
    from tests.test_me_media import MeMediaRouterTests

    return MeMediaRouterTests()


@pytest.fixture
def library_world():
    case = _new_media_case()
    case.setUp()

    admin = admin_store.upsert_oidc_user(
        "issuer", "admin-sub", email="admin@example.com", path=case.admin_db,
    )
    admin_store.assign_admin_role(
        role="admin", subject_kind="user", subject_id=admin["id"], path=case.admin_db,
    )
    owner_app = admin_store.create_app(
        "Owner", kind="personal", owner_user_id=case.owner["id"],
        access_plan="person", path=case.admin_db,
    )
    admin_app = admin_store.create_app(
        "Admin", kind="personal", owner_user_id=admin["id"],
        access_plan="person", path=case.admin_db,
    )
    owner_key = issue_app_key(case.admin_db, owner_app["id"])
    admin_key = issue_app_key(case.admin_db, admin_app["id"])
    oauth_store = mcp_oauth_store.Store(lambda: case.admin_db)
    mcp_client = oauth_store.put_client(
        client_id="slice-one-client", kind="registered", name="Slice one fixture",
        redirect_uris=["https://client.example/callback"],
        grant_types=["authorization_code"], auth_method="none", secret_hash=None, metadata={},
    )
    grant = oauth_store.create_grant(
        person_id=case.alice["id"], client=mcp_client,
        redirect_uri="https://client.example/callback", resource="http://testserver/mcp",
        scope=mcp_oauth.SCOPE, session=None,
    )
    mcp_token = mcp_oauth_store.new_secret(mcp_oauth.ACCESS_TOKEN_PREFIX)
    oauth_store.save_token(
        mcp_token, kind="access", grant_id=grant.id, client_id=mcp_client.client_id,
        scope=mcp_oauth.SCOPE, ttl=3600,
    )
    actors = {
        "owner": (case.owner, owner_app, hash_api_key(owner_key)),
        "admin": (admin, admin_app, hash_api_key(admin_key)),
        "member_a": (case.alice, case.app_a, "sha256:alice"),
        "member_b": (case.bob, case.app_b, "sha256:bob"),
    }
    assets: dict[str, str] = {}
    jobs: dict[str, str] = {}
    for name, (user, app, key_hash) in actors.items():
        # Use a stable, unique digest for each actor.
        asset_id = "img_" + {"owner": "1", "admin": "2", "member_a": "3", "member_b": "4"}[name] * 64
        request_id = f"req_{name}"
        case._write_asset(asset_id, [key_hash], prompt=f"private prompt for {name}", request_id=request_id)
        case._receipt(app["id"], request_id)
        case._job(request_id, asset_id)
        job_id = f"running_{name}"
        media_jobs.create(media_jobs.MediaJob(
            job_id=job_id, intent="image.generate", app_id=app["id"],
            status=media_jobs.STATUS_RUNNING, created_at=1_700_000_000,
            updated_at=1_700_000_001,
        ), path=case.jobs_db)
        assets[name] = asset_id
        jobs[name] = job_id

    case.current_user = case.alice
    yield {
        "case": case,
        "actors": actors,
        "assets": assets,
        "jobs": jobs,
        "app_key": issue_app_key(case.admin_db, case.app_a["id"]),
        "mcp_grant": mcp_token,
    }
    case.tearDown()


def request_as(world, actor: str, path: str):
    case = world["case"]
    if actor in SESSION_ACTORS:
        case.current_user = world["actors"][actor][0]
        return case.client.get(path)
    token = world[actor]
    return case.client.get(path, headers={"authorization": f"Bearer {token}"})


@pytest.mark.parametrize("actor", ACTORS)
def test_list_actor_state_matrix(library_world, actor):
    world = library_world
    response = request_as(world, actor, "/me/api/media")
    if actor not in SESSION_ACTORS:
        assert response.status_code == 401
        return

    assert response.status_code == 200, response.text
    assert [item["asset_id"] for item in response.json()["assets"]] == [world["assets"][actor]]
    body = json.dumps(response.json())
    for foreign_actor in set(SESSION_ACTORS) - {actor}:
        assert f"private prompt for {foreign_actor}" not in body


@pytest.mark.parametrize("actor", ACTORS)
def test_detail_actor_state_matrix(library_world, actor):
    world = library_world
    case = world["case"]
    own_asset = world["assets"].get(actor, world["assets"]["member_a"])
    if actor in SESSION_ACTORS:
        case.current_user = world["actors"][actor][0]
    headers = {} if actor in SESSION_ACTORS else {"authorization": f"Bearer {world[actor]}"}
    own = case.client.get(f"/me/api/media/{own_asset}", headers=headers)
    if actor not in SESSION_ACTORS:
        assert own.status_code == 401
        return

    assert own.status_code == 200, own.text
    assert own.json()["asset"]["asset_id"] == own_asset
    for foreign_actor in set(SESSION_ACTORS) - {actor}:
        response = case.client.get(f"/me/api/media/{world['assets'][foreign_actor]}")
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("actor", ACTORS)
def test_download_requires_access(library_world, actor):
    world = library_world
    case = world["case"]
    own_asset = world["assets"].get(actor, world["assets"]["member_a"])
    if actor in SESSION_ACTORS:
        case.current_user = world["actors"][actor][0]
    headers = {} if actor in SESSION_ACTORS else {"authorization": f"Bearer {world[actor]}"}
    own = case.client.get(f"/me/api/media/{own_asset}/download", headers=headers)
    if actor not in SESSION_ACTORS:
        assert own.status_code == 401
        return

    assert own.status_code == 200, own.text
    assert own.json()["asset_id"] == own_asset
    foreign_asset = world["assets"]["member_b" if actor != "member_b" else "member_a"]
    assert case.client.get(f"/me/api/media/{foreign_asset}/download").status_code == 404


@pytest.mark.parametrize("actor", ACTORS)
def test_job_status_actor_matrix(library_world, actor):
    world = library_world
    case = world["case"]
    if actor in SESSION_ACTORS:
        case.current_user = world["actors"][actor][0]
        with patch("gateway.me_media.decide_own", wraps=content_access.decide_own) as decide:
            response = case.client.get("/me/api/jobs?watch=1")
        assert response.status_code == 200, response.text
        assert [job["job_id"] for job in response.json()["jobs"]] == [world["jobs"][actor]]
        assert decide.call_count >= 1
        return

    response = case.client.get(
        "/me/api/jobs?watch=1",
        headers={"authorization": f"Bearer {world[actor]}"},
    )
    assert response.status_code == 401


def test_cache_only_vs_job_backed():
    case = _new_media_case()
    case.setUp()
    try:
        asset_id = "img_" + "a" * 64
        case._asset_path(asset_id).write_bytes(b"owned cache item")
        case._meta_path(asset_id).write_text(json.dumps({
            "asset_id": asset_id,
            "owner_key_hashes": ["sha256:alice"],
        }))
        with patch("gateway.me_media.decide_own", wraps=content_access.decide_own) as decide:
            response = case.client.get("/me/api/media")
        assert response.status_code == 200, response.text
        assert [item["asset_id"] for item in response.json()["assets"]] == [asset_id]
        assert any("sha256:alice" in call.args[1] for call in decide.call_args_list)
    finally:
        case.tearDown()


def test_query_filters_noninterference(library_world):
    world = library_world
    case = world["case"]
    case.current_user = case.alice
    response = case.client.get("/me/api/media", params={"q": "private prompt for member_b"})
    assert response.status_code == 200, response.text
    assert response.json()["assets"] == []
    baseline = case.client.get("/me/api/media").json()
    assert response.json()["models"] == baseline["models"]
    assert "member_b" not in json.dumps(response.json())


def test_local_network_scope_not_detail_escape(library_world):
    world = library_world
    case = world["case"]
    asset_id = "img_" + "f" * 64
    case._write_asset(asset_id, [], prompt="local-only prompt", request_id="req_local")
    case._receipt(None, "req_local", owner_key_hash="sha256:unattributed")
    case._job("req_local", asset_id)
    case.current_user = case.owner
    assert asset_id not in {item["asset_id"] for item in case.client.get("/me/api/media").json()["assets"]}
    assert case.client.get(f"/me/api/media/{asset_id}").status_code == 404


def test_library_removed_parameters_before_target_lookup(library_world):
    world = library_world
    case = world["case"]
    case.current_user = case.owner
    paths = (
        "/me/api/media",
        f"/me/api/media/{world['assets']['member_a']}",
        f"/me/api/media/{world['assets']['member_a']}/download",
    )
    queries = (
        "scope=", "scope=me&scope=everyone", "%73cope=everyone",
        "owner=", "owner=me&owner=everyone", "person=", "person=alice&person=all",
    )
    case.signed_asset_ids.clear()
    with patch("gateway.me_media._list_media", return_value={"assets": [], "models": []}) as list_media, patch(
        "gateway.me_media._find_asset", return_value=None,
    ) as find_asset:
        for path in paths:
            for query in queries:
                response = case.client.get(f"{path}?{query}")
                assert response.status_code == 400, response.text
                assert response.json()["error"]["code"] == "param_not_supported"
        list_media.assert_not_called()
        find_asset.assert_not_called()
    assert case.signed_asset_ids == []


def test_owner_cannot_mint_foreign_library_url(library_world):
    world = library_world
    case = world["case"]
    case.current_user = case.owner
    foreign = world["assets"]["member_a"]
    case.signed_asset_ids.clear()
    denied = case.client.get(f"/me/api/media/{foreign}/download")
    assert denied.status_code == 404
    assert foreign not in case.signed_asset_ids
    removed_link = case.client.get(f"/me/api/media/{foreign}/download?scope=everyone")
    assert removed_link.status_code == 400
    assert foreign not in case.signed_asset_ids


def test_owner_library_running_feed_is_own(library_world):
    world = library_world
    case = world["case"]
    case.current_user = case.owner
    response = case.client.get("/me/api/jobs?watch=1")
    assert response.status_code == 200, response.text
    assert [job["job_id"] for job in response.json()["jobs"]] == [world["jobs"]["owner"]]
    assert "private prompt for member_a" not in response.text
