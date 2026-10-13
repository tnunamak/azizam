"""Work budgets: a request's SQLite statements and subprocesses must not grow with the data.

Counts, not seconds: a count is the same on a loaded host and on a fast one. Each
test runs one request at a small and a large data size. Equal counts mean no
per-item query or probe; a bounded count keeps the constant part honest.
(docs/design/TEST_STRATEGY.md; scripts/perf/ measures the same counts on real data.)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, me_media, media_jobs, ownership, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.receipts import RouteReceipt
from tests.support.work import count_work


class UsageFixture:
    """Alice's apps and audio assets: each asset has a job, a receipt, a file and a sidecar."""

    def __init__(self, root: Path) -> None:
        self.cache = root / "cache"
        self.cache.mkdir()
        self.admin, self.jobs, self.receipts = root / "admin.db", root / "jobs.db", root / "receipts.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        receipt_store.init_db(self.receipts)
        self.alice = admin_store.upsert_oidc_user("iss", "alice", email="alice@example.test", path=self.admin)
        self.apps = [admin_store.create_app(f"App {n}", kind="personal", owner_user_id=self.alice["id"], path=self.admin)
                     for n in range(3)]
        self.count = 0
        self.assets: list[str] = []
        app = FastAPI()
        app.include_router(me_media.create_me_media_router(
            session_provider=lambda request: AdminSession(
                subject=self.alice["id"], email=self.alice["email"], role="none",
                issued_at=time.time(), expires_at=time.time() + 3600),
            signed_url_builder=lambda request, asset_id, mode: f"http://t/{mode}/{asset_id}",
            asset_path_resolver=lambda asset_id: self.cache / f"{asset_id.split('_', 1)[1]}.mp3",
            asset_media_type_resolver=lambda asset_id, path: "audio/mpeg",
            asset_deleter=lambda asset_id: False,
            asset_catalog_provider=lambda: list(self.assets),
            asset_metadata_reader=lambda asset_id: {
                "owner_key_hashes": [ownership.principal_key("app", self.apps[0]["id"])]},
            admin_db_path=self.admin, media_jobs_path=self.jobs,
            receipts_path=self.receipts, prompt_memory_path=self.jobs,
        ))
        self.client = TestClient(app)

    def add_audio(self, *, with_job: bool = True) -> str:
        self.count += 1
        asset_id = f"aud_{self.count:064x}"
        request_id = f"req_{self.count}"
        now = int(time.time())
        (self.cache / f"{self.count:064x}.mp3").write_bytes(b"not really audio")
        self.assets.append(asset_id)
        if with_job:
            app = self.apps[self.count % len(self.apps)]
            receipt_store.record(RouteReceipt(
                protocol="openai.audio", intent="audio.generate", path="/v1/audio/generate",
                request_id=request_id, app_id=app["id"], requested_model="music", resolved_model="music",
                status="succeeded", accepted_at_ms=now * 1000,
            ), path=self.receipts)
            media_jobs.create(media_jobs.MediaJob(
                request_id=request_id, intent="audio.generate", status=media_jobs.STATUS_SUCCEEDED,
                created_at=now, updated_at=now, progress=100,
                assets=[{"asset_id": asset_id, "kind": "audio", "model": "music"}],
            ), path=self.jobs)
        return asset_id

    def usage(self):
        response = self.client.get("/me/api/usage/summary")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture()
def usage(tmp_path: Path) -> UsageFixture:
    me_media._EMBEDDED_MEMO.clear()
    return UsageFixture(tmp_path)


def test_usage_summary_work_does_not_grow_with_the_assets(usage: UsageFixture) -> None:
    for _ in range(6):
        usage.add_audio()
    with count_work() as small:
        body = usage.usage()
    for _ in range(54):
        usage.add_audio()
    with count_work() as large:
        grown = usage.usage()
    assert body["media_count"] == 6 and grown["media_count"] == 60
    assert small.statements > 10, small  # the counter saw the request
    assert large.statements == small.statements, f"{small.statements} statements for 6 assets, {large.statements} for 60"
    assert large.processes == small.processes == 0, (small.commands, large.commands)


def test_usage_summary_does_not_probe_files_that_a_job_already_describes(usage: UsageFixture) -> None:
    for _ in range(5):
        usage.add_audio()
    with count_work() as work:
        usage.usage()
    assert work.processes == 0, work.commands


