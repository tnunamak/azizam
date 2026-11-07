"""The media input resolver (S2.6, D7): access, measuring, disclosure, leases and the orphan sweep.

The resolver runs against a fake asset store and a real jobs database. Each behaviour has a test that fails when its
fix is taken out; the comment above a test names the line to revert.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gateway import media_inputs, media_jobs
from gateway.media_input_assets import CacheAssetPort
from gateway.media_input_leases import MediaInputLedger
from gateway.media_inputs import (
    DISCLOSURE_REFUSED, EMPTY, FETCH_FAILED, FORBIDDEN, MISSING, TOO_LARGE, UNSUPPORTED, Destination, Holder,
    MediaInputError, MediaInputResolver,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
LOCAL = Destination("engine-a")
REMOTE = Destination("provider-b", remote=True, provider_id="provider-b")
IMAGES = ("image/*",)
ROOT = Path(__file__).resolve().parent.parent


class FakeAssets:
    """An in-memory store with the two read rules the real one has: an asset exists, and an owner may read it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.owners: dict[str, set[str]] = {}
        self.calls: list[str] = []

    def add(self, data: bytes, owner: str, kind: str = "img") -> str:
        asset_id = f"{kind}_{hashlib.sha256(data).hexdigest()}"
        self.files[asset_id] = data
        self.owners.setdefault(asset_id, set()).add(owner)
        return asset_id

    def parse(self, asset_id):
        kind, _, digest = str(asset_id).partition("_")
        return (kind, digest) if kind in {"img", "aud", "vid", "ast"} and len(digest) == 64 else None

    def exists(self, asset_id):
        return asset_id in self.files

    def has_claim(self, asset_id, owner_key):
        return asset_id in self.files and (not owner_key or owner_key in self.owners.get(asset_id, set()))

    def size(self, asset_id):
        return len(self.files[asset_id])

    def may_read(self, asset_id, auth):
        return auth.owner in self.owners.get(asset_id, set())

    def read(self, asset_id):
        self.calls.append("read")
        return self.files[asset_id]

    def owner_key(self, auth):
        return auth.owner

    def planned_id(self, data, suffix, kind):
        return f"{kind}_{hashlib.sha256(data).hexdigest()}"

    def put(self, data, *, kind, suffix, owner_key):
        self.calls.append("put")
        return self.add(data, owner_key or "", kind)

    def discard(self, asset_id, owner_key):
        owners = self.owners.get(asset_id, set()) - {owner_key}
        if owners:
            self.owners[asset_id] = owners
        else:
            self.files.pop(asset_id, None)
            self.owners.pop(asset_id, None)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def person(name: str) -> SimpleNamespace:
    return SimpleNamespace(owner=name)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build(db: Path, clock: Clock, *, assets: FakeAssets | None = None, fetch=None, remote_ok=False,
          max_bytes: int = 1024):
    assets = assets or FakeAssets()
    sent: list[str] = []

    def may_send_remote(provider: str, auth: Any) -> bool:
        sent.append(provider)
        return remote_ok

    resolver = MediaInputResolver(assets=assets, ledger=MediaInputLedger(db, clock=clock),
                                  may_send_remote=may_send_remote, max_bytes=max_bytes, fetch=fetch)
    return resolver, assets, sent


