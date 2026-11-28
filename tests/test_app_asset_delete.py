"""DELETE /v1/assets/{id}: an App key deletes an asset its owner made, and only that."""

from __future__ import annotations

import io
import time

import proxy
from fastapi.testclient import TestClient
from gateway import admin_store, media_jobs, prompt_memory
from tests.support.access import auth_identity


def _auth(app_id, person_id, key):
    return proxy.AuthContext(identity=auth_identity(name="Try", key_hash=key, app_id=app_id, person_id=person_id))


def _png():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, format="PNG")
    return out.getvalue()


class _World:
    def __init__(self, tmp_path, monkeypatch):
        self.admin, self.jobs, cache = tmp_path / "admin.db", tmp_path / "jobs.db", tmp_path / "cache"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        self.maker = admin_store.upsert_oidc_user("test", "maker", path=self.admin)
        self.other = admin_store.upsert_oidc_user("test", "other", path=self.admin)
        self.maker_app = admin_store.create_app("Maker", kind="personal", owner_user_id=self.maker["id"], access_plan="person", path=self.admin)
        self.other_app = admin_store.create_app("Other", kind="personal", owner_user_id=self.other["id"], access_plan="person", path=self.admin)
        self.maker_auth = _auth(self.maker_app["id"], self.maker["id"], "sha256:maker")
        self.other_auth = _auth(self.other_app["id"], self.other["id"], "sha256:other")
        monkeypatch.setattr(proxy.ADMIN_STORE, "path", self.admin)
        monkeypatch.setattr(media_jobs, "DB_PATH", self.jobs)
        monkeypatch.setattr(prompt_memory, "DB_PATH", self.jobs)
        monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
        monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-signing-secret")
        self.monkeypatch = monkeypatch
        self.client = TestClient(proxy.app)

    def make_asset(self) -> str:
        asset_id = proxy.store_asset_bytes(_png(), kind="img", suffix=".png",
                                           owner_key_hash=proxy._asset_owner_key(self.maker_auth))
        prompt_memory.put(asset_id, self.maker["id"], "kept prompt", kept_until=None, admin_path=self.admin)
        return asset_id

    def delete(self, asset_id, auth):
        self.monkeypatch.setattr(proxy, "authenticate_request", lambda *_a, _auth=auth, **_kw: _auth)
        return self.client.delete(f"/v1/assets/{asset_id}")

    def content_status(self, asset_id) -> int:
        expires = int(time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires, "none")
        return self.client.get(f"/v1/assets/{asset_id}/content?expires={expires}&md=none&sig={sig}").status_code

    def audit(self):
        with admin_store._conn(self.admin) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_events WHERE action='media.asset.delete'")]