def test_usage_summary_probes_a_file_no_job_knows_once(usage: UsageFixture) -> None:
    usage.add_audio(with_job=False)
    with count_work() as first:
        usage.usage()
    with count_work() as again:
        usage.usage()
    assert first.processes == 1 and again.processes == 0, (first.commands, again.commands)


# -- category gate: one store read per read request ----------------------------

def test_category_gate_asks_the_store_once_per_read_request(tmp_path: Path) -> None:
    from gateway import category_enforcement, request_connections
    from gateway.policy import PolicyRequest
    from tests.test_category_enforcement import Identity, _activate, _snapshot

    path = tmp_path / "categories.db"
    _activate(path, _snapshot())
    category_enforcement.configure(path)
    try:
        request = PolicyRequest(protocol="openai.images", intent="image.generate", workflow="flux")
        who = Identity("mia", "family-app")

        def checks(count: int) -> int:
            with count_work() as work, request_connections.request_scope():
                for _ in range(count):
                    category_enforcement.visible(who, request)
            return work.statements

        checks(1)  # opens the gate's own watch connection
        few, many = checks(2), checks(200)
        assert many == few, f"{few} statements for 2 checks in one read request, {many} for 200"
        # An activation made after the request started is seen by the next one.
        with request_connections.request_scope():
            category_enforcement.visible(who, request)
        from gateway.category_api import snapshot_to_json
        from gateway.category_store import CategoryStore

        store = CategoryStore(path)
        base = store.get_active().revision
        later = store.create_candidate(snapshot_to_json(_snapshot(value="adult")), base, "owner-1", "tighten")
        store.activate(later.revision, base)
        with request_connections.request_scope():
            assert category_enforcement.visible(who, request) is False
    finally:
        category_enforcement.configure(None)


# -- provider presets: parsed once, again only when a file changes ---------------