def rows(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def live(db: Path, holder: Holder) -> list[str]:
    return MediaInputLedger(db).live_leases(holder.kind, holder.id)


JOB = Holder("job", "mj_one")
REQUEST = Holder("request", "req_one")


# --- access -----------------------------------------------------------------


# Revert: drop the ``may_read`` check in ``resolve_asset``.
def test_another_persons_asset_is_refused_with_no_read_and_no_lease(db, clock):
    resolver, assets, _ = build(db, clock)
    theirs = assets.add(PNG, "alice")
    with pytest.raises(MediaInputError) as refused:
        resolver.resolve_asset(person("bob"), theirs, holder=JOB, destination=LOCAL, accepts=IMAGES)
    assert refused.value.code == FORBIDDEN
    assert assets.calls == []  # the bytes were never read
    assert rows(db, "media_input_lease") == 0


def test_the_owner_reads_their_own_asset_and_holds_a_lease(db, clock):
    resolver, assets, _ = build(db, clock)
    mine = assets.add(PNG, "alice")
    resolved = resolver.resolve_asset(person("alice"), mine, holder=JOB, destination=LOCAL, accepts=IMAGES)
    assert resolved.data == PNG
    assert resolved.ref.asset_id == mine
    assert live(db, JOB) == [mine]


def test_a_missing_or_malformed_asset_id_is_refused_before_any_read(db, clock):
    resolver, assets, _ = build(db, clock)
    with pytest.raises(MediaInputError) as missing:
        resolver.resolve_asset(person("a"), "img_" + "0" * 64, holder=JOB, destination=LOCAL, accepts=IMAGES)
    assert missing.value.code == MISSING
    with pytest.raises(MediaInputError) as malformed:
        resolver.resolve_asset(person("a"), "not an id", holder=JOB, destination=LOCAL, accepts=IMAGES)
    assert malformed.value.code == media_inputs.INVALID
    assert assets.calls == [] and rows(db, "media_input_lease") == 0


# --- measuring --------------------------------------------------------------


class Fetched:
    """A download whose headers lie about everything the resolver must measure for itself."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.content_type = "application/pdf"
        self.filename = "report.pdf"
        self.headers = {"content-length": "1"}


# Revert: ``measure`` returns ``hint`` (the claimed type) instead of the sniffed one.
def test_a_claimed_type_size_and_name_are_ignored_and_the_bytes_are_measured(db, clock):
    async def fetch(url: str):
        return Fetched(PNG)

    resolver, assets, _ = build(db, clock, fetch=fetch)
    resolved = asyncio.run(resolver.resolve_url(person("a"), "https://x.test/report.pdf", holder=REQUEST,
                                                destination=LOCAL, accepts=IMAGES))
    assert resolved.ref.media_type == "image/png"
    assert resolved.ref.size_bytes == len(PNG)
    assert resolved.ref.sha256 == hashlib.sha256(PNG).hexdigest()


def test_the_upload_entry_takes_no_claimed_type_size_or_name():
    import inspect
    names = set(inspect.signature(MediaInputResolver.resolve_upload).parameters)
    assert names == {"self", "auth", "body", "holder", "destination", "accepts"}


# Revert: ``accepted`` returns True for an unidentified file.
def test_bytes_that_only_claim_to_be_an_image_are_refused_and_nothing_is_stored(db, clock):
    resolver, assets, _ = build(db, clock)
    with pytest.raises(MediaInputError) as refused:
        resolver.resolve_upload(person("a"), b"<html>not an image</html>", holder=REQUEST, destination=LOCAL,
                                accepts=IMAGES)
    assert refused.value.code == UNSUPPORTED
    assert assets.calls == [] and rows(db, "media_input_effect") == 0 and rows(db, "media_input_lease") == 0


def test_the_type_check_follows_the_input_declaration(db, clock):
    resolver, _, _ = build(db, clock)
    with pytest.raises(MediaInputError):
        resolver.resolve_upload(person("a"), JPEG, holder=REQUEST, destination=LOCAL, accepts=("image/png",))
    assert resolver.resolve_upload(person("a"), JPEG, holder=REQUEST, destination=LOCAL,
                                   accepts=("image/png", "image/jpeg")).ref.media_type == "image/jpeg"


def test_an_empty_or_oversized_body_is_refused_before_storing(db, clock):
    resolver, assets, _ = build(db, clock, max_bytes=100)
    with pytest.raises(MediaInputError) as empty:
        resolver.resolve_upload(person("a"), b"", holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    assert empty.value.code == EMPTY
    with pytest.raises(MediaInputError) as large:
        resolver.resolve_upload(person("a"), PNG + b"\x00" * 100, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    assert large.value.code == TOO_LARGE
    assert assets.calls == [] and rows(db, "media_input_effect") == 0


# Revert: ``resolve_asset`` passes ``capped=True`` to ``_check_bytes`` (or checks ``size`` against ``max_bytes``).
def test_a_stored_asset_over_the_limit_still_resolves(db, clock):
    """The limit bounds what the gateway fetches or receives. A file already in the store is not refused by it."""
    resolver, assets, _ = build(db, clock, max_bytes=100)
    big = assets.add(PNG + b"\x00" * 100, "a")
    resolved = resolver.resolve_asset(person("a"), big, holder=JOB, destination=LOCAL, accepts=IMAGES)
    assert resolved.data == assets.files[big] and resolved.ref.size_bytes == len(resolved.data)
    assert live(db, JOB) == [big]


# Revert: the length check in ``_check_bytes`` (the ``capped`` branch).
def test_a_fetched_body_over_the_limit_is_refused_and_nothing_is_stored(db, clock):
    async def fetch(url: str):
        return Fetched(PNG + b"\x00" * 100)

    resolver, assets, _ = build(db, clock, max_bytes=100, fetch=fetch)
    with pytest.raises(MediaInputError) as large:
        asyncio.run(resolver.resolve_url(person("a"), "https://x.test/p.png", holder=REQUEST, destination=LOCAL,
                                         accepts=IMAGES))
    assert large.value.code == TOO_LARGE
    assert assets.calls == [] and rows(db, "media_input_effect") == 0


# --- disclosure -------------------------------------------------------------


# Revert: ``check_disclosure`` returns for a remote destination.
def test_a_remote_upstream_the_plan_does_not_allow_gets_nothing(db, clock):
    fetched: list[str] = []

    async def fetch(url: str):
        fetched.append(url)
        return Fetched(PNG)

    resolver, assets, asked = build(db, clock, fetch=fetch, remote_ok=False)
    mine = assets.add(PNG, "alice")
    calls_before = list(assets.calls)
    with pytest.raises(MediaInputError) as by_asset:
        resolver.resolve_asset(person("alice"), mine, holder=JOB, destination=REMOTE, accepts=IMAGES)
    with pytest.raises(MediaInputError) as by_upload:
        resolver.resolve_upload(person("alice"), PNG, holder=JOB, destination=REMOTE, accepts=IMAGES)
    with pytest.raises(MediaInputError) as by_url:
        asyncio.run(resolver.resolve_url(person("alice"), "https://x.test/a.png", holder=JOB, destination=REMOTE,
                                         accepts=IMAGES))
    assert {by_asset.value.code, by_upload.value.code, by_url.value.code} == {DISCLOSURE_REFUSED}
    assert asked == ["provider-b"] * 3
    # Zero effects: no read, no store, no download, no marker, no lease.
    assert assets.calls == calls_before and fetched == []
    assert rows(db, "media_input_effect") == 0 and rows(db, "media_input_lease") == 0


def test_a_remote_upstream_the_plan_allows_receives_the_input(db, clock):
    resolver, assets, asked = build(db, clock, remote_ok=True)
    mine = assets.add(PNG, "alice")
    assert resolver.resolve_asset(person("alice"), mine, holder=JOB, destination=REMOTE, accepts=IMAGES).data == PNG
    assert asked == ["provider-b"]


def test_a_local_upstream_needs_no_grant(db, clock):
    resolver, assets, asked = build(db, clock)
    resolver.resolve_upload(person("a"), PNG, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    assert asked == []


# Revert: ``check_disclosure`` ignores ``known``.
def test_an_upstream_the_registry_does_not_know_gets_nothing(db, clock):
    resolver, assets, _ = build(db, clock)
    with pytest.raises(MediaInputError) as refused:
        resolver.resolve_upload(person("a"), PNG, holder=REQUEST, destination=Destination("ghost", known=False),
                                accepts=IMAGES)
    assert refused.value.code == DISCLOSURE_REFUSED and assets.calls == []


def test_a_remote_destination_with_no_provider_id_is_refused(db, clock):
    resolver, assets, _ = build(db, clock, remote_ok=True)
    with pytest.raises(MediaInputError):
        resolver.resolve_upload(person("a"), PNG, holder=REQUEST, destination=Destination("x", remote=True),
                                accepts=IMAGES)


# --- URLs -------------------------------------------------------------------


# Revert: ``resolve_url`` calls a client of its own instead of ``self._fetch``.
def test_a_url_goes_only_through_the_callers_fetch(db, clock):
    seen: list[str] = []

    async def fetch(url: str):
        seen.append(url)
        return Fetched(PNG)

    resolver, _, _ = build(db, clock, fetch=fetch)
    asyncio.run(resolver.resolve_url(person("a"), "https://x.test/p.png", holder=REQUEST, destination=LOCAL,
                                     accepts=IMAGES))
    assert seen == ["https://x.test/p.png"]


def test_a_refusal_by_the_fetchers_origin_rules_stores_nothing(db, clock):
    async def fetch(url: str):
        raise ValueError("media URL resolves to a blocked network address")

    resolver, assets, _ = build(db, clock, fetch=fetch)
    with pytest.raises(MediaInputError) as refused:
        asyncio.run(resolver.resolve_url(person("a"), "http://127.0.0.1/p.png", holder=REQUEST, destination=LOCAL,
                                         accepts=IMAGES))
    assert refused.value.code == FETCH_FAILED and "blocked network address" in refused.value.message
    assert assets.calls == [] and rows(db, "media_input_effect") == 0


def test_the_resolver_holds_no_network_client_of_its_own():
    """One fetcher: the module imports nothing that can open a connection."""
    tree = ast.parse((ROOT / "gateway" / "media_inputs.py").read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"httpx", "requests", "urllib", "aiohttp", "socket", "http", "ssl"}
    assert "gateway.media_fetch" not in {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}


# --- leases: released when the holder ends -----------------------------------


def lease_one(db, clock, holder: Holder, owner: str = "alice"):
    resolver, assets, _ = build(db, clock)
    resolved = resolver.resolve_upload(person(owner), PNG, holder=holder, destination=LOCAL, accepts=IMAGES)
    return resolver, assets, resolved


# Revert: the ``release_job_in`` call in ``media_jobs._write_job``.
@pytest.mark.parametrize("status", [media_jobs.STATUS_SUCCEEDED, media_jobs.STATUS_FAILED, media_jobs.STATUS_CANCELED])
def test_the_lease_is_released_when_the_job_ends(db, clock, status):
    job = media_jobs.create(media_jobs.MediaJob(intent="x.y", request_id="r1"), path=db)
    holder = Holder("job", job.job_id)
    lease_one(db, clock, holder)
    assert len(live(db, holder)) == 1
    media_jobs.update_status(job.job_id, media_jobs.STATUS_RUNNING, path=db)
    assert len(live(db, holder)) == 1, "a running job keeps its inputs"
    media_jobs.update_status(job.job_id, status, path=db)
    assert live(db, holder) == []


# Revert: the ``release_job_in`` call in ``media_jobs._release_envelopes_in``.
def test_the_lease_is_released_when_the_job_is_deleted(db, clock):
    job = media_jobs.create(media_jobs.MediaJob(intent="x.y", request_id="r1", status=media_jobs.STATUS_SUCCEEDED,
                                                progress=100), path=db)
    holder = Holder("job", job.job_id)
    lease_one(db, clock, holder)
    other = Holder("job", "mj_other")
    lease_one(db, clock, other)
    assert media_jobs.delete_succeeded([job.job_id], path=db) == 1
    assert live(db, holder) == []
    assert len(live(db, other)) == 1, "another holder's lease stays"


# Revert: the ``release_owners_in`` call in ``media_jobs.erase_prompt_derivatives``.
def test_the_lease_is_released_when_its_owner_is_erased(db, clock):
    mine, theirs = Holder("job", "mj_mine"), Holder("job", "mj_theirs")
    lease_one(db, clock, mine, owner="alice")
    lease_one(db, clock, theirs, owner="bob")
    media_jobs.erase_prompt_derivatives({"alice"}, before=2**31, path=db)
    assert live(db, mine) == []
    assert len(live(db, theirs)) == 1


def test_a_household_erasure_releases_every_lease(db, clock):
    a, b = Holder("job", "mj_a"), Holder("job", "mj_b")
    lease_one(db, clock, a, owner="alice")
    lease_one(db, clock, b, owner="bob")
    media_jobs.erase_prompt_derivatives({"*"}, before=2**31, path=db)
    assert live(db, a) == [] and live(db, b) == []


def test_a_request_holder_releases_when_the_request_ends(db, clock):
    resolver, _, _ = lease_one(db, clock, REQUEST)
    assert len(live(db, REQUEST)) == 1
    assert resolver.release(REQUEST) == 1
    assert live(db, REQUEST) == []


def test_a_released_upload_is_removed_by_the_sweep_when_no_other_lease_holds_it(db, clock):
    resolver, assets, resolved = lease_one(db, clock, REQUEST)
    resolver.release(REQUEST)
    assert resolved.ref.asset_id in assets.files, "release only marks the lease; the file goes in the sweep"
    assert resolver.sweep() == 1
    assert resolved.ref.asset_id not in assets.files
    assert rows(db, "media_input_lease") == 0


def test_a_released_asset_the_ingest_did_not_create_is_never_removed(db, clock):
    resolver, assets, _ = build(db, clock)
    mine = assets.add(PNG, "alice")
    resolver.resolve_asset(person("alice"), mine, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    resolver.release(REQUEST)
    assert resolver.sweep() == 0
    assert mine in assets.files and rows(db, "media_input_lease") == 0


def test_a_file_two_holders_share_stays_until_both_release(db, clock):
    resolver, assets, _ = build(db, clock)
    first = resolver.resolve_upload(person("a"), PNG, holder=Holder("request", "r1"), destination=LOCAL, accepts=IMAGES)
    resolver.resolve_upload(person("a"), PNG, holder=Holder("request", "r2"), destination=LOCAL, accepts=IMAGES)
    resolver.release(Holder("request", "r1"))
    assert resolver.sweep() == 0 and first.ref.asset_id in assets.files
    resolver.release(Holder("request", "r2"))
    assert resolver.sweep() >= 1 and first.ref.asset_id not in assets.files


# --- crash and orphan sweep ---------------------------------------------------


class Crash(Exception):
    pass


# Revert: ``_ingest`` writes the marker after ``put``, or not at all.
def test_a_crash_between_the_file_and_the_lease_leaves_a_marker_the_sweep_clears(db, clock):
    resolver, assets, _ = build(db, clock)
    ledger_lease = resolver._ledger.lease

    def crash(*args, **kwargs):
        raise Crash

    resolver._ledger.lease = crash  # the process dies after the file is stored, before the lease commits
    with pytest.raises(Crash):
        resolver.resolve_upload(person("alice"), PNG, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    resolver._ledger.lease = ledger_lease
    stored = next(iter(assets.files))
    assert rows(db, "media_input_effect") == 1 and rows(db, "media_input_lease") == 0
    # Inside the grace period a marker may belong to a live ingest, so the sweep leaves it.
    assert resolver.sweep() == 0 and stored in assets.files and rows(db, "media_input_effect") == 1
    clock.now += 3601
    assert resolver.sweep() == 1
    assert stored not in assets.files and rows(db, "media_input_effect") == 0


def test_a_crash_before_the_file_is_stored_leaves_only_a_marker_for_a_file_that_does_not_exist(db, clock):
    resolver, assets, _ = build(db, clock)
    original = assets.put

    def crash(*args, **kwargs):
        raise Crash

    assets.put = crash
    with pytest.raises(Crash):
        resolver.resolve_upload(person("alice"), PNG, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    assets.put = original
    clock.now += 3601
    assert resolver.sweep() == 1 and assets.files == {} and rows(db, "media_input_effect") == 0


def test_the_sweep_never_removes_a_file_that_existed_before_the_ingest(db, clock):
    resolver, assets, _ = build(db, clock)
    mine = assets.add(PNG, "alice")  # already stored: the ingest creates nothing
    resolver._ledger.lease = lambda *a, **k: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        resolver.resolve_upload(person("alice"), PNG, holder=REQUEST, destination=LOCAL, accepts=IMAGES)
    clock.now += 3601
    resolver._ledger = MediaInputLedger(db, clock=clock)
    assert resolver.sweep() == 0 and mine in assets.files


def test_a_marker_inside_the_grace_period_survives_the_sweep(db, clock):
    resolver, assets, _ = build(db, clock)
    resolver._ledger.begin_effect("mie_live", owner_key="alice", asset_id="img_" + "1" * 64, already_claimed=lambda: False)
    assets.files["img_" + "1" * 64] = PNG
    clock.now += 60
    assert resolver.sweep() == 0 and rows(db, "media_input_effect") == 1


# Revert: the job-existence check in ``sweep_in``.
def test_a_job_lease_whose_job_never_came_to_exist_is_released_by_the_sweep(db, clock):
    resolver, assets, resolved = lease_one(db, clock, Holder("job", "mj_never_created"))
    assert resolver.sweep() == 0 and len(live(db, Holder("job", "mj_never_created"))) == 1, "inside the grace period"
    clock.now += 3601
    assert resolver.sweep() == 1
    assert resolved.ref.asset_id not in assets.files and rows(db, "media_input_lease") == 0


def test_a_job_lease_of_a_running_job_survives_the_sweep(db, clock):
    job = media_jobs.create(media_jobs.MediaJob(intent="x.y", request_id="r1"), path=db)
    holder = Holder("job", job.job_id)
    resolver, assets, resolved = lease_one(db, clock, holder)
    clock.now += 10_000
    assert resolver.sweep() == 0 and resolved.ref.asset_id in assets.files and len(live(db, holder)) == 1


# Revert: the request-age release in ``sweep_in``.
def test_a_request_lease_that_was_never_released_expires(db, clock):
    resolver, assets, resolved = lease_one(db, clock, REQUEST)
    clock.now += 3601
    assert resolver.sweep() == 1 and resolved.ref.asset_id not in assets.files


def test_the_sweep_is_idempotent(db, clock):
    resolver, _, _ = lease_one(db, clock, REQUEST)
    resolver.release(REQUEST)
    assert resolver.sweep() == 1
    assert resolver.sweep() == 0


def test_a_reference_is_immutable_and_holds_no_bytes():
    ref = media_inputs.StoredInput("img_" + "0" * 64, "image/png", 3, "ab", "mil_x")
    with pytest.raises(Exception):
        ref.asset_id = "other"  # type: ignore[misc]
    assert not any(isinstance(v, (bytes, bytearray)) for v in vars(ref).values())


# --- the sweep re-checks each file when it removes it -------------------------


def released_upload(db, clock):
    """An upload that this ingest created, released, with its lease row not yet swept."""
    resolver, assets, resolved = lease_one(db, clock, REQUEST)
    resolver.release(REQUEST)
    return resolver, assets, resolved.ref.asset_id


# Revert: ``discard_unheld`` calls ``discard`` without the held and pending checks.
def test_a_lease_granted_after_the_scan_keeps_its_file(db, clock):
    resolver, assets, asset = released_upload(db, clock)
    ledger = resolver._ledger
    found = ledger.sweep_scan()
    assert [item.asset_id for item in found] == [asset], "the scan saw nothing holding it"
    # Another request reads the file and leases it after the scan committed, before the discard.
    other = Holder("request", "r_late")
    resolver.resolve_asset(person("alice"), asset, holder=other, destination=LOCAL, accepts=IMAGES)
    assert ledger.discard_unheld(found, assets.discard) == 0
    assert asset in assets.files
    # The duty row stays with the file: when the late holder lets go, the next pass removes it.
    resolver.release(other)
    assert resolver.sweep() == 1 and asset not in assets.files


# Revert: ``discard_unheld`` calls ``discard`` without the held and pending checks.
def test_an_ingest_marker_written_after_the_scan_keeps_its_file(db, clock):
    resolver, assets, asset = released_upload(db, clock)
    ledger = resolver._ledger
    found = ledger.sweep_scan()
    assert [item.asset_id for item in found] == [asset]
    # A second ingest of the same bytes starts after the scan: it writes its marker, then the lease.
    ledger.begin_effect("mie_late", owner_key="alice", asset_id=asset, already_claimed=lambda: True)
    assert ledger.discard_unheld(found, assets.discard) == 0
    assert asset in assets.files
    holder = Holder("request", "r_late")
    ledger.lease("mil_late", asset_id=asset, owner_key="alice", holder_kind=holder.kind, holder_id=holder.id,
                 effect_id="mie_late")
    resolver.release(holder)
    assert resolver.sweep() == 1 and asset not in assets.files


# Revert: ``resolve_asset`` returns without looking for the file again after its lease.
def test_a_file_the_sweep_removed_before_the_lease_is_refused_as_missing(db, clock):
    resolver, assets, asset = released_upload(db, clock)
    ledger = resolver._ledger
    found = ledger.sweep_scan()
    lease = ledger.lease

    def sweep_wins_then_lease(*args, **kwargs):
        # The sweep's discard runs after the resolver read the bytes and before its lease lands.
        assert ledger.discard_unheld(found, assets.discard) == 1
        lease(*args, **kwargs)

    ledger.lease = sweep_wins_then_lease
    with pytest.raises(MediaInputError) as refused:
        resolver.resolve_asset(person("alice"), asset, holder=Holder("request", "r_late"), destination=LOCAL, accepts=IMAGES)
    assert refused.value.code == MISSING
    assert asset not in assets.files


def test_a_file_nothing_holds_at_discard_time_is_removed(db, clock):
    resolver, assets, asset = released_upload(db, clock)
    found = resolver._ledger.sweep_scan()
    assert resolver._ledger.discard_unheld(found, assets.discard) == 1
    assert asset not in assets.files


# --- the duty to remove a file outlives its first holder ---------------------


# Revert: ``discard_unheld`` hands the duty to the holder as a flag (and drops the other owner's removal).
def test_a_second_owners_ingest_of_the_same_bytes_does_not_lose_the_first_owners_removal(db, clock):
    resolver, assets, first = lease_one(db, clock, Holder("request", "r_alice"), owner="alice")
    asset = first.ref.asset_id
    resolver.release(Holder("request", "r_alice"))
    ledger = resolver._ledger
    found = ledger.sweep_scan()  # Alice's lease row is gone; her removal is the scan's to offer
    assert [(item.asset_id, item.owner_key) for item in found] == [(asset, "alice")]
    bob = Holder("request", "r_bob")
    resolver.resolve_upload(person("bob"), PNG, holder=bob, destination=LOCAL, accepts=IMAGES)
    assert ledger.discard_unheld(found, assets.discard) == 0 and asset in assets.files, "Bob holds it now"
    resolver.release(bob)
    resolver.sweep()
    assert asset not in assets.files and assets.owners == {}, "both owners' claims are gone"
    assert resolver.sweep() == 0
    assert rows(db, "media_input_lease") == 0 and rows(db, "media_input_effect") == 0
    assert rows(db, "media_input_created") == 0


# Revert: the existence check in ``_ingest`` runs outside the transaction that writes the marker.
def test_a_removal_cannot_run_between_the_existence_check_and_the_marker(db, clock):
    resolver, assets, asset = released_upload(db, clock)
    ledger = resolver._ledger
    found = ledger.sweep_scan()
    errors: list[BaseException] = []

    def race():
        try:
            ledger.discard_unheld(found, assets.discard)
        except BaseException as exc:  # noqa: BLE001 - the test reports it below
            errors.append(exc)

    racer = threading.Thread(target=race)
    real_has_claim = assets.has_claim
    fired: list[bool] = []

    def check_then_race(asset_id, owner_key):
        result = real_has_claim(asset_id, owner_key)
        if not fired:
            fired.append(True)
            # The check runs inside the ledger's write transaction: no other writer can start one now.
            probe = sqlite3.connect(db, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    probe.execute("BEGIN IMMEDIATE")
            finally:
                probe.close()
            # The sweep's removal starts right after the check. It finishes before the marker if nothing orders them.
            racer.start()
            racer.join(0.5)
        return result

    assets.has_claim = check_then_race
    bob = Holder("request", "r_bob")
    resolver.resolve_upload(person("alice"), PNG, holder=bob, destination=LOCAL, accepts=IMAGES)
    racer.join()
    assets.has_claim = real_has_claim
    assert not errors, errors
    assert fired and asset in assets.files, "the ingest's file is held"
    resolver.release(bob)
    resolver.sweep()
    assert assets.files == {} and assets.owners == {}, "nothing is left"


# Revert: ``begin_effect_in`` owes a removal whenever another owner's removal is pending.
def test_an_ingest_never_owes_the_removal_of_a_claim_the_owner_already_held(db, clock):
    resolver, assets, asset = released_upload(db, clock)  # alice's upload, released, its removal pending
    assert assets.add(PNG, "bob") == asset  # bob keeps the same bytes in his own library
    bob = Holder("request", "r_bob")
    resolver.resolve_upload(person("bob"), PNG, holder=bob, destination=LOCAL, accepts=IMAGES)
    resolver.release(bob)
    resolver.sweep()
    assert asset in assets.files and assets.owners[asset] == {"bob"}, "bob's own claim stays; alice's upload goes"
    assert rows(db, "media_input_created") == 0


def test_an_erased_owners_upload_is_removed_by_the_sweep(db, clock):
    holder = Holder("job", "mj_erased")
    resolver, assets, resolved = lease_one(db, clock, holder, owner="alice")
    media_jobs.erase_prompt_derivatives({"alice"}, before=2**31, path=db)
    assert resolver.sweep() == 1 and resolved.ref.asset_id not in assets.files
    assert rows(db, "media_input_created") == 0


def test_a_failed_removal_keeps_its_duty_for_the_next_pass(db, clock):
    resolver, assets, resolved = lease_one(db, clock, REQUEST)
    resolver.release(REQUEST)
    real = assets.discard

    def fail(asset_id, owner_key):
        raise Crash

    with pytest.raises(Crash):
        resolver._ledger.sweep(fail)
    assert resolved.ref.asset_id in assets.files and rows(db, "media_input_created") == 1
    assert resolver._ledger.sweep(real) == 1 and resolved.ref.asset_id not in assets.files


def test_the_store_port_names_a_claim_only_for_an_owner_its_sidecar_lists(tmp_path: Path):
    files = {"img_" + "a" * 64: tmp_path / "a.png"}
    sidecars = {key: path.with_suffix(".json") for key, path in files.items()}
    asset = next(iter(files))
    port = CacheAssetPort(
        parse=lambda a: None, path_for=files.get, may_read=lambda a, auth: True, deliver=lambda p: p.read_bytes(),
        owner_key=lambda auth: None, strip=lambda data, suffix: data, store=lambda *a, **k: asset,
        read_metadata=lambda a: json.loads(sidecars[a].read_text()) if sidecars[a].exists() else {},
        write_metadata=lambda a, m: sidecars[a].write_text(json.dumps(m)), metadata_path=sidecars.get,
        lock=contextlib.nullcontext(),
    )
    assert not port.has_claim(asset, "alice") and not port.has_claim(asset, ""), "no file, no claim"
    files[asset].write_bytes(PNG)
    sidecars[asset].write_text(json.dumps({"owner_key_hashes": ["bob"]}))
    assert port.has_claim(asset, "bob") and not port.has_claim(asset, "alice")
    assert port.has_claim(asset, ""), "with no owner key, a stored file counts"
