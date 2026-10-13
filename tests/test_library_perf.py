"""Library list: paging, scope, query count, previews (thumbnails) and Server-Timing."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import proxy
from gateway import admin_store, me_media, media_jobs, receipt_store, thumbnails
from gateway.admin_oidc import AdminSession
from gateway.receipts import RouteReceipt
from gateway.server_timing import ServerTimingMiddleware


def _png(width: int = 1600, height: int = 1200) -> bytes:
    image = Image.effect_noise((width, height), 90).convert("RGB")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class Library:
    """A router over temporary stores. Assets are files named by digest."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache = root / "cache"
        self.cache.mkdir()
        self.admin, self.jobs, self.receipts = root / "admin.db", root / "jobs.db", root / "receipts.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        receipt_store.init_db(self.receipts)
        self.alice = admin_store.upsert_oidc_user("iss", "alice", email="alice@example.test", path=self.admin)
        self.bob = admin_store.upsert_oidc_user("iss", "bob", email="bob@example.test", path=self.admin)
        self.app_a = admin_store.create_app("Alice", kind="personal", owner_user_id=self.alice["id"], path=self.admin)
        self.app_b = admin_store.create_app("Bob", kind="personal", owner_user_id=self.bob["id"], path=self.admin)
        self.user = self.alice
        self.count = 0
        app = FastAPI()
        app.add_middleware(ServerTimingMiddleware)
        app.include_router(me_media.create_me_media_router(
            session_provider=lambda request: AdminSession(
                subject=self.user["id"], email=self.user["email"], role="none",
                issued_at=time.time(), expires_at=time.time() + 3600),
            signed_url_builder=lambda request, asset_id, mode: f"http://t/{mode}/{asset_id}",
            asset_path_resolver=self.path_of,
            asset_media_type_resolver=lambda asset_id, path: "image/png",
            asset_deleter=lambda asset_id: False,
            asset_catalog_provider=lambda: [],
            asset_metadata_reader=lambda asset_id: {},
            admin_db_path=self.admin, media_jobs_path=self.jobs,
            receipts_path=self.receipts, prompt_memory_path=self.jobs,
        ))
        self.client = TestClient(app)

    def path_of(self, asset_id: str) -> Path:
        return self.cache / f"{asset_id.split('_', 1)[1]}.png"

    def add(self, app: dict, *, created_at: int, model: str = "image", kind: str = "image", png: bytes | None = None) -> str:
        self.count += 1
        asset_id = f"{'img' if kind == 'image' else 'aud'}_{self.count:064x}"
        request_id = f"req_{self.count}"
        self.path_of(asset_id).write_bytes(png or _png(8, 8))
        receipt_store.record(RouteReceipt(
            protocol="openai.images", intent="image.generate", path="/v1/images/generations",
            request_id=request_id, app_id=app["id"], requested_model=model, resolved_model=model,
            status="succeeded", accepted_at_ms=created_at * 1000,
        ), path=self.receipts)
        media_jobs.create(media_jobs.MediaJob(
            request_id=request_id, intent="image.generate", status=media_jobs.STATUS_SUCCEEDED,
            created_at=created_at, updated_at=created_at, progress=100,
            assets=[{"asset_id": asset_id, "kind": kind, "model": model}],
        ), path=self.jobs)
        return asset_id

    def ids(self, query: str = "") -> list[str]:
        response = self.client.get(f"/me/api/media?{query}")
        assert response.status_code == 200, response.text
        return [item["asset_id"] for item in response.json()["assets"]]

    def walk(self, query: str = "", limit: int = 4) -> list[str]:
        seen: list[str] = []
        cursor = ""
        while True:
            response = self.client.get(f"/me/api/media?limit={limit}&{query}&cursor={cursor}")
            assert response.status_code == 200, response.text
            body = response.json()
            seen += [item["asset_id"] for item in body["assets"]]
            cursor = body["next_cursor"] or ""
            if not cursor:
                return seen


@pytest.fixture()
def library(tmp_path: Path) -> Library:
    return Library(tmp_path)