def test_provider_catalog_is_parsed_once_until_a_file_changes(tmp_path: Path) -> None:
    from unittest.mock import patch

    import yaml

    from gateway import provider_catalog

    source = provider_catalog.CATALOG_DIR
    for file in source.glob("*.yml"):
        (tmp_path / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    with patch.object(yaml, "safe_load", wraps=yaml.safe_load) as parse:
        first = provider_catalog.load_provider_catalog(tmp_path)
        parsed = parse.call_count
        again = provider_catalog.load_provider_catalog(tmp_path)
        assert parse.call_count == parsed > 0 and again == first
        victim = next(tmp_path.glob("*.yml"))
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        provider_catalog.load_provider_catalog(tmp_path)
        assert parse.call_count == parsed * 2


# -- categories API: the store is integrity-checked once per file ------------------

def test_categories_api_checks_the_store_file_once(tmp_path: Path) -> None:
    import sqlite3
    from unittest.mock import patch

    from gateway import category_api

    path = tmp_path / "categories.db"
    category_api._VERIFIED_STORES.clear()
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real(*args, **kwargs)
        connection.set_trace_callback(seen.append)
        return connection

    with patch.object(sqlite3, "connect", connect):
        for _ in range(5):
            category_api._verified_store(path)
    assert sum("integrity_check" in statement for statement in seen) == 1, seen
    path.unlink()  # a replaced file is a new file and is checked again
    with patch.object(sqlite3, "connect", connect):
        category_api._verified_store(path)
        category_api._verified_store(path)
    assert sum("integrity_check" in statement for statement in seen) == 2


# -- credential use stamp: a key used inside the interval does not write -------------

def test_a_busy_key_is_stamped_once_per_interval(tmp_path: Path) -> None:
    import sqlite3
    from unittest.mock import patch

    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("iss", "u", email="u@example.test", path=db)
    app = admin_store.create_app("A", kind="personal", owner_user_id=user["id"], path=db)
    key = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"], hash="h" * 64, prefix="masked", path=db)
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real(*args, **kwargs)
        connection.set_trace_callback(seen.append)
        return connection

    with patch.object(sqlite3, "connect", connect):
        results = [admin_store.touch_credential_last_used(key["id"], now=5000 + n, path=db) for n in range(30)]
        later = admin_store.touch_credential_last_used(key["id"], now=5000 + 90, path=db)
    assert results.count(True) == 1 and later is True
    writes = [statement for statement in seen if statement.startswith("UPDATE credentials")]
    assert len(writes) == 2, f"{len(writes)} UPDATEs for 31 uses in two intervals"


# -- request scope: which GET paths share one connection per store ------------------

@pytest.mark.parametrize(("path", "scoped"), [
    ("/me/api/home", True),
    ("/me/api/media/aud_x/download", True),  # a JSON document with a signed link, not bytes
    ("/openai/v1/models", True),
    ("/anthropic/v1/models", True),
    ("/gateway/v1/receipts", True),
    ("/v1/assets/aud_x/content", False),  # streams a file: no connection is held for its length
    ("/v1/audio/voices/v/sample", False),
    ("/admin/api/status", True),
    ("/health", False),
])
def test_which_get_paths_get_a_request_scope(path: str, scoped: bool) -> None:
    from gateway import request_connections

    seen: list[bool] = []

    async def app(scope, receive, send):
        seen.append(request_connections.active())

    import asyncio

    asyncio.run(request_connections.RequestConnectionsMiddleware(app)(
        {"type": "http", "method": "GET", "path": path}, None, None))
    assert seen == [scoped], path


# -- PNG provenance: the text chunks are found without reading the picture -----------

def _png_with(text_before: dict[str, str], text_after: dict[str, str], idat: int = 1000) -> bytes:  # idat: noise bytes
    import struct
    import zlib

    from gateway import media_metadata

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    body = media_metadata._PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for key, value in text_before.items():
        body += media_metadata._make_png_text_chunk(key, value)
    body += chunk(b"IDAT", zlib.compress(__import__("os").urandom(idat)))
    for key, value in text_after.items():
        body += media_metadata._make_png_text_chunk(key, value)
    return body + chunk(b"IEND", b"")


@pytest.mark.parametrize("before,after", [
    ({}, {}), ({"azizam": "a"}, {}), ({}, {"azizam": "late"}), ({"a": "1", "b": "2"}, {"a": "3"}),
    ({"é": "ü"}, {"long": "x" * 5000}),
])
def test_png_text_chunks_from_a_path_equal_those_of_the_bytes(tmp_path: Path, before, after) -> None:
    from gateway import media_metadata

    path = tmp_path / "x.png"
    path.write_bytes(_png_with(before, after))
    assert media_metadata.png_text_chunks_from_path(path) == media_metadata.png_text_chunks(path.read_bytes())


def test_png_text_chunks_from_a_path_fail_like_the_bytes_on_a_truncated_file(tmp_path: Path) -> None:
    from gateway import media_metadata

    whole = _png_with({"azizam": "a"}, {})
    for cut in (len(whole) - 5, len(whole) - 40, 20, 8):
        path = tmp_path / f"cut{cut}.png"
        path.write_bytes(whole[:cut])
        try:
            expected = media_metadata.png_text_chunks(whole[:cut])
        except media_metadata.MediaMetadataError:
            with pytest.raises(media_metadata.MediaMetadataError):
                media_metadata.png_text_chunks_from_path(path)
        else:
            assert media_metadata.png_text_chunks_from_path(path) == expected
    notpng = tmp_path / "n.png"
    notpng.write_bytes(b"GIF89a....")
    assert media_metadata.png_text_chunks_from_path(notpng) == {}


def test_reading_provenance_does_not_read_the_picture(tmp_path: Path) -> None:
    import builtins
    from unittest.mock import patch

    from gateway import media_metadata

    path = tmp_path / "big.png"
    path.write_bytes(_png_with({"azizam": "kept"}, {}, idat=2_000_000))
    real_open, read = builtins.open, [0]

    class Counting:
        def __init__(self, handle):
            self._handle = handle

        def read(self, n=-1):
            data = self._handle.read(n)
            read[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    def opener(file, *args, **kwargs):
        handle = real_open(file, *args, **kwargs)
        return Counting(handle) if str(file) == str(path) else handle

    with patch.object(builtins, "open", opener):
        assert media_metadata.png_text_chunks_from_path(path) == {"azizam": "kept"}
    assert read[0] < 300, f"read {read[0]} bytes of a {path.stat().st_size}-byte picture"