def test_owner_app_deletes_its_asset(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    path = proxy.asset_cache_path(asset_id)
    assert path.exists() and world.content_status(asset_id) == 200
    response = world.delete(asset_id, world.maker_auth)
    assert response.status_code == 200
    assert response.json() == {"id": asset_id, "object": "gateway.asset", "deleted": True, "physical_removed": True}
    assert not path.exists()
    assert world.content_status(asset_id) == 404
    assert prompt_memory.get(asset_id, world.maker["id"]) is None
    (row,) = world.audit()
    assert row["actor_kind"] == "app" and row["actor_id"] == world.maker_app["id"]
    assert row["target_id"] == asset_id


def test_repeat_delete_answers_not_found(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    assert world.delete(asset_id, world.maker_auth).status_code == 200
    again = world.delete(asset_id, world.maker_auth)
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "asset_not_found"
    assert len(world.audit()) == 1


def test_another_apps_key_cannot_delete_and_cannot_tell_it_exists(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    path = proxy.asset_cache_path(asset_id)
    refused = world.delete(asset_id, world.other_auth)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "asset_not_found"
    missing_id = "img_" + "0" * 64
    unknown = world.delete(missing_id, world.other_auth)
    assert unknown.status_code == 404
    def shape(body, asset):
        return {**body["error"], "details": {**body["error"]["details"], "asset_id": None}, "message": body["error"]["message"].replace(asset, "X")}
    assert shape(refused.json(), asset_id) == shape(unknown.json(), missing_id)
    assert world.delete("not-an-asset-id", world.other_auth).status_code == 404
    assert path.exists() and prompt_memory.get(asset_id, world.maker["id"]) is not None
    assert world.audit() == []


def test_signed_content_url_is_no_right_to_delete(tmp_path, monkeypatch):
    # The signed URL is a bearer capability to read. Without a key of the owner, DELETE does nothing.
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    assert world.content_status(asset_id) == 200
    keyless = proxy.AuthContext(identity=None)
    assert world.delete(asset_id, keyless).status_code == 404
    assert proxy.asset_cache_path(asset_id).exists()


def test_asset_with_no_recorded_owner_is_not_deleted(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = proxy.store_asset_bytes(_png(), kind="img", suffix=".png", owner_key_hash=None)
    assert world.delete(asset_id, world.maker_auth).status_code == 404
    assert proxy.asset_cache_path(asset_id).exists()


def _share(world, asset_id):
    """Add the other App's key and person as a second owner and maker of the asset."""
    proxy._record_asset_owner(asset_id, proxy._asset_owner_key(world.other_auth))
    metadata = proxy._read_asset_metadata(asset_id)
    metadata["maker_person_ids"] = sorted({*metadata.get("maker_person_ids", []), world.other["id"]})
    proxy._write_asset_metadata(asset_id, metadata)
    prompt_memory.put(asset_id, world.other["id"], "other prompt", kept_until=None, admin_path=world.admin)


def test_shared_asset_delete_releases_only_the_callers_ownership(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    _share(world, asset_id)
    path = proxy.asset_cache_path(asset_id)
    maker_key, other_key = proxy._asset_owner_key(world.maker_auth), proxy._asset_owner_key(world.other_auth)

    response = world.delete(asset_id, world.maker_auth)
    assert response.status_code == 200
    assert response.json()["physical_removed"] is False
    assert path.exists()
    metadata = proxy._read_asset_metadata(asset_id)
    assert maker_key not in metadata["owner_key_hashes"] and other_key in metadata["owner_key_hashes"]
    assert metadata["maker_person_ids"] == [world.other["id"]]
    assert prompt_memory.get(asset_id, world.maker["id"]) is None
    assert prompt_memory.get(asset_id, world.other["id"]) is not None
    assert proxy._asset_access_allowed(asset_id, world.other_auth)
    assert not proxy._asset_access_allowed(asset_id, world.maker_auth)

    again = world.delete(asset_id, world.maker_auth)
    assert again.status_code == 404 and again.json()["error"]["code"] == "asset_not_found"
    assert len(world.audit()) == 1


def test_last_owner_delete_removes_the_file_after_a_shared_delete(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    _share(world, asset_id)
    assert world.delete(asset_id, world.maker_auth).json()["physical_removed"] is False
    last = world.delete(asset_id, world.other_auth)
    assert last.status_code == 200 and last.json()["physical_removed"] is True
    assert not proxy.asset_cache_path(asset_id).exists()
    assert proxy._read_asset_metadata(asset_id) == {}


def test_asset_owned_only_by_the_callers_own_keys_is_removed(tmp_path, monkeypatch):
    # Two owner keys, both the caller's (its principal and its credential), and the caller is the
    # only maker: nobody else holds the file, so the delete removes it instead of keeping it.
    world = _World(tmp_path, monkeypatch)
    asset_id = world.make_asset()
    own_keys = proxy._asset_read_owner_keys(world.maker_auth)
    assert len(own_keys) >= 2
    metadata = proxy._read_asset_metadata(asset_id)
    metadata["owner_key_hashes"] = sorted(own_keys[:2])
    proxy._write_asset_metadata(asset_id, metadata)
    response = world.delete(asset_id, world.maker_auth)
    assert response.status_code == 200 and response.json()["physical_removed"] is True
    assert not proxy.asset_cache_path(asset_id).exists()
    assert world.delete(asset_id, world.maker_auth).status_code == 404
    assert len(world.audit()) == 1