def test_cursor_walk_visits_each_asset_once_newest_first_with_ties(library: Library) -> None:
    ids = [library.add(library.app_a, created_at=1_700_000_000 + (index // 3)) for index in range(14)]
    walked = library.walk()
    assert sorted(walked) == sorted(ids) and len(walked) == len(set(walked))
    keys = [(1_700_000_000 + ids.index(asset) // 3, asset) for asset in walked]
    assert keys == sorted(keys, reverse=True)


def test_cursor_is_stable_when_newer_assets_arrive_between_pages(library: Library) -> None:
    older = [library.add(library.app_a, created_at=1_700_000_000 + index) for index in range(9)]
    first = library.client.get("/me/api/media?limit=4").json()
    library.add(library.app_a, created_at=1_800_000_000)
    rest = library.client.get(f"/me/api/media?limit=100&cursor={first['next_cursor']}").json()
    paged = [item["asset_id"] for item in first["assets"]] + [item["asset_id"] for item in rest["assets"]]
    assert paged == sorted(older, key=lambda asset: older.index(asset), reverse=True)


def test_filters_narrow_the_pages_and_facets_cover_the_whole_library(library: Library) -> None:
    for index in range(6):
        library.add(library.app_a, created_at=1_700_000_000 + index, model="alpha" if index % 2 else "beta")
    library.add(library.app_a, created_at=1_700_000_100, kind="audio", model="gamma")
    assert len(library.walk("model=alpha")) == 3
    assert len(library.walk("kind=audio")) == 1
    window = library.ids("start=2023-11-14&end=2023-11-14&limit=100")
    assert window and set(window) <= set(library.ids("limit=100"))
    body = library.client.get("/me/api/media?model=alpha&limit=1").json()
    assert body["models"] == ["alpha", "beta", "gamma"] and body["has_more"] is True
    assert library.client.get("/me/api/media?cursor=not-a-cursor").status_code == 400


def test_scope_still_denies_other_people_and_everyone_for_members(library: Library) -> None:
    mine = library.add(library.app_a, created_at=1_700_000_000)
    theirs = library.add(library.app_b, created_at=1_700_000_001)
    assert library.ids("limit=100") == [mine]
    assert library.client.get("/me/api/media?scope=everyone").status_code == 403
    assert library.client.get("/me/api/media?person=" + library.bob["id"]).status_code == 403
    assert library.client.get(f"/me/api/media/{theirs}").status_code == 404
    library.user = library.bob
    assert library.ids("limit=100") == [theirs]
    assert library.client.get(f"/me/api/media/{mine}").status_code == 404


def _statements(library: Library, query: str) -> int:
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    with patch.object(sqlite3, "connect", connect):
        assert library.client.get(query).status_code == 200
    return len(seen)


def test_list_query_count_does_not_grow_with_the_library(library: Library) -> None:
    for index in range(8):
        library.add(library.app_a, created_at=1_700_000_000 + index)
    small = _statements(library, "/me/api/media?limit=5")
    for index in range(8, 120):
        library.add(library.app_a, created_at=1_700_000_000 + index)
    large = _statements(library, "/me/api/media?limit=5")
    assert small > 10, small  # the trace saw the requests
    assert large == small, f"{small} statements for 8 assets, {large} for 120"


def test_server_timing_reports_db_build_and_total(library: Library) -> None:
    library.add(library.app_a, created_at=1_700_000_000)
    header = library.client.get("/me/api/media").headers["server-timing"]
    names = [part.split(";")[0].strip() for part in header.split(",")]
    assert names == ["db", "build", "total"] and all("dur=" in part for part in header.split(","))
    assert "server-timing" in library.client.get("/me/api/jobs").headers


def test_items_carry_a_thumbnail_url_only_when_the_file_exists(library: Library) -> None:
    present = library.add(library.app_a, created_at=1_700_000_001)
    gone = library.add(library.app_a, created_at=1_700_000_000)
    library.path_of(gone).unlink()
    items = {item["asset_id"]: item for item in library.client.get("/me/api/media?limit=10").json()["assets"]}
    assert items[present]["thumbnail_url"] == f"http://t/thumb/{present}"
    assert items[gone]["thumbnail_url"] is None


# -- previews ---------------------------------------------------------------

def test_thumbnail_is_small_webp_without_metadata_and_is_made_once(tmp_path: Path) -> None:
    source = tmp_path / ("a" * 64 + ".png")
    source.write_bytes(_png())
    with patch.object(thumbnails, "_make", wraps=thumbnails._make) as make:
        first = thumbnails.thumbnail_for(source)
        again = thumbnails.thumbnail_for(source)
    assert first == again == tmp_path / "thumbs" / f"{'a' * 64}.{thumbnails.THUMB_EDGE}.webp"
    assert make.call_count == 1
    with Image.open(first) as image:
        assert image.format == "WEBP" and max(image.size) == thumbnails.THUMB_EDGE
        assert not image.getexif() and "workflow" not in image.info
    assert first.stat().st_size < source.stat().st_size / 5


def test_thumbnail_never_enlarges_and_is_remade_when_the_asset_changes(tmp_path: Path) -> None:
    source = tmp_path / ("b" * 64 + ".png")
    source.write_bytes(_png(120, 80))
    with Image.open(thumbnails.thumbnail_for(source)) as image:
        assert image.size == (120, 80)
    time.sleep(0.01)
    source.write_bytes(_png(900, 300))
    with Image.open(thumbnails.thumbnail_for(source)) as image:
        assert image.size == (thumbnails.THUMB_EDGE, 160)


def test_thumbnail_refuses_audio_and_missing_or_broken_files(tmp_path: Path) -> None:
    assert thumbnails.thumbnail_for(tmp_path / "missing.png") is None
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"ID3")
    assert thumbnails.thumbnail_for(audio) is None
    broken = tmp_path / ("c" * 64 + ".png")
    broken.write_bytes(b"not an image")
    assert thumbnails.thumbnail_for(broken) is None
    assert not (tmp_path / "thumbs").exists() or not list((tmp_path / "thumbs").glob("*.tmp"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_video_thumbnail_comes_from_the_first_frame(tmp_path: Path) -> None:
    video = tmp_path / ("d" * 64 + ".mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=5:duration=1",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    with Image.open(thumbnails.thumbnail_for(video)) as image:
        assert max(image.size) == thumbnails.THUMB_EDGE


def test_removing_a_thumbnail_is_quiet_when_there_is_none(tmp_path: Path) -> None:
    source = tmp_path / ("e" * 64 + ".png")
    source.write_bytes(_png(64, 64))
    thumbnails.remove_thumbnail(source)
    made = thumbnails.thumbnail_for(source)
    thumbnails.remove_thumbnail(source)
    assert not made.exists()


# -- the signed thumbnail route ----------------------------------------------

@pytest.fixture()
def gateway_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "images"
    cache.mkdir()
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-secret")
    return cache


def _signed(asset_id: str, mode: str, *, expires: int | None = None) -> str:
    expires = expires or int(time.time()) + 600
    return f"/v1/assets/{asset_id}/thumbnail?expires={expires}&sig={proxy._sign_asset_token(asset_id, expires, mode)}"


def test_thumbnail_route_serves_cached_webp_for_a_valid_signature(gateway_cache: Path) -> None:
    asset_id = "img_" + "1" * 64
    (gateway_cache / f"{'1' * 64}.png").write_bytes(_png())
    client = TestClient(proxy.app)
    response = client.get(_signed(asset_id, thumbnails.THUMB_MODE))
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert "max-age" in response.headers["cache-control"]
    assert len(response.content) < (gateway_cache / f"{'1' * 64}.png").stat().st_size / 5
    again = client.get(_signed(asset_id, thumbnails.THUMB_MODE), headers={"if-none-match": response.headers["etag"]})
    assert again.status_code == 304


@pytest.mark.parametrize("mode", ["none", "full", None])
def test_content_signatures_do_not_open_the_thumbnail_route(gateway_cache: Path, mode: str | None) -> None:
    asset_id = "img_" + "2" * 64
    (gateway_cache / f"{'2' * 64}.png").write_bytes(_png(64, 64))
    assert TestClient(proxy.app).get(_signed(asset_id, mode)).status_code == 403  # type: ignore[arg-type]


def test_thumbnail_route_refuses_bad_expired_and_foreign_signatures(gateway_cache: Path) -> None:
    asset_id, other = "img_" + "3" * 64, "img_" + "4" * 64
    (gateway_cache / f"{'3' * 64}.png").write_bytes(_png(64, 64))
    (gateway_cache / f"{'4' * 64}.png").write_bytes(_png(64, 64))
    client = TestClient(proxy.app)
    assert client.get(_signed(asset_id, thumbnails.THUMB_MODE, expires=int(time.time()) - 5)).status_code == 403
    assert client.get(f"/v1/assets/{asset_id}/thumbnail?expires={int(time.time()) + 60}&sig=bad").status_code == 403
    assert client.get(f"/v1/assets/{asset_id}/thumbnail?expires=soon&sig=x").status_code == 400
    # A signature made for one asset opens no other.
    foreign = _signed(other, thumbnails.THUMB_MODE).replace(other, asset_id)
    assert client.get(foreign).status_code == 403
    audio = "aud_" + "5" * 64
    (gateway_cache / f"{'5' * 64}.mp3").write_bytes(b"ID3")
    assert client.get(_signed(audio, thumbnails.THUMB_MODE)).status_code == 404


def test_thumbnail_url_is_stable_within_the_signing_window(gateway_cache: Path) -> None:
    from starlette.requests import Request
    request = Request({"type": "http", "headers": [(b"host", b"t")], "scheme": "http", "server": ("t", 80), "path": "/", "query_string": b""})
    asset_id = "img_" + "6" * 64
    first = proxy.build_signed_asset_url(request, asset_id, thumbnails.THUMB_MODE)
    assert first == proxy.build_signed_asset_url(request, asset_id, thumbnails.THUMB_MODE)
    assert "/thumbnail?expires=" in first and "md=" not in first
    expires = int(first.split("expires=")[1].split("&")[0])
    assert proxy.IMAGE_SIGNED_URL_TTL <= expires - time.time() <= 2 * proxy.IMAGE_SIGNED_URL_TTL
