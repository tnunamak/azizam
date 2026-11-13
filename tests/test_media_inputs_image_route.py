"""The image route's asset-id extension inputs on the media input resolver (S2.6).

The golden is the old ingestion itself: ``_resolve_cached_image_asset`` and ``asset_media_type`` stay in proxy.py, and
each case holds what the resolver path sends to the engine against what they send. A route that cannot match them stays
on the old ingestion; this one matches for every image type the store holds.
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
from types import SimpleNamespace

import pytest
from PIL import Image

import proxy
from tests.support.graph_service import GRAPH_SERVICE_ID
from gateway import directory_index, media_jobs
from tests.support.access import access, access_context, plan_for
from tests.test_image_extensions import CONTROLNET_EDIT_MANIFEST

NS = proxy.WORKFLOW_EXTENSION_NAMESPACE
OWNER_APP = {"app_id": "app_owner", "key_hash": "sha256:owner"}
OTHER_APP = {"app_id": "app_other", "key_hash": "sha256:other"}


def image_bytes(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (3, 2), (10, 20, 30)).save(out, format=fmt)
    return out.getvalue()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "assets")
    monkeypatch.setattr(media_jobs, "DB_PATH", tmp_path / "jobs.db")
    media_jobs.init_db(path=media_jobs.DB_PATH)
    monkeypatch.setattr(proxy, "_media_input_ledgers", {})
    directory_index.forget()
    registry = {"schema_version": 2, "services": {
        GRAPH_SERVICE_ID: {"id": GRAPH_SERVICE_ID, "config": {"base_url": "http://engine.test:1"}},
    }}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    uploads: list[tuple] = []

    async def upload(client, headers, content, *, filename, media_type="image/png"):
        uploads.append((filename, content, media_type))
        return "engine-name.png", None

    monkeypatch.setattr(proxy, "_upload_graph_image_bytes", upload)
    yield SimpleNamespace(registry=registry, uploads=uploads, cache=tmp_path / "assets")
    directory_index.forget()


def stored(data: bytes, owner: str = "app:app_owner") -> str:
    return proxy.store_image_bytes(data, owner_key_hash=owner, suffix={b"\x89": ".png", b"\xff": ".jpg", b"R": ".webp", b"G": ".gif"}[data[:1]])


def run_extension(asset_id: str, *, app: dict[str, str] = OWNER_APP, plan_or_access=None):
    auth = access_context(plan_or_access, app_id=app["app_id"], key_hash=app["key_hash"])
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"asset_id": asset_id}}}}
    return asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=None, request_body=body, manifest=CONTROLNET_EDIT_MANIFEST, client=object(), headers={}, receipt=None,
        auth_context=auth,
    ))


def lease_rows() -> int:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM media_input_lease").fetchone()[0]


def live_rows() -> int:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM media_input_lease WHERE released_at IS NULL").fetchone()[0]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_the_engine_receives_what_the_old_ingestion_sent(rig, fmt):
    asset_id = stored(image_bytes(fmt))
    old_bytes, old_name = proxy._resolve_cached_image_asset(
        asset_id, access_context(app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"]))
    old_type = proxy.asset_media_type(asset_id, proxy.asset_cache_path(asset_id))
    updated, error = run_extension(asset_id)
    assert error is None
    assert rig.uploads == [(old_name, old_bytes, old_type)]
    assert updated["extensions"][NS]["control_image"] == "engine-name.png"


def test_the_lease_is_released_once_the_engine_has_the_bytes(rig):
    run_extension(stored(image_bytes("PNG")))
    assert lease_rows() == 1 and live_rows() == 0


def test_the_lease_is_released_when_the_upload_fails_too(rig, monkeypatch):
    async def failing(client, headers, content, *, filename, media_type="image/png"):
        return None, proxy.Response(status_code=502)

    monkeypatch.setattr(proxy, "_upload_graph_image_bytes", failing)
    _, error = run_extension(stored(image_bytes("PNG")))
    assert error is not None and live_rows() == 0


def test_the_lease_is_released_when_the_upload_raises(rig, monkeypatch):
    async def raising(client, headers, content, *, filename, media_type="image/png"):
        raise ConnectionError("engine went away")

    monkeypatch.setattr(proxy, "_upload_graph_image_bytes", raising)
    with pytest.raises(ConnectionError):
        run_extension(stored(image_bytes("PNG")))
    assert lease_rows() == 1 and live_rows() == 0


# Revert: ``resolve_asset`` caps a stored asset by ``max_bytes`` again.
def test_a_stored_image_over_the_download_cap_still_goes_to_the_engine(rig, monkeypatch):
    """The cap bounds what the gateway fetches or receives; the old path never measured a stored image against it."""
    monkeypatch.setattr(proxy.media_fetch, "max_bytes", lambda: 8)
    data = image_bytes("PNG")
    assert len(data) > 8
    _, error = run_extension(stored(data))
    assert error is None and [content for _, content, _ in rig.uploads] == [data]


# Revert: the ``may_read`` check, or the call to the resolver.
def test_another_apps_asset_is_refused_with_the_old_text_and_no_upload(rig):
    asset_id = stored(image_bytes("PNG"))
    _, error = run_extension(asset_id, app=OTHER_APP)
    assert error is not None and error.status_code == 400
    assert b"asset_id is not accessible for this API key" in error.body
    assert rig.uploads == [] and lease_rows() == 0


def test_a_missing_asset_is_refused_with_the_old_text(rig):
    _, error = run_extension("img_" + "e" * 64)
    assert error is not None and b"asset_id not found (may have been evicted)" in error.body
    assert rig.uploads == []


def test_an_asset_id_of_another_kind_is_refused_before_anything_else(rig):
    _, error = run_extension("aud_" + "e" * 64)
    assert error is not None and b"asset_id must reference a cached image asset" in error.body
    assert rig.uploads == [] and lease_rows() == 0


# Revert: ``accepted`` in the resolver, or ``accepts=("image/*",)`` here.
def test_a_stored_file_that_is_not_an_image_is_refused_whatever_its_id_says(rig):
    """The old route sent any bytes under an image id. The resolver measures them, so these are refused."""
    asset_id = stored(image_bytes("PNG"))
    # Simulate a legacy/corrupted cache entry: new writes reject these bytes.
    proxy.asset_cache_path(asset_id).write_bytes(b"not an image at all")
    _, error = run_extension(asset_id)
    assert error is not None and b"asset_id must reference a cached image asset" in error.body
    assert rig.uploads == [] and lease_rows() == 0


def make_engine_remote(rig):
    rig.registry["services"][GRAPH_SERVICE_ID]["config"]["provider_type"] = "paid-remote"


# Revert: the disclosure check in ``resolve_asset``.
def test_a_remote_engine_the_plan_does_not_list_receives_nothing(rig):
    make_engine_remote(rig)
    asset_id = stored(image_bytes("PNG"))
    _, error = run_extension(asset_id, plan_or_access=access(plan_for(paid=[])))
    assert error is not None and b"does not allow sending files to the provider" in error.body
    assert rig.uploads == [] and lease_rows() == 0


def test_a_remote_engine_the_plan_lists_receives_the_input(rig):
    make_engine_remote(rig)
    asset_id = stored(image_bytes("PNG"))
    _, error = run_extension(asset_id, plan_or_access=access(plan_for(paid=[GRAPH_SERVICE_ID])))
    assert error is None and len(rig.uploads) == 1


def test_an_engine_missing_from_the_registry_receives_nothing(rig):
    rig.registry["services"].clear()
    _, error = run_extension(stored(image_bytes("PNG")))
    assert error is not None and rig.uploads == [] and lease_rows() == 0
