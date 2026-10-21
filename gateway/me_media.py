"""Signed-in user media library and usage summary APIs.

This module is the user-facing projection over existing durable facts:
media jobs, generated asset handles, receipts, and the admin store's App
ownership. It does not execute media work and it does not read live services;
the session make routes it mounts live in ``gateway.me_make``.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.content_access import JobRecord, Own, ReceiptRecord, Viewer, decide

from gateway import directory_index, admin_store, mcp_oauth_store, request_connections, server_timing, thumbnails, me_make, media_jobs, media_metadata, ownership, prompt_memory, provenance, receipt_store, sqlite_startup
from gateway.admin_oidc import AdminSession
from gateway.model_ids import display_model_id
from gateway.public_failure import PublicFailure
from gateway.user_runtime import UserPortalError

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
SignedUrlBuilder = Callable[[Request, str, str], str]
AssetPathResolver = Callable[[str], Path | None]
AssetMediaTypeResolver = Callable[[str, Path | None], str]
AssetDeleter = Callable[[str], bool]
AssetCatalogProvider = Callable[[], Iterable[str]]
AssetMetadataReader = Callable[[str], Mapping[str, Any]]
PathProvider = Path | Callable[[], Path]

MEDIA_DEFAULT_LIMIT = 24
MEDIA_MAX_LIMIT = 100
USAGE_RECEIPT_LIMIT = 50

_KINDS_BY_PREFIX = {"img": "image", "vid": "video", "aud": "audio"}
_CAPABILITY_BY_INTENT_PREFIX = {
    "default": "chat",
    "chat": "chat",
    "image": "image",
    "video": "video",
    "audio": "audio",
    "speech": "voice",
    "stt": "transcribe",
    "transcription": "transcribe",
    "embedding": "embeddings",
    "rerank": "rerank",
    "decision": "decide",
}

_ASSET_SUFFIXES_BY_KIND = {
    "img": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "aud": (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"),
    "vid": (".mp4", ".webm", ".mov", ".mkv"),
}


@dataclass(frozen=True)
class _Actor:
    user: Mapping[str, Any]
    role: str

    @property
    def user_id(self) -> str:
        return str(self.user["id"])

    @property
    def owner(self) -> bool:
        return self.role in {"owner", "superadmin"}


def create_me_media_router(
    *,
    session_provider: SessionProvider,
    mutation_validator: MutationValidator | None = None,
    signed_url_builder: SignedUrlBuilder,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_deleter: AssetDeleter,
    asset_catalog_provider: AssetCatalogProvider | None = None,
    asset_metadata_reader: AssetMetadataReader | None = None,
    asset_storage_provider: StorageSnapshotProvider | None = None,
    admin_db_path: PathProvider = admin_store.DB_PATH,
    media_jobs_path: PathProvider = media_jobs.DB_PATH,
    receipts_path: PathProvider = receipt_store.DB_PATH,
    prompt_memory_path: PathProvider = prompt_memory.DB_PATH,
    make_runtime: me_make.MakeRuntime | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api", tags=["me"])

    def find_owned_asset(request: Request, user_id: str, asset_id: str) -> dict[str, Any] | None:
        return owned_library_asset(
            user_id, asset_id,
            asset_path_resolver=asset_path_resolver,
            asset_media_type_resolver=asset_media_type_resolver,
            asset_catalog_provider=asset_catalog_provider,
            asset_metadata_reader=asset_metadata_reader,
            admin_db_path=admin_db_path,
            media_jobs_path=media_jobs_path,
            receipts_path=receipts_path,
            prompt_memory_path=prompt_memory_path,
        )

    me_make.add_make_routes(
        router,
        session_provider=session_provider,
        mutation_validator=mutation_validator,
        find_owned_asset=find_owned_asset,
        admin_db_path=admin_db_path,
        media_jobs_path=media_jobs_path,
        receipts_path=receipts_path,
        runtime=make_runtime,
    )

    @router.get("/jobs", include_in_schema=False)
    def list_jobs(request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            # Even owners receive only their own jobs. Scope before pagination.
            scope = _member_media_scope(actor.user_id, admin_path)
            polled_at = int(time.time())
            watch = request.query_params.get("watch") == "1"
            since = request.query_params.get("since")
            if since is not None and (not since.isdecimal() or len(since) > 12):
                raise PublicFailure("invalid_since", 400, param="since", expected="Unix timestamp")
            # Inclusive seconds replay ties; capture the watermark before reading.
            updated_since = (int(since) if since is not None else polled_at) if watch else None
            rows = media_jobs.recent(
                101, app_ids=scope.app_ids, owner_key_hashes=list(scope.owner_keys or ()),
                before=_decode_cursor(request.query_params.get("cursor")),
                updated_since=updated_since, include_active=watch and since is None,
                path=_resolve_path(media_jobs_path),
            )
            page = rows[:100]
            cursor = _encode_cursor(page[-1]["updated_at"], page[-1]["job_id"]) if len(rows) > 100 else None
            return _json({"jobs": [me_make._client_view(job) for job in page],
                          "next_cursor": cursor, **({"polled_at": polled_at} if watch else {})})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/jobs/{job_id}", include_in_schema=False)
    def job_detail(job_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            scope = _member_media_scope(actor.user_id, admin_path)
            job = media_jobs.get(job_id, path=_resolve_path(media_jobs_path))
            if job is None or not _job_in_scope(job, scope):
                raise PublicFailure("job_not_found", 404, job_id=job_id)
            return _json({"job": me_make._client_view(job)})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/media", include_in_schema=False)
    def list_media(request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            prompt_path = _resolve_path(prompt_memory_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            scope = _media_scope(request, actor, admin_path)
            result = _list_media(
                request,
                scope=scope,
                signed_url_builder=signed_url_builder,
                asset_path_resolver=asset_path_resolver,
                asset_media_type_resolver=asset_media_type_resolver,
                asset_catalog_provider=asset_catalog_provider,
                asset_metadata_reader=asset_metadata_reader,
                admin_db_path=admin_path,
                media_jobs_path=jobs_path,
                receipts_path=receipt_path,
                prompt_memory_path=prompt_path,
            )
            # The folder is listed and counted once per Library, on the first page; later pages carry no state.
            storage = asset_storage_provider() if asset_storage_provider is not None else None
            result["media_folder"] = (
                _media_folder_view(actor, storage, admin_path=admin_path, jobs_path=jobs_path,
                                   receipts_path=receipt_path, prompt_path=prompt_path)
                if not request.query_params.get("cursor") else None
            )
            if scope.audit_everyone:
                _audit_everyone(actor, "media.library.everyone", admin_path)
            return _json(result)
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/media/{asset_id}", include_in_schema=False)
    def media_detail(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            prompt_path = _resolve_path(prompt_memory_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            scope = _media_scope(request, actor, admin_path)
            item = _find_asset(
                asset_id,
                scope=scope,
                request=request,
                signed_url_builder=signed_url_builder,
                asset_path_resolver=asset_path_resolver,
                asset_media_type_resolver=asset_media_type_resolver,
                asset_catalog_provider=asset_catalog_provider,
                asset_metadata_reader=asset_metadata_reader,
                admin_db_path=admin_path,
                media_jobs_path=jobs_path,
                receipts_path=receipt_path,
                prompt_memory_path=prompt_path,
            )
            if item is None:
                raise PublicFailure("asset_not_found", 404, asset_id=asset_id)
            if scope.audit_everyone:
                _audit_everyone(actor, "media.detail.everyone", admin_path)
            return _json({"asset": item})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.delete("/media/{asset_id}", include_in_schema=False)
    def delete_media(asset_id: str, request: Request) -> JSONResponse:
        try:
            session = session_provider(request)
            if session is not None and mutation_validator is not None:
                action_error = mutation_validator(request, session)
                if action_error is not None:
                    return action_error
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            prompt_path = _resolve_path(prompt_memory_path)
            actor = _actor_from_session(session, admin_path)
            scope = _member_media_scope(actor.user_id, admin_path)
            item = _find_asset(
                asset_id,
                scope=scope,
                request=request,
                signed_url_builder=signed_url_builder,
                asset_path_resolver=asset_path_resolver,
                asset_media_type_resolver=asset_media_type_resolver,
                asset_catalog_provider=asset_catalog_provider,
                asset_metadata_reader=asset_metadata_reader,
                admin_db_path=admin_path,
                media_jobs_path=jobs_path,
                receipts_path=receipt_path,
                prompt_memory_path=prompt_path,
            )
            if item is None:
                raise PublicFailure("asset_not_found", 404, asset_id=asset_id)
            physical_removed = asset_deleter(asset_id)
            prompt_memory.delete_asset_for_person(asset_id, actor.user_id, path=prompt_path)
            admin_store.record_audit(
                "media.asset.delete",
                actor_kind="user",
                actor_id=actor.user_id,
                actor_label=str(actor.user.get("email") or actor.user_id),
                target_kind="media_asset",
                target_id=asset_id,
                metadata={"physical_removed": bool(physical_removed), "job_id": item.get("job_id")},
                path=admin_path,
            )
            return _json({
                "asset_id": asset_id,
                "deleted": True,
                "physical_removed": bool(physical_removed),
            })
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.post("/media/confirm-folder", include_in_schema=False)
    def confirm_media_folder(request: Request) -> JSONResponse:
        """The owner's one-time confirmation that this folder is the Library's media folder.

        Refuses a folder that holds none of the Library's items while the Library has items: that is
        an unmounted disk or the wrong folder. Removes nothing.
        """
        try:
            session = session_provider(request)
            if session is not None and mutation_validator is not None:
                action_error = mutation_validator(request, session)
                if action_error is not None:
                    return action_error
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            actor = _actor_from_session(session, admin_path)
            if not actor.owner:
                raise PublicFailure("media_folder_owner_required", 403)
            storage = asset_storage_provider() if asset_storage_provider is not None else None
            if storage is None or storage.folder is None:
                raise PublicFailure("media_folder_unavailable", 503)
            stored, total, _uncheckable = _folder_counts(storage, admin_path=admin_path, jobs_path=jobs_path,
                                                         receipts_path=_resolve_path(receipts_path))
            if total and not stored:
                raise PublicFailure("media_folder_no_matches", 409)
            volume_id = prompt_memory.confirm_media_folder(
                storage.folder, owner=actor.owner, path=jobs_path, expected=storage.identity,
            )
            try:
                admin_store.record_audit(
                    "media.folder.confirm",
                    actor_kind="user",
                    actor_id=actor.user_id,
                    actor_label=str(actor.user.get("email") or actor.user_id),
                    target_kind="media_folder",
                    target_id=volume_id,
                    metadata={"library_items": total, "items_here": stored},
                    path=admin_path,
                )
            except Exception:  # the confirmation is committed; the audit row is a record, not the change
                logging.getLogger(__name__).exception("could not record the media folder confirmation")
            return _json({"confirmed": True})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.post("/media/remove-missing", include_in_schema=False)
    def remove_missing_media(request: Request) -> JSONResponse:
        """Drop the caller's Library and job entries whose files are gone.

        Refuses when the media folder cannot be read. It also refuses when the folder holds no media file
        and the caller has entries to drop: an empty folder is what an unmounted volume looks like, and
        there every entry would read as gone. A file counts as gone only when nothing in the folder holds
        its digest, under any suffix. Stored files and receipts are never touched.
        """
        try:
            session = session_provider(request)
            if session is not None and mutation_validator is not None:
                action_error = mutation_validator(request, session)
                if action_error is not None:
                    return action_error
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            prompt_path = _resolve_path(prompt_memory_path)
            actor = _actor_from_session(session, admin_path)
            storage = asset_storage_provider() if asset_storage_provider is not None else None
            if storage is None or storage.folder is None:
                raise PublicFailure("media_folder_unavailable", 503)
            scope = _member_media_scope(actor.user_id, admin_path)
            plan = _plan_removal(
                _scoped_jobs(scope, jobs_path=jobs_path, receipts_path=_resolve_path(receipts_path)),
                is_stored=lambda asset_id: _is_stored(asset_id, storage),
                already_hidden=(
                    _deleted_asset_ids(scope, admin_path),
                    media_jobs.hidden_assets(scope.user_ids or (), path=jobs_path),
                ),
            )
            if (plan.gone_assets or plan.job_ids) and not storage.digests:
                raise PublicFailure("media_folder_unavailable", 503, copy="media_folder_empty")
            if plan.gone_assets or plan.job_ids:
                removed_jobs = prompt_memory.remove_missing_media(
                    actor.user_id, plan.gone_assets, plan.hides, plan.job_ids,
                    path=jobs_path, media_folder=storage.folder, media_identity=storage.identity,
                    prompt_path=prompt_path,
                )
                counts = {"removed_assets": len(plan.gone_assets), "removed_jobs": removed_jobs}
                try:
                    admin_store.record_audit(
                        "media.library.remove_missing",
                        actor_kind="user",
                        actor_id=actor.user_id,
                        actor_label=str(actor.user.get("email") or actor.user_id),
                        target_kind="media_library",
                        target_id=actor.user_id,
                        metadata=counts,
                        path=admin_path,
                    )
                except Exception:  # the removal is committed; the summary is a record, not the removal
                    logging.getLogger(__name__).exception("could not record the remove-missing summary")
                return _json(counts)
            return _json({"removed_assets": 0, "removed_jobs": 0})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/media/{asset_id}/download", include_in_schema=False)
    def media_download(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            prompt_path = _resolve_path(prompt_memory_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            scope = _media_scope(request, actor, admin_path)
            item = _find_asset(
                asset_id,
                scope=scope,
                request=request,
                signed_url_builder=signed_url_builder,
                asset_path_resolver=asset_path_resolver,
                asset_media_type_resolver=asset_media_type_resolver,
                asset_catalog_provider=asset_catalog_provider,
                asset_metadata_reader=asset_metadata_reader,
                admin_db_path=admin_path,
                media_jobs_path=jobs_path,
                receipts_path=receipt_path,
                prompt_memory_path=prompt_path,
            )
            if item is None:
                raise PublicFailure("asset_not_found", 404, asset_id=asset_id)
            if item.get("available") is False:
                raise PublicFailure("asset_unavailable", 404, asset_id=asset_id, reason="file missing")
            mode = request.query_params.get("metadata", "none")
            if mode not in {"none", "full"}:
                raise PublicFailure("invalid_metadata", 400, param="metadata", accepted=["none", "full"])
            if mode == "full" and not item.get("workflow_download_url"):
                raise PublicFailure("prompt_not_accessible", 403, asset_id=asset_id)
            if scope.audit_everyone:
                _audit_everyone(actor, "media.download.everyone", admin_path)
            return _json({"asset_id": asset_id, "download_url": signed_url_builder(request, asset_id, mode)})
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/usage/summary", include_in_schema=False)
    def usage_summary(request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            actor = _actor_from_request(request, session_provider, admin_path)
            summary = _usage_summary(
                request,
                actor=actor,
                admin_db_path=admin_path,
                media_jobs_path=jobs_path,
                receipts_path=receipt_path,
                asset_catalog_provider=asset_catalog_provider,
                asset_metadata_reader=asset_metadata_reader,
                asset_path_resolver=asset_path_resolver,
            )
            return _json(summary)
        except (PublicFailure, UserPortalError) as exc:
            return _error_response(exc)

    return router


@dataclass(frozen=True)
class StorageSnapshot:
    """The media folder as it was listed once: the digest of every stored file, under any suffix."""

    digests: frozenset[str]
    folder: Path | None = None
    identity: tuple[int, int] | None = None


StorageSnapshotProvider = Callable[[], "StorageSnapshot | None"]


def create_storage_snapshot_provider(cache_dir: Path) -> StorageSnapshotProvider:
    """List the media folder, or return None when it cannot be seen.

    None covers a folder that is missing, not a directory, or unreadable. A folder that cannot be
    seen must never be read as "everything is gone". A readable folder is listed even when it holds
    no media file (empty, or only sidecars or scratch files): the removal decides what that means.
    """

    def snapshot() -> StorageSnapshot | None:
        try:
            if not cache_dir.is_dir():
                return None
            before = cache_dir.stat()
            names = os.listdir(cache_dir)
            after = cache_dir.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                return None
        except OSError:
            return None
        digests = frozenset(digest for digest in map(directory_index.stored_digest, names) if digest is not None)
        return StorageSnapshot(digests, cache_dir, (after.st_dev, after.st_ino))

    return snapshot


def _media_folder_view(
    actor: _Actor, storage: StorageSnapshot | None, *, admin_path: Path, jobs_path: Path, receipts_path: Path,
    prompt_path: Path,
) -> dict[str, Any] | None:
    """The folder's confirmation state for the Library. Only the owner sees where the folder is and what it holds.

    None when the folder cannot be seen: the Library's own read shows that failure.
    """
    if storage is None or storage.folder is None:
        return None
    try:
        state = prompt_memory.media_folder_state(
            storage.folder, path=jobs_path, prompt_path=prompt_path, expected=storage.identity,
        )
    except PublicFailure:
        return None
    if state == "confirmed" or not actor.owner:
        return {"state": state}
    stored, total, uncheckable = _folder_counts(storage, admin_path=admin_path, jobs_path=jobs_path,
                                                receipts_path=receipts_path)
    return {
        "state": state, "path": str(storage.folder), "library_items": total, "items_here": stored,
        "uncheckable_items": uncheckable,
    }


_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")


def _folder_counts(
    storage: StorageSnapshot, *, admin_path: Path, jobs_path: Path, receipts_path: Path,
) -> tuple[int, int, int]:
    """(items whose file is in the folder, checkable Library items, uncheckable Library items), across everyone.

    The Library items are the assets the finished jobs name, less the ones a person deleted. An id whose
    digest is not 64 hex characters cannot be matched to a file, so it is in neither count; the caller
    reports it apart, and a Library with only such ids is unverifiable rather than present.
    """
    deleted = _audited_deletions(admin_path)
    named: dict[str, None] = {}
    for job, _receipt in _scoped_jobs(_MediaScope(app_ids=None), jobs_path=jobs_path, receipts_path=receipts_path):
        for asset in _iter_job_assets(job):
            asset_id = str(asset.get("asset_id") or asset.get("id") or "")
            if asset_id and asset_id not in deleted:
                named[asset_id] = None
    checkable = [asset_id for asset_id in named if _is_checkable(asset_id)]
    stored = sum(1 for asset_id in checkable if _digest_of(asset_id) in storage.digests)
    return stored, len(checkable), len(named) - len(checkable)


def _is_checkable(asset_id: str) -> bool:
    return _HEX_DIGEST.fullmatch(_digest_of(asset_id)) is not None


def _digest_of(asset_id: str) -> str:
    return asset_id.split("_", 1)[1] if isinstance(asset_id, str) and "_" in asset_id else ""


def _is_stored(asset_id: str, storage: StorageSnapshot) -> bool:
    """Whether a file holds the asset's bytes. An id that cannot be checked counts as stored.

    A set lookup in the one listing; the folder is not scanned again for each asset. Removal uses this,
    so an id that cannot be checked is never removed.
    """
    digest = _digest_of(asset_id)
    return len(digest) != 64 or digest in storage.digests


@dataclass(frozen=True)
class _Removal:
    gone_assets: tuple[str, ...]
    hides: tuple[tuple[str, str], ...]
    job_ids: tuple[str, ...]


def _plan_removal(
    jobs: Iterable[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
    *,
    is_stored: Callable[[str], bool],
    already_hidden: tuple[set[str], set[tuple[str, str]]],
) -> _Removal:
    """What to remove, decided from each job's own record and the files, not from the Library listing.

    A job goes only when every asset its record names is gone. Otherwise each gone asset is
    hidden as that job made it.
    """
    deleted, hidden = already_hidden
    gone_assets: dict[str, None] = {}
    hides: list[tuple[str, str]] = []
    job_ids: list[str] = []
    for job, _receipt in jobs:
        if job.get("status") != media_jobs.STATUS_SUCCEEDED:
            continue
        named = [str(asset.get("asset_id") or asset.get("id") or "") for asset in _iter_job_assets(job)]
        named = [asset_id for asset_id in dict.fromkeys(named) if asset_id]
        if not named:
            continue
        gone = [asset_id for asset_id in named if not is_stored(asset_id)]
        job_id = str(job.get("job_id") or "")
        if not gone or not job_id:
            continue
        everything_gone = len(gone) == len(named)
        for asset_id in gone:
            if asset_id not in deleted and (asset_id, job_id) not in hidden:
                gone_assets[asset_id] = None
            if not everything_gone and (asset_id, job_id) not in hidden:
                hides.append((asset_id, job_id))
        if everything_gone:
            job_ids.append(job_id)
    return _Removal(tuple(gone_assets), tuple(hides), tuple(job_ids))


def _resolve_path(provider: PathProvider) -> Path:
    return provider() if callable(provider) else provider


@dataclass(frozen=True)
class _MediaScope:
    app_ids: list[str] | None
    person_id: str | None = None
    viewer_person_id: str | None = None
    user_ids: list[str] | None = None
    owner_keys: frozenset[str] | None = None
    audit_everyone: bool = False
    _app_viewer: Viewer = field(init=False, repr=False, compare=False)
    _key_viewer: Viewer = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # One immutable readable set per scope, reused across every receipt/job.
        object.__setattr__(self, "_app_viewer", Viewer(auth_kind="service_key", person_id=self.viewer_person_id,
                           owner_keys=frozenset(f"app:{app_id}" for app_id in (self.app_ids or ()))))
        object.__setattr__(self, "_key_viewer", Viewer(auth_kind="service_key", person_id=self.viewer_person_id,
                           owner_keys=self.owner_keys or frozenset()))


def _actor_from_request(
    request: Request,
    session_provider: SessionProvider,
    admin_db_path: Path,
) -> _Actor:
    session = session_provider(request)
    if session is None:
        raise PublicFailure("not_authenticated", 401)
    return _actor_from_session(session, admin_db_path)


def _actor_from_session(session: AdminSession | None, admin_db_path: Path) -> _Actor:
    if session is None:
        raise PublicFailure("not_authenticated", 401)
    user = admin_store.get_user(session.subject, path=admin_db_path)
    if user is None:
        raise PublicFailure("session_user_not_found", 401)
    role = _live_role_for_user(str(user["id"]), admin_db_path)
    return _Actor(user=user, role=role)


def create_asset_catalog_provider(cache_dir: Path) -> AssetCatalogProvider:
    memo: dict[str, tuple[frozenset[str], list[str]]] = {}

    def scan() -> list[str]:
        ids: set[str] = set()
        for meta_path in cache_dir.glob("*.meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            asset_id = data.get("asset_id") if isinstance(data, Mapping) else None
            if isinstance(asset_id, str) and _parse_asset_id(asset_id) is not None:
                ids.add(asset_id)
        for path in cache_dir.iterdir():
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            digest = path.stem
            if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
                continue
            suffix = path.suffix.lower()
            for kind, suffixes in _ASSET_SUFFIXES_BY_KIND.items():
                if suffix in suffixes:
                    ids.add(f"{kind}_{digest}")
                    break
        return sorted(ids)

    def catalog() -> list[str]:
        if not cache_dir.exists():
            return []
        # The ids follow from the file names (and the asset id each sidecar holds, which never
        # changes), so while the names are unchanged the sidecars are not read again.
        listed = directory_index.names(cache_dir)
        if listed is None:
            return scan()
        hit = memo.get("ids")
        if hit is not None and hit[0] is listed:  # the same listing object: nothing changed
            return list(hit[1])
        ids = scan()
        memo["ids"] = (listed, ids)
        return list(ids)

    return catalog


def conservative_asset_deleter(
    *,
    metadata_reader: AssetMetadataReader,
    asset_path_resolver: AssetPathResolver,
    metadata_path_resolver: Callable[[str], Path | None],
    prompt_memory_path: PathProvider | None = None,
) -> AssetDeleter:
    def delete(asset_id: str) -> bool:
        from gateway.prompt_metadata_migration import CACHE_MUTATION_LOCK
        removed = False
        # Asset storage records owners and makers under this lock. Check them
        # under it too, so an owner added after a check cannot lose the file.
        with CACHE_MUTATION_LOCK:
            metadata = metadata_reader(asset_id)
            owners = {
                str(owner)
                for owner in metadata.get("owner_key_hashes") or []
                if isinstance(owner, str)
            }
            if len(owners) > 1 or len(metadata.get("maker_person_ids") or []) > 1:
                return False
            source = asset_path_resolver(asset_id)
            thumbnails.remove_thumbnail(source)
            for path in (source, metadata_path_resolver(asset_id)):
                if path is not None and path.exists():
                    path.unlink()
                    removed = True
            if removed:
                prompt_memory.delete_asset(asset_id, path=_resolve_path(prompt_memory_path) if prompt_memory_path is not None else None)
        return removed

    return delete


def _parse_asset_id(asset_id: str) -> tuple[str, str] | None:
    if not isinstance(asset_id, str) or "_" not in asset_id:
        return None
    kind, digest = asset_id.split("_", 1)
    if kind not in _ASSET_SUFFIXES_BY_KIND:
        return None
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return None
    return kind, digest


def _live_role_for_user(user_id: str, path: Path) -> str:
    roles = admin_store.effective_admin_roles("user", user_id, path=path)
    for role in ("owner", "superadmin", "admin"):
        if role in roles:
            return role
    return "none"


def _owned_app_ids(user_id: str, path: Path) -> list[str]:
    return [
        str(app["id"])
        for app in admin_store.list_apps_for_user(user_id, path=path)
        if app.get("id")
    ]


def _media_scope(request: Request, actor: _Actor, admin_db_path: Path) -> _MediaScope:
    raw_scope = request.query_params.get("scope") or request.query_params.get("owner")
    person = request.query_params.get("person")
    if person:
        if person in {"self", actor.user_id}:
            return _member_media_scope(actor.user_id, admin_db_path, person_id=actor.user_id)
        if not actor.owner:
            raise PublicFailure("owner_required", 403, param="person")
        if person == "all":
            # OWNER_VISIBILITY_STEP3: remove the owner-everyone selector.
            return _MediaScope(app_ids=None, person_id="all", viewer_person_id=actor.user_id, user_ids=None, audit_everyone=True)
        return _member_media_scope(person, admin_db_path, person_id=person, viewer_person_id=actor.user_id)
    # Everyone starts with their own work; "everyone" is an explicit owner choice.
    scope = str(raw_scope or "me")
    if scope == "everyone":
        # OWNER_VISIBILITY_STEP3: remove the owner-everyone selector.
        if not actor.owner:
            raise PublicFailure("owner_required", 403, param="scope")
        return _MediaScope(app_ids=None, viewer_person_id=actor.user_id, user_ids=None, audit_everyone=True)
    return _member_media_scope(actor.user_id, admin_db_path)


def _scope_requested(request: Request) -> bool:
    return any(request.query_params.get(name) for name in ("scope", "owner", "person"))


def _member_media_scope(user_id: str, admin_db_path: Path, *, person_id: str | None = None, viewer_person_id: str | None = None) -> _MediaScope:
    scope = _MediaScope(
        app_ids=_owned_app_ids(user_id, admin_db_path),
        person_id=person_id or user_id,
        viewer_person_id=viewer_person_id or user_id,
        user_ids=[user_id],
    )
    return _MediaScope(
        app_ids=scope.app_ids,
        person_id=scope.person_id,
        viewer_person_id=scope.viewer_person_id,
        user_ids=scope.user_ids,
        owner_keys=frozenset(_credential_hashes_for_scope(scope, admin_db_path) or ()),
    )


def _list_media(
    request: Request,
    *,
    scope: _MediaScope,
    signed_url_builder: SignedUrlBuilder,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_catalog_provider: AssetCatalogProvider | None,
    asset_metadata_reader: AssetMetadataReader | None,
    admin_db_path: Path,
    media_jobs_path: Path,
    receipts_path: Path,
    prompt_memory_path: Path,
) -> dict[str, Any]:
    limit = _bounded_int(request.query_params.get("limit"), MEDIA_DEFAULT_LIMIT, MEDIA_MAX_LIMIT)
    cursor = _decode_cursor(request.query_params.get("cursor"))
    sources = _media_sources(scope, admin_db_path)
    source_filter = _media_source_filter(request, scope)
    with server_timing.span("db"):
        entries = _media_entries(
            request=request,
            scope=scope,
            signed_url_builder=signed_url_builder,
            asset_path_resolver=asset_path_resolver,
            asset_media_type_resolver=asset_media_type_resolver,
            asset_catalog_provider=asset_catalog_provider,
            asset_metadata_reader=asset_metadata_reader,
            admin_db_path=admin_db_path,
            media_jobs_path=media_jobs_path,
            receipts_path=receipts_path,
            prompt_memory_path=prompt_memory_path,
        )
    filtered = [entry for entry in entries if _entry_matches(entry, request)]
    if source_filter is not None:
        filtered = [entry for entry in filtered if entry.source_id == source_filter]
    if cursor:
        filtered = [entry for entry in filtered if entry.sort_key < cursor]
    page = filtered[: limit + 1]
    next_cursor = None
    if len(page) > limit:
        last = page[limit - 1]
        next_cursor = _encode_cursor(*last.sort_key)
        page = page[:limit]
    with server_timing.span("build"):
        assets = [entry.build(False) for entry in page]
    return {
        "schema_version": 1,
        "authenticated": True,
        "scope": "everyone" if scope.app_ids is None else "me",
        "default_scope": "explicit" if _scope_requested(request) else "self",
        "source": source_filter,
        "sources": sources,
        "assets": assets,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "models": sorted({entry.model for entry in entries if entry.model}),
    }


def owned_library_asset(
    user_id: str,
    asset_id: str,
    *,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_catalog_provider: AssetCatalogProvider | None,
    asset_metadata_reader: AssetMetadataReader | None,
    admin_db_path: PathProvider,
    media_jobs_path: PathProvider,
    receipts_path: PathProvider,
    prompt_memory_path: PathProvider,
) -> dict[str, Any] | None:
    """Return the person's Library item for an asset, or None.

    None also covers an asset the person deleted. The item carries no
    download links, so it works off the HTTP path (for example, in a job).
    """
    admin_path = _resolve_path(admin_db_path)
    return _find_asset(
        asset_id,
        scope=_member_media_scope(user_id, admin_path),
        request=None,  # type: ignore[arg-type]
        signed_url_builder=lambda request, asset_id, mode: None,  # type: ignore[arg-type,return-value]
        asset_path_resolver=asset_path_resolver,
        asset_media_type_resolver=asset_media_type_resolver,
        asset_catalog_provider=asset_catalog_provider,
        asset_metadata_reader=asset_metadata_reader,
        admin_db_path=admin_path,
        media_jobs_path=_resolve_path(media_jobs_path),
        receipts_path=_resolve_path(receipts_path),
        prompt_memory_path=_resolve_path(prompt_memory_path),
    )


def _find_asset(
    asset_id: str,
    *,
    scope: _MediaScope,
    request: Request,
    signed_url_builder: SignedUrlBuilder,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_catalog_provider: AssetCatalogProvider | None,
    asset_metadata_reader: AssetMetadataReader | None,
    admin_db_path: Path,
    media_jobs_path: Path,
    receipts_path: Path,
    prompt_memory_path: Path,
) -> dict[str, Any] | None:
    for entry in _media_entries(
        request=request,
        scope=scope,
        signed_url_builder=signed_url_builder,
        asset_path_resolver=asset_path_resolver,
        asset_media_type_resolver=asset_media_type_resolver,
        asset_catalog_provider=asset_catalog_provider,
        asset_metadata_reader=asset_metadata_reader,
        admin_db_path=admin_db_path,
        media_jobs_path=media_jobs_path,
        receipts_path=receipts_path,
        prompt_memory_path=prompt_memory_path,
    ):
        if entry.asset_id == asset_id:
            return entry.build(True)
    return None


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_EMBEDDED_MEMO: dict[tuple[str, int, int], dict[str, Any]] = {}
_EMBEDDED_MEMO_MAX = 4096


def _embedded_provenance_cached(path: Path | None, *, probe: bool) -> dict[str, Any]:
    """Embedded provenance, remembered per file version (path, mtime, size).

    ``probe=False`` skips audio and video, whose tags need an ``ffprobe``
    process (tens of milliseconds each). The result is read-only.
    """
    if path is None:
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _EMBEDDED_MEMO.get(key)
    if hit is not None:
        return hit
    if not probe and path.suffix.lower() not in _IMAGE_SUFFIXES:
        return {}
    value = _embedded_provenance(path)
    if len(_EMBEDDED_MEMO) >= _EMBEDDED_MEMO_MAX:
        _EMBEDDED_MEMO.clear()
    _EMBEDDED_MEMO[key] = value
    return value


class _Apps:
    """Apps and credential-to-app links, read once for a whole listing."""

    def __init__(self, admin_db_path: Path) -> None:
        self._path = admin_db_path
        self._by_id: dict[str, dict[str, Any]] | None = None
        self._by_hash: dict[str, dict[str, Any]] | None = None

    def get(self, app_id: str) -> dict[str, Any] | None:
        if self._by_id is None:
            self._by_id = {str(app["id"]): app for app in admin_store.list_apps(path=self._path) if app.get("id")}
        return self._by_id.get(app_id)

    def by_hash(self) -> dict[str, dict[str, Any]]:
        if self._by_hash is None:
            self._by_hash = _credential_app_by_hash(self._path)
        return self._by_hash


@dataclass(frozen=True)
class _Entry:
    """What filtering, sorting and paging need, without opening the file.

    ``build(probe)`` makes the full Library item. ``probe=True`` also reads
    audio and video tags with ``ffprobe``; the list leaves that to the detail view.
    """

    asset_id: str
    kind: str
    created_at: int | None
    model: str | None
    workflow: str | None
    prompt: str | None
    source_id: str | None
    build: Callable[[bool], dict[str, Any]]

    @property
    def sort_key(self) -> tuple[int, str]:
        return (int(self.created_at or 0), self.asset_id)


def _media_items(**kwargs: Any) -> list[dict[str, Any]]:
    return [entry.build(True) for entry in _media_entries(**kwargs)]


def _scoped_jobs(
    scope: _MediaScope, *, jobs_path: Path, receipts_path: Path,
) -> list[tuple[dict[str, Any], Mapping[str, Any] | None]]:
    """Every finished job in scope, newest first, with its receipt. Reads the jobs in one query and their receipts in a few."""
    jobs = media_jobs.recent(
        limit=media_jobs.count(path=jobs_path),
        audience="internal",
        status=media_jobs.STATUS_SUCCEEDED,
        path=jobs_path,
    )
    receipts = receipt_store.get_many(
        [request_id for job in jobs for request_id in _job_receipt_ids(job)],
        audience="internal", path=receipts_path,
    )
    scoped = []
    for job in jobs:
        receipt = next((receipts[key] for key in _job_receipt_ids(job) if key in receipts), None)
        if receipt is not None and not _receipt_in_scope(receipt, scope):
            continue
        if receipt is None and not _job_in_scope(job, scope):
            continue
        scoped.append((job, receipt))
    return scoped


def _media_entries(
    *,
    request: Request,
    scope: _MediaScope,
    signed_url_builder: SignedUrlBuilder,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_catalog_provider: AssetCatalogProvider | None,
    asset_metadata_reader: AssetMetadataReader | None,
    admin_db_path: Path,
    media_jobs_path: Path,
    receipts_path: Path,
    prompt_memory_path: Path,
) -> list[_Entry]:
    """Every Library asset in scope, newest first, as cheap entries.

    Reads the jobs in one query and their receipts in a few, so the query
    count does not grow with the Library. Files are not opened here.
    """
    apps = _Apps(admin_db_path)
    out: list[_Entry] = []
    seen: set[str] = set()
    prompts = prompt_memory.get_person_prompts(scope.viewer_person_id, path=prompt_memory_path)
    deleted = _deleted_asset_ids(scope, admin_db_path)
    hidden = media_jobs.hidden_assets(scope.user_ids, path=media_jobs_path) if scope.user_ids else set()
    for job, receipt in _scoped_jobs(scope, jobs_path=media_jobs_path, receipts_path=receipts_path):
        job_source = None
        for asset in _iter_job_assets(job):
            asset_id = str(asset.get("asset_id") or asset.get("id") or "")
            if not asset_id or asset_id in seen or asset_id in deleted or (asset_id, job.get("job_id")) in hidden:
                continue
            seen.add(asset_id)
            if job_source is None:
                job_source = _source_projection(receipt, admin_db_path, metadata=job, visible_app_ids=scope.app_ids, apps=apps)
            prompt_record = prompts.get(asset_id)
            path = asset_path_resolver(asset_id)
            created_at = _asset_created_at(path, job, receipt, _embedded_provenance_cached(path, probe=False))
            out.append(_Entry(
                asset_id=asset_id,
                kind=_asset_kind(asset_id, asset),
                created_at=created_at,
                model=_model_for_asset(asset, job, receipt),
                workflow=_workflow_for_asset(asset, job, receipt),
                prompt=prompt_record.get("prompt_text") if prompt_record else None,
                source_id=_source_id(job_source),
                build=partial(
                    _build_job_item, asset_id=asset_id, asset=asset, job=job, receipt=receipt,
                    created_at=created_at, prompt_record=prompt_record, source=job_source, request=request,
                    signed_url_builder=signed_url_builder, asset_path_resolver=asset_path_resolver,
                    asset_media_type_resolver=asset_media_type_resolver,
                    asset_metadata_reader=asset_metadata_reader, admin_db_path=admin_db_path,
                ),
            ))
    if asset_catalog_provider is not None and asset_metadata_reader is not None:
        credential_hashes = _credential_hashes_for_scope(scope, admin_db_path)
        for asset_id in asset_catalog_provider():
            asset_id = str(asset_id or "")
            if not asset_id or asset_id in seen or asset_id in deleted:
                continue
            metadata = dict(asset_metadata_reader(asset_id) or {})
            owners = {str(value) for value in metadata.get("owner_key_hashes") or [] if isinstance(value, str)}
            if credential_hashes is not None and not (owners & credential_hashes):
                continue
            path = asset_path_resolver(asset_id)
            if path is None or not path.exists():
                continue
            embedded = _embedded_provenance_cached(path, probe=False)
            receipt = _receipt_for_embedded_cache_asset(embedded, receipts_path)
            if not _receipt_in_scope(receipt, scope) and receipt is not None:
                continue
            kind = _asset_kind(asset_id, metadata)
            created_at = _asset_created_at(path, {}, None, embedded)
            prompt_record = prompts.get(asset_id)
            source = _source_projection(receipt, admin_db_path, metadata=metadata, embedded=embedded,
                                        visible_app_ids=scope.app_ids, apps=apps)
            seen.add(asset_id)
            out.append(_Entry(
                asset_id=asset_id,
                kind=kind,
                created_at=created_at,
                model=_string_first(embedded.get("resolved_model"), embedded.get("requested_model")),
                workflow=_string_first(embedded.get("workflow_id"), embedded.get("workflow")),
                prompt=prompt_record.get("prompt_text") if prompt_record else None,
                source_id=_source_id(source),
                build=partial(
                    _build_cache_item, asset_id=asset_id, path=path, metadata=metadata, receipt=receipt,
                    kind=kind, created_at=created_at, prompt_record=prompt_record, source=source,
                    request=request, signed_url_builder=signed_url_builder,
                    asset_media_type_resolver=asset_media_type_resolver,
                ),
            ))
    return sorted(out, key=lambda entry: entry.sort_key, reverse=True)


def _build_job_item(
    probe: bool,
    *,
    asset_id: str,
    asset: Mapping[str, Any],
    job: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    created_at: int | None,
    prompt_record: Mapping[str, Any] | None,
    source: dict[str, Any],
    request: Request,
    signed_url_builder: SignedUrlBuilder,
    asset_path_resolver: AssetPathResolver,
    asset_media_type_resolver: AssetMediaTypeResolver,
    asset_metadata_reader: AssetMetadataReader | None,
    admin_db_path: Path,
) -> dict[str, Any]:
    kind = _asset_kind(asset_id, asset)
    path = asset_path_resolver(asset_id)
    embedded = _embedded_provenance_cached(path, probe=probe)
    media_type = str(asset.get("media_type") or asset_media_type_resolver(asset_id, path))
    sidecar = dict(asset_metadata_reader(asset_id) or {}) if asset_metadata_reader else {}
    prompt = prompt_record.get("prompt_text") if prompt_record else None
    model = _string_first(
        _model_for_asset(asset, job, receipt),
        embedded.get("resolved_model"),
        embedded.get("requested_model"),
    )
    workflow = _string_first(
        _workflow_for_asset(asset, job, receipt),
        embedded.get("workflow_id"),
        embedded.get("workflow"),
    )
    makers = sidecar.get("maker_person_ids")
    if makers and len(makers) == 1:
        user = admin_store.get_user(makers[0], path=admin_db_path)
        source = {**source, "label": str((user or {}).get("display_name") or (user or {}).get("email") or "Unknown maker"), "person_id": makers[0]}
    available = _file_is_stored(path)
    return {
        "asset_id": asset_id,
        "id": asset_id,
        "kind": kind,
        "media_type": media_type,
        "size_bytes": int(path.stat().st_size) if path and path.exists() else None,
        "created_at": created_at,
        "file_modified_at": _file_modified_at(path) if created_at is None else None,
        "updated_at": job.get("updated_at") or created_at,
        "job_id": job.get("job_id"),
        "request_id": job.get("request_id"),
        "status": job.get("status"),
        "available": available,
        "prompt": prompt,
        "title": _media_title(prompt, kind, created_at, source),
        "model": model,
        "workflow": workflow,
        "seed": _first_present(asset.get("seed"), _nested(job, "metadata", "seed"), _nested(receipt, "metadata", "seed"), embedded.get("seed")),
        "width": _first_present(asset.get("width"), _nested(job, "metadata", "width"), _nested(embedded, "parameters", "width")),
        "height": _first_present(asset.get("height"), _nested(job, "metadata", "height"), _nested(embedded, "parameters", "height")),
        "duration_s": _first_present(asset.get("duration_s"), asset.get("duration"), _nested(job, "metadata", "duration_s"), _nested(embedded, "parameters", "duration_s")),
        "poster_asset_id": asset.get("poster_asset_id"),
        "download_url": signed_url_builder(request, asset_id, "none") if available else None,
        "workflow_download_url": signed_url_builder(request, asset_id, "full") if available and prompt_record else None,
        "thumbnail_url": signed_url_builder(request, asset_id, thumbnails.THUMB_MODE) if available and thumbnails.can_thumbnail(path) else None,
        "generation_ms": receipt.get("latency_ms") if receipt else None,
        "provenance": _provenance(asset, job, receipt, embedded, created_at=created_at, prompt=prompt),
        "source": source,
        "unavailable_reason": None if available else "Asset file is missing or expired.",
    }


def _build_cache_item(
    probe: bool,
    *,
    asset_id: str,
    path: Path,
    metadata: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    kind: str,
    created_at: int | None,
    prompt_record: Mapping[str, Any] | None,
    source: dict[str, Any],
    request: Request,
    signed_url_builder: SignedUrlBuilder,
    asset_media_type_resolver: AssetMediaTypeResolver,
) -> dict[str, Any]:
    embedded = _embedded_provenance_cached(path, probe=probe)
    prompt = prompt_record.get("prompt_text") if prompt_record else None
    return {
        "asset_id": asset_id,
        "id": asset_id,
        "kind": kind,
        "media_type": asset_media_type_resolver(asset_id, path),
        "size_bytes": int(path.stat().st_size),
        "created_at": created_at,
        "file_modified_at": _file_modified_at(path) if created_at is None else None,
        "updated_at": int(metadata.get("updated_at") or path.stat().st_mtime),
        "job_id": None,
        "request_id": (receipt or embedded).get("request_id"),
        "status": "available",
        "prompt": prompt,
        "title": _media_title(prompt, kind, created_at, source),
        "model": _string_first(embedded.get("resolved_model"), embedded.get("requested_model")),
        "workflow": _string_first(embedded.get("workflow_id"), embedded.get("workflow")),
        "seed": embedded.get("seed"),
        "width": _nested(embedded, "parameters", "width"),
        "height": _nested(embedded, "parameters", "height"),
        "duration_s": _nested(embedded, "parameters", "duration_s"),
        "poster_asset_id": None,
        "download_url": signed_url_builder(request, asset_id, "none"),
        "workflow_download_url": signed_url_builder(request, asset_id, "full") if prompt_record else None,
        "thumbnail_url": signed_url_builder(request, asset_id, thumbnails.THUMB_MODE) if thumbnails.can_thumbnail(path) else None,
        "generation_ms": None,
        "provenance": {
            "prompt": prompt,
            "model": _string_first(embedded.get("resolved_model"), embedded.get("requested_model")),
            "workflow": _string_first(embedded.get("workflow_id"), embedded.get("workflow")),
            "seed": embedded.get("seed"),
            "size": {
                "width": _nested(embedded, "parameters", "width"),
                "height": _nested(embedded, "parameters", "height"),
            },
            "created_at": created_at,
            "request_id": (receipt or embedded).get("request_id"),
            "job_id": None,
            "source": "asset_cache",
            "complete": bool(embedded),
        },
        "source": source,
    }


def _job_receipt_ids(job: Mapping[str, Any]) -> list[str]:
    """Try immutable creating provenance first, then legacy lookup candidates.

    Request IDs can be derived or reused; none is a receipt until lookup finds
    it. The durable creating link survives replay release and prompt erasure.
    """
    ids = [str(job.get(name) or "") for name in
           ("creating_receipt_id", "parent_request_id", "request_id")]
    return [value for index, value in enumerate(ids)
            if value and not value.startswith("imgjob_seeded_") and value not in ids[:index]]


def _receipts_by_request_id(request_ids: Iterable[str], *, path: Path) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for request_id in {value for value in request_ids if value}:
        receipt = receipt_store.get(request_id, audience="internal", path=path)
        if receipt is not None:
            out[request_id] = receipt
    return out


def _receipt_for_embedded_cache_asset(embedded: Mapping[str, Any], receipts_path: Path) -> Mapping[str, Any] | None:
    request_id = embedded.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    return receipt_store.get(request_id, audience="internal", path=receipts_path)


def _credential_hashes_for_scope(scope: _MediaScope, path: Path) -> set[str] | None:
    if scope.user_ids is None:
        return None
    principals = {
        owner
        for owner in (
            *(ownership.principal_key("app", app_id) for app_id in scope.app_ids or []),
            *(ownership.principal_key("user", user_id) for user_id in scope.user_ids),
        )
        if owner is not None
    }
    hashes = set(principals)
    for credential in admin_store.list_credentials(path=path):
        if ownership.principal_key_for_credential(credential) not in principals:
            continue
        key_hash = credential.get("hash")
        if isinstance(key_hash, str) and key_hash:
            hashes.add(key_hash)
    return hashes


def _credential_app_by_hash(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    apps = {
        str(app.get("id") or ""): app
        for app in admin_store.list_apps(path=path)
        if app.get("id")
    }
    for app_id, app in apps.items():
        principal = ownership.principal_key("app", app_id)
        if principal is not None:
            out[principal] = app
    for credential in admin_store.list_credentials(path=path):
        key_hash = credential.get("hash")
        principal = ownership.principal_key_for_credential(credential)
        app = apps.get(principal.removeprefix("app:") if isinstance(principal, str) and principal.startswith("app:") else "")
        if not isinstance(key_hash, str) or not key_hash or app is None:
            continue
        out[key_hash] = app
    return out


def _deleted_asset_ids(scope: _MediaScope, path: Path) -> set[str]:
    if scope.user_ids is None:
        return set()
    deleted: set[str] = set()
    for user_id in scope.user_ids:
        deleted |= _audited_deletions(path, actor_id=user_id)
    return deleted


def _audited_deletions(path: Path, *, actor_id: str | None = None) -> set[str]:
    """Every asset a person deleted, read from the audit trail. Everyone's when no actor is named."""
    deleted: set[str] = set()
    cursor: dict[str, Any] = {}
    while True:
        events = admin_store.list_audit(
            actor_id=actor_id, action_prefix="media.asset.delete",
            limit=500, path=path, **cursor,
        )
        deleted.update(str(event["target_id"]) for event in events if event.get("target_id"))
        if len(events) < 500:
            break
        last = events[-1]
        cursor = {"before_created_at": last["created_at"], "before_id": last["id"]}
    return deleted


def _embedded_provenance(path: Path | None) -> dict[str, Any]:
    # Images use PNG text or EXIF. Audio/video use the existing container-tag
    # contract. Limit byte reads; large files can still be downloaded normally.
    if path is None or not path.is_file():
        return {}
    try:
        if path.suffix.lower() == ".png":
            if path.stat().st_size > 32 * 1024 * 1024:
                return {}
            chunks = media_metadata.png_text_chunks_from_path(path)
            raw = provenance.read_provenance_value(chunks)
            workflow = chunks.get("workflow")
        elif path.suffix.lower() in {".jpg", ".jpeg", ".webp"}:
            from PIL import Image
            with Image.open(path) as image:
                exif = image.getexif()
                description = str(exif.get(0x010E, ""))
                raw = provenance.read_provenance_lines(description, ":")
                workflow = str(exif.get(0x010F, ""))
        else:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_entries",
                 "format_tags:stream_tags", str(path)],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if probe.returncode or len(probe.stdout) > 1024 * 1024:
                return {}
            facts = json.loads(probe.stdout)
            tags = {}
            for stream in facts.get("streams", []) + [facts.get("format", {})]:
                tags.update({k.lower(): v for k, v in stream.get("tags", {}).items()})
            raw = provenance.read_provenance_value(tags)
            if raw is None:
                raw = provenance.read_provenance_lines(str(tags.get("comment", "")), "=")
            workflow = None
        data = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(data, dict):
            return {}
        data = provenance.sanitize_provenance(data)
        data["workflow_available"] = bool(workflow) and path.suffix.lower() == ".png"
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired, media_metadata.MediaMetadataError):
        return {}


def _record_in_scope(record: Mapping[str, Any] | None, scope: _MediaScope, *, job: bool) -> bool:
    if scope.app_ids is None:
        # OWNER_VISIBILITY_STEP3: delete owner-everyone / unscoped collection access.
        return True
    if record is None:
        return False
    if record.get("app_id"):
        principals = frozenset({f"app:{record['app_id']}"})
        # The existing Library scope treats an explicit App as authoritative.
        viewer = scope._app_viewer
    else:
        owner = record.get("owner_key_hash")
        principals = frozenset({owner}) if isinstance(owner, str) else frozenset()
        viewer = scope._key_viewer
    content = JobRecord(principals) if job else ReceiptRecord(principals)
    return isinstance(decide(viewer, content, "read"), Own)


def _receipt_in_scope(receipt: Mapping[str, Any] | None, scope: _MediaScope) -> bool:
    return _record_in_scope(receipt, scope, job=False)


def _job_in_scope(job: Mapping[str, Any], scope: _MediaScope) -> bool:
    return _record_in_scope(job, scope, job=True)


# Nothing recorded where the request came from.
_UNKNOWN_SOURCE = {"kind": "unknown", "label": "Source unknown", "profile": None, "client_hint": None}


def _source_projection(
    receipt: Mapping[str, Any] | None,
    admin_db_path: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
    embedded: Mapping[str, Any] | None = None,
    visible_app_ids: list[str] | None = None,
    apps: _Apps | None = None,
    connections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    def lookup(app_id: str) -> dict[str, Any] | None:
        return apps.get(app_id) if apps is not None else admin_store.get_app(app_id, path=admin_db_path)

    # A request an MCP sign-in made runs as its person's session App; the
    # connection is the source, so it is not blended into the person's own use.
    connection = (connections or {}).get(str((receipt or {}).get("connection_id") or ""))
    if connection is not None:
        return {
            "kind": "connection",
            "connection_id": connection["connection_id"],
            "label": str(connection["label"]),
            "profile": (receipt or {}).get("app_profile"),
            "client_hint": _client_hint(receipt),
        }
    app_id = str((receipt or {}).get("app_id") or "")
    if app_id:
        app = lookup(app_id)
        if app is not None:
            return {
                "kind": "app",
                "app_id": app_id,
                "label": str(app.get("name") or app_id),
                "profile": (receipt or {}).get("app_profile"),
                "client_hint": _client_hint(receipt),
            }
        return {"kind": "app", "app_id": app_id, "label": "Unknown app", "profile": (receipt or {}).get("app_profile"), "client_hint": _client_hint(receipt)}
    receipt_owner_app = _app_from_asset_owner_hashes(receipt, admin_db_path, visible_app_ids=visible_app_ids, apps=apps)
    if receipt_owner_app is not None:
        recovered_app_id = str(receipt_owner_app.get("id") or "")
        return {
            "kind": "app",
            "app_id": recovered_app_id,
            "label": str(receipt_owner_app.get("name") or recovered_app_id or "App"),
            "profile": None,
            "client_hint": _client_hint(receipt),
            "recovered_from": "receipt_owner_key",
        }
    profile = str((receipt or {}).get("app_profile") or "")
    if receipt is None:
        trusted_app_id = str((metadata or {}).get("app_id") or "")
        if trusted_app_id and (visible_app_ids is None or trusted_app_id in set(visible_app_ids)):
            app = lookup(trusted_app_id)
            if app is not None:
                return {
                    "kind": "app",
                    "app_id": trusted_app_id,
                    "label": str(app.get("name") or trusted_app_id),
                    "profile": None,
                    "client_hint": None,
                    "recovered_from": "media_job",
                }
        app = _app_from_asset_owner_hashes(metadata, admin_db_path, visible_app_ids=visible_app_ids, apps=apps)
        if app is not None:
            recovered_app_id = str(app.get("id") or "")
            return {
                "kind": "app",
                "app_id": recovered_app_id,
                "label": str(app.get("name") or recovered_app_id or "App"),
                "profile": None,
                "client_hint": None,
                "recovered_from": "asset_owner_key",
            }
        return dict(_UNKNOWN_SOURCE, profile=profile or None)
    if not receipt.get("source_kind"):  # Older receipts did not record where a request came from.
        return dict(_UNKNOWN_SOURCE, profile=profile or None)
    hint = _client_hint(receipt)
    base_label = str((receipt or {}).get("source_label") or "Local network · no key")
    return {
        "kind": "local-network",
        "label": f"{base_label} · {hint}" if hint and hint not in base_label else base_label,
        "profile": profile or None,
        "client_hint": hint,
    }


def _app_from_asset_owner_hashes(
    metadata: Mapping[str, Any] | None,
    admin_db_path: Path,
    *,
    visible_app_ids: list[str] | None,
    apps: _Apps | None = None,
) -> Mapping[str, Any] | None:
    raw_owners = list((metadata or {}).get("owner_key_hashes") or [])
    single_owner = (metadata or {}).get("owner_key_hash")
    if isinstance(single_owner, str):
        raw_owners.append(single_owner)
    owners = [str(value) for value in raw_owners if isinstance(value, str)]
    if not owners:
        return None
    visible = set(visible_app_ids) if visible_app_ids is not None else None
    by_hash = apps.by_hash() if apps is not None else _credential_app_by_hash(admin_db_path)
    for owner_hash in owners:
        app = by_hash.get(owner_hash)
        if app is not None and (visible is None or str(app.get("id") or "") in visible):
            return app
    return None


def _source_id(source: Any) -> str | None:
    if not isinstance(source, Mapping):
        return None
    kind = source.get("kind")
    if kind == "app":
        app_id = source.get("app_id")
        return f"app:{app_id}" if isinstance(app_id, str) and app_id else None
    if kind == "connection":
        connection_id = source.get("connection_id")
        return f"connection:{connection_id}" if isinstance(connection_id, str) and connection_id else None
    return None


def _media_sources(scope: _MediaScope, admin_db_path: Path) -> list[dict[str, Any]]:
    app_ids = set(scope.app_ids) if scope.app_ids is not None else None
    sources: list[dict[str, Any]] = []
    for app in admin_store.list_apps(path=admin_db_path):
        app_id = str(app.get("id") or "")
        if not app_id or (app_ids is not None and app_id not in app_ids):
            continue
        sources.append({
            "id": f"app:{app_id}",
            "kind": "app",
            "label": str(app.get("name") or "App"),
            "app_id": app_id,
            "owner_user_id": app.get("owner_user_id"),
        })
    return sources


def _media_source_filter(request: Request, scope: _MediaScope) -> str | None:
    source = request.query_params.get("source")
    if source in (None, "", "all"):
        return None
    if source.startswith("app:"):
        app_id = source.removeprefix("app:")
        if scope.app_ids is not None and app_id not in set(scope.app_ids):
            raise PublicFailure("source_not_allowed", 403, source=source)
        return source
    raise PublicFailure("invalid_source", 400, param="source", accepted=["all", "app:<id>"])


def _client_hint(receipt: Mapping[str, Any] | None) -> str | None:
    """The client name recorded with the request, if any.

    Key names and raw user agents are not evidence of a client, so nothing is
    guessed from them here.
    """
    for value in ((receipt or {}).get("client_hint"), _nested(receipt, "metadata", "client_hint")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _iter_job_assets(job: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    assets = job.get("assets")
    if isinstance(assets, list):
        for item in assets:
            if isinstance(item, Mapping):
                yield item


def _asset_kind(asset_id: str, asset: Mapping[str, Any]) -> str:
    raw = str(asset.get("kind") or "")
    if raw in {"image", "video", "audio"}:
        return raw
    prefix = asset_id.split("_", 1)[0]
    return _KINDS_BY_PREFIX.get(prefix, "asset")


def _asset_created_at(
    path: Path | None, job: Mapping[str, Any], receipt: Mapping[str, Any] | None,
    embedded: Mapping[str, Any] | None = None,
) -> int | None:
    recorded = (embedded or {}).get("created_at")
    if isinstance(recorded, str):
        try:
            return int(datetime.fromisoformat(recorded.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    if job.get("created_at"):
        return int(job["created_at"])
    accepted_ms = receipt.get("accepted_at_ms") if receipt else None
    if isinstance(accepted_ms, int):
        return accepted_ms // 1000
    return None


def _file_is_stored(path: Path | None) -> bool:
    return path is not None and path.is_file()


def _file_modified_at(path: Path | None) -> int | None:
    return int(path.stat().st_mtime) if path is not None and path.exists() else None


def _model_for_asset(asset: Mapping[str, Any], job: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> str | None:
    return _string_first(
        asset.get("model"),
        _nested(job, "metadata", "model"),
        _nested(job, "metadata", "model_alias"),
        receipt.get("resolved_model") if receipt else None,
        receipt.get("requested_model") if receipt else None,
    )


def _workflow_for_asset(asset: Mapping[str, Any], job: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> str | None:
    return _string_first(
        asset.get("workflow"),
        job.get("workflow"),
        _nested(job, "metadata", "workflow"),
        receipt.get("resolved_workflow") if receipt else None,
        receipt.get("requested_workflow") if receipt else None,
    )


def _provenance(
    asset: Mapping[str, Any],
    job: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    embedded: Mapping[str, Any] | None = None,
    *,
    created_at: int | None,
    prompt: str | None = None,
) -> dict[str, Any]:
    embedded = embedded or {}
    return {
        "prompt": prompt,
        "model": _string_first(
            _model_for_asset(asset, job, receipt),
            embedded.get("resolved_model"),
            embedded.get("requested_model"),
        ),
        "workflow": _string_first(
            _workflow_for_asset(asset, job, receipt),
            embedded.get("workflow_id"),
            embedded.get("workflow"),
        ),
        "seed": _first_present(asset.get("seed"), _nested(job, "metadata", "seed"), _nested(receipt, "metadata", "seed"), embedded.get("seed")),
        "size": {
            "width": _first_present(asset.get("width"), _nested(job, "metadata", "width"), _nested(embedded, "parameters", "width")),
            "height": _first_present(asset.get("height"), _nested(job, "metadata", "height"), _nested(embedded, "parameters", "height")),
        },
        "created_at": created_at,
        "request_id": job.get("request_id"),
        "job_id": job.get("job_id"),
    }


def _prompt_title(prompt: str | None) -> str | None:
    if not prompt:
        return None
    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    return first_line[:120] or None


def _media_title(prompt: str | None, kind: str, created_at: int | None, source: Mapping[str, Any]) -> str:
    private = _prompt_title(prompt)
    if private:
        return private
    noun = {"image": "Picture", "audio": "Audio", "video": "Video"}.get(kind, "Item")
    maker = str(source.get("label") or "unknown maker")
    if created_at is None:
        return f"{noun} by {maker}"
    date = datetime.fromtimestamp(created_at).strftime("%b %-d at %-I:%M %p")
    return f"{noun} by {maker} · {date}"


def _entry_matches(item: _Entry, request: Request) -> bool:
    return _matches_media_filters(
        {"kind": item.kind, "model": item.model, "workflow": item.workflow,
         "prompt": item.prompt, "created_at": item.created_at},
        request,
    )


def _matches_media_filters(item: Mapping[str, Any], request: Request) -> bool:
    kind = request.query_params.get("kind")
    if kind and item.get("kind") != kind:
        return False
    model = request.query_params.get("model")
    if model and item.get("model") != model:
        return False
    workflow = request.query_params.get("workflow")
    if workflow and item.get("workflow") != workflow:
        return False
    search = str(request.query_params.get("q") or request.query_params.get("search") or "").strip().lower()
    if search and search not in str(item.get("prompt") or "").lower():
        return False
    start = _date_to_epoch(request.query_params.get("start") or request.query_params.get("date_from"))
    if start is not None and int(item.get("created_at") or 0) < start:
        return False
    end = _date_to_epoch(request.query_params.get("end") or request.query_params.get("date_to"), end=True)
    if end is not None and int(item.get("created_at") or 0) > end:
        return False
    return True


def _usage_summary(
    request: Request, *, actor: _Actor, admin_db_path: Path,
    media_jobs_path: Path, receipts_path: Path,
    asset_catalog_provider: AssetCatalogProvider | None,
    asset_metadata_reader: AssetMetadataReader | None,
    asset_path_resolver: AssetPathResolver,
) -> dict[str, Any]:
    if request.query_params.get("period", "month") != "month":
        raise PublicFailure("unsupported_period", 400, param="period", accepted=["month"])
    app_ids, scope_label, scoped_users = _usage_app_scope(request, actor, admin_db_path)
    source_filter = _source_filter(request)
    sources = _usage_sources(admin_db_path, app_ids=None if actor.owner else app_ids,
                             person_ids=None if actor.owner else scoped_users)
    _reject_forbidden_source_filter(source_filter, app_ids, sources, scoped_users)
    connections = {str(source["connection_id"]): source for source in sources if source.get("kind") == "connection"}
    apps = _Apps(admin_db_path)  # apps and credential links, read once for the whole summary
    now = datetime.now(timezone.utc)
    start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    # The unary plus keeps SQLite on the created_at index: with it the planner reads every top-level receipt.
    clauses = ["created_at >= ?", "+parent_request_id IS NULL"]
    params: list[Any] = [start]
    media_scope = _MediaScope(app_ids=app_ids, user_ids=scoped_users)
    owner_keys = _credential_hashes_for_scope(media_scope, admin_db_path)
    media_scope = _MediaScope(
        app_ids=app_ids, user_ids=scoped_users,
        owner_keys=frozenset(owner_keys) if owner_keys is not None else None,
    )
    if app_ids is not None:
        ownership_clauses: list[str] = []
        if app_ids:
            ownership_clauses.append("app_id IN (" + ",".join("?" for _ in app_ids) + ")")
            params.extend(app_ids)
        if owner_keys:
            ownership_clauses.append(
                "json_extract(receipt_json, '$.owner_key_hash') IN ("
                + ",".join("?" for _ in owner_keys) + ")"
            )
            params.extend(sorted(owner_keys))
        clauses.append("(" + " OR ".join(ownership_clauses) + ")" if ownership_clauses else "0")
    # The job list does not depend on the receipts, so read it while they load.
    succeeded_jobs = request_connections.submit(
        lambda: media_jobs.recent(limit=media_jobs.count(path=media_jobs_path), audience="internal",
                                  status=media_jobs.STATUS_SUCCEEDED, path=media_jobs_path))
    # Aggregate every retained row in the period, rather than a recent-feed cap.
    with receipt_store.reader(receipts_path) as conn:
        rows = conn.execute(
            "SELECT receipt_json, created_at FROM receipts WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC, request_id DESC", params,
        ).fetchall()
    receipts = []
    # Requests per source across the whole scope, so the Source picker can rank
    # sources by activity whichever source is currently selected.
    source_requests: dict[str, int] = {}
    for raw, stored_at in rows:
        value = json.loads(raw)
        if isinstance(value, dict):
            if not _receipt_in_scope(value, media_scope):
                continue
            value["_stored_at"] = stored_at
            _annotate_receipt_source(value, sources, admin_db_path, apps, connections)
            tallied = _source_id(value.get("source"))
            if tallied:
                source_requests[tallied] = source_requests.get(tallied, 0) + 1
            if _source_matches_filter(value.get("source"), source_filter):
                receipts.append(value)
    for source_row in sources:
        source_row["requests"] = source_requests.get(str(source_row.get("id")), 0)
    # A disconnected connection stays listed only while it has requests to show.
    sources[:] = [row for row in sources if not (row.get("kind") == "connection" and row.get("disabled") and not row["requests"])]
    by_day: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    spend = 0.0
    measured = 0
    paid_requests = 0
    unknown_paid_requests = 0
    failed_requests = 0
    outcomes_known = True
    for receipt in receipts:
        status = receipt.get("status")
        if not isinstance(status, str):
            status = None
        if status in {"failed", "error", "backend_failed", "refused", "rejected", "timed_out"}:
            failed_requests += 1
        elif status not in {
            "success", "succeeded", "ok", "accepted", "running", "in_progress",
            "cancelled", "disconnected",
        }:
            outcomes_known = False
        day = datetime.fromtimestamp(receipt["_stored_at"], timezone.utc).strftime("%Y-%m-%d")
        capability = _capability(receipt)
        daily = by_day.setdefault(day, {"date": day})
        daily[capability] = daily.get(capability, 0) + 1
        model = _usage_model(receipt)
        row = by_model.setdefault((model, capability), {"model": model, "requests": 0, "remote_spend": None,
                                          "capability": capability, "paid_requests": 0,
                                          "unknown_paid_requests": 0})
        row["requests"] += 1
        cost = _receipt_cost(receipt)
        if cost is not None:
            measured += 1
            spend += cost
            row["remote_spend"] = (row["remote_spend"] or 0) + cost
            if _receipt_is_paid_remote(receipt) or cost > 0:
                paid_requests += 1
                row["paid_requests"] += 1
        elif _receipt_is_paid_remote(receipt):
            unknown_paid_requests += 1
            row["unknown_paid_requests"] += 1
    hashes = owner_keys
    made: set[str] = set()
    # Retain first-match semantics without scanning the month's receipts per job.
    receipts_by_id: dict[str, Mapping[str, Any]] = {}
    for receipt in receipts:
        receipts_by_id.setdefault(str(receipt["request_id"]), receipt)
    jobs = succeeded_jobs.result()
    # Receipts older than the month (or out of the month's scope) in one read, not one per job.
    older = receipt_store.get_many(
        [request_id for job in jobs for request_id in _job_receipt_ids(job) if request_id not in receipts_by_id],
        audience="internal", path=receipts_path)
    # Assets of an in-scope job are decided by the job, as the Library lists them; the file
    # loop below only adds files no job knows, so a job's audio and video are not probed.
    in_jobs: set[str] = set()
    for job in jobs:
        job_receipt = None
        for request_id in _job_receipt_ids(job):
            job_receipt = receipts_by_id.get(request_id)
            if job_receipt is None:
                job_receipt = older.get(request_id)
                if job_receipt is not None:
                    _annotate_receipt_source(job_receipt, sources, admin_db_path, apps, connections)
            if job_receipt is not None:
                break
        if job_receipt is not None:
            job_in_scope = _receipt_in_scope(job_receipt, media_scope)
            if job_in_scope:
                in_jobs.update(str(a.get("asset_id") or a.get("id")) for a in _iter_job_assets(job))
            if (
                int(job_receipt.get("_stored_at") or 0) >= start
                and job_in_scope
                and _source_matches_filter(job_receipt.get("source"), source_filter)
            ):
                made.update(str(a.get("asset_id") or a.get("id")) for a in _iter_job_assets(job))
            continue
        if _job_in_scope(job, media_scope):
            in_jobs.update(str(a.get("asset_id") or a.get("id")) for a in _iter_job_assets(job))
            if int(job.get("created_at") or 0) >= start:
                source = _source_projection(None, admin_db_path, metadata=job, visible_app_ids=app_ids, apps=apps)
                if _source_matches_filter(source, source_filter):
                    made.update(str(a.get("asset_id") or a.get("id")) for a in _iter_job_assets(job))
    if asset_catalog_provider and asset_metadata_reader:
        for asset_id in asset_catalog_provider():
            if asset_id in in_jobs:
                continue
            metadata = asset_metadata_reader(asset_id)
            owners = set(metadata.get("owner_key_hashes") or [])
            if hashes is not None and not (owners & hashes):
                continue
            path = asset_path_resolver(asset_id)
            if path is not None and path.exists():
                embedded = _embedded_provenance_cached(path, probe=True)
                receipt = _receipt_for_embedded_cache_asset(embedded, receipts_path)
                if receipt is not None and not _receipt_in_scope(receipt, media_scope):
                    continue
                source = _source_projection(receipt, admin_db_path, metadata=metadata, embedded=embedded,
                                            visible_app_ids=app_ids, apps=apps, connections=connections)
                # A file without a recorded creation time counts by its file time.
                made_at = _asset_created_at(path, {}, receipt, embedded) or _file_modified_at(path) or 0
                if made_at >= start and _source_matches_filter(source, source_filter):
                    made.add(asset_id)
    receipt_list = []
    shown = receipts[:USAGE_RECEIPT_LIMIT]
    visible_receipts = receipt_store.get_many(
        [internal["request_id"] for internal in shown], audience="client",
        viewer_person_id=actor.user_id, path=receipts_path,
    )
    for internal in shown:
        visible = visible_receipts.get(internal["request_id"])
        if visible:
            visible["remote_spend"] = _receipt_cost(internal)
            visible["capability"] = _capability(internal)
            visible["source"] = internal.get("source")
            if isinstance(internal.get("source"), Mapping):
                visible["source_label"] = internal["source"].get("label")
            else:
                visible["source_label"] = internal.get("source_label")
            receipt_list.append(visible)
    if scope_label == "everyone":
        _audit_everyone(actor, "usage.summary.everyone", admin_db_path)
    retention_days = receipt_store.retention_days(admin_db_path)
    coverage_start = max(start, receipt_store.history_floor(receipts_path))
    aggregate_complete = coverage_start <= start
    coverage_note = ("Request totals include saved activity only. "
                     "Creation totals also include files still in your Library.")
    if not aggregate_complete:
        coverage_note = "This month is partial; earlier activity isn't included. " + coverage_note
    return {
        "schema_version": 1, "period": "month", "scope": scope_label,
        "source": source_filter, "sources": sources,
        "requests": len(receipts), "media_count": len(made),
        "failed_requests": failed_requests if outcomes_known else None,
        "remote_spend": round(spend, 6), "remote_spend_available": measured > 0,
        "remote_spend_source": "receipt costs" if measured else "not_available",
        "remote_spend_coverage": "This total includes recorded request costs only; other charges may be missing.",
        "spend_measured_requests": measured, "spend_total_requests": len(receipts),
        "paid_requests": paid_requests, "unknown_paid_requests": unknown_paid_requests,
        "retention_days": retention_days,
        "coverage_start": datetime.fromtimestamp(coverage_start, timezone.utc).isoformat(),
        "coverage_note": coverage_note,
        "truncated": False, "receipts_limit": USAGE_RECEIPT_LIMIT,
        "aggregate_complete": aggregate_complete,
        "receipt_list_has_more": len(receipts) > USAGE_RECEIPT_LIMIT,
        "by_day": [by_day[day] for day in sorted(by_day)],
        "by_model": sorted(by_model.values(), key=lambda row: (-row["requests"], row["model"])),
        "receipts": receipt_list,
        "people": _usage_people(actor, admin_db_path) if actor.owner else [],
    }


def _usage_app_scope(request: Request, actor: _Actor, admin_db_path: Path) -> tuple[list[str] | None, str, list[str] | None]:
    person = request.query_params.get("person")
    if not person or person in {"self", actor.user_id}:  # Everyone is an explicit choice.
        return _owned_app_ids(actor.user_id, admin_db_path), "me", [actor.user_id]
    if not actor.owner:
        raise PublicFailure("owner_required", 403, param="person")
    if person == "all":
        return None, "everyone", None
    return _owned_app_ids(person, admin_db_path), "person", [person]


def _usage_people(actor: _Actor, admin_db_path: Path) -> list[dict[str, str]]:
    people = [
        {"id": "all", "label": "Everyone"},
        {"id": "self", "label": "You"},
    ]
    for user in admin_store.list_users(path=admin_db_path):
        user_id = str(user["id"])
        if user_id == actor.user_id:
            continue
        people.append({
            "id": user_id,
            "label": str(user.get("display_name") or user.get("email") or "Person"),
        })
    return people


def _usage_sources(
    admin_db_path: Path, *, app_ids: list[str] | None, person_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Apps, then connections. ``None`` lists everyone's; otherwise only the given people's."""
    sources: list[dict[str, Any]] = []
    allowed = set(app_ids) if app_ids is not None else None
    for app in admin_store.list_apps(path=admin_db_path):
        app_id = str(app.get("id") or "")
        if app_id and (allowed is None or app_id in allowed):
            sources.append({
                "id": f"app:{app_id}",
                "label": str(app.get("name") or "App"),
                "kind": "app",
                "app_id": app_id,
                "owner_user_id": app.get("owner_user_id"),
                "created_at": app.get("created_at"),
                "disabled": app.get("disabled_at") is not None,
            })
    # A connection is its own source, labelled by the client that was approved.
    with admin_store.shared_connection(admin_db_path) as conn:
        grants = mcp_oauth_store.grants_in(conn, person_ids)
    for grant in grants:
        sources.append({
            "id": f"connection:{grant.id}",
            "label": grant.client_name,
            "kind": "connection",
            "connection_id": grant.id,
            "owner_user_id": grant.person_id,
            "created_at": grant.created_at,
            "disabled": grant.revoked_at is not None,
        })
    return sources


def _source_filter(request: Request) -> str | None:
    source = request.query_params.get("source")
    if source in (None, "", "all"):
        return None
    if source.startswith(("app:", "connection:")):
        return source
    raise PublicFailure("invalid_source", 400, param="source", accepted=["all", "app:<id>", "connection:<id>"])


def _reject_forbidden_source_filter(
    source: str | None, app_ids: list[str] | None,
    sources: list[Mapping[str, Any]] = (), person_ids: list[str] | None = None,
) -> None:
    if source is None:
        return
    if app_ids is not None and source.startswith("app:") and source.removeprefix("app:") not in set(app_ids):
        raise PublicFailure("source_not_allowed", 403, source=source)
    if person_ids is not None and source.startswith("connection:"):
        # Only a connection made by someone in scope may be asked for.
        if not any(row.get("id") == source and row.get("owner_user_id") in person_ids for row in sources):
            raise PublicFailure("source_not_allowed", 403, source=source)


def _source_matches_filter(source: Any, source_filter: str | None) -> bool:
    return source_filter is None or _source_id(source) == source_filter


def _annotate_receipt_source(
    receipt: dict[str, Any],
    sources: list[Mapping[str, Any]],
    admin_db_path: Path,
    apps: _Apps | None = None,
    connections: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    visible_app_ids = [
        str(source["app_id"])
        for source in sources
        if isinstance(source.get("app_id"), str) and source["app_id"]
    ]
    source = _source_projection(receipt, admin_db_path, visible_app_ids=visible_app_ids, apps=apps,
                                connections=connections)
    receipt["source"] = source
    receipt.setdefault("source_kind", source["kind"])
    receipt.setdefault("source_label", source["label"])


def _receipt_cost(receipt: Mapping[str, Any]) -> float | None:
    value = _nested(receipt, "metadata", "litellm", "response_cost")
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _capability(receipt: Mapping[str, Any]) -> str:
    intent = str(receipt.get("intent") or "")
    path = str(receipt.get("path") or "")
    if intent.startswith("audio.transcri") or path.startswith(("/v1/audio/transcriptions", "/v1/audio/translations")):
        return "transcribe"
    # Text to speech is its own task ("Read text aloud"), not music or sounds.
    if intent.startswith("audio.speech") or path.startswith("/v1/audio/speech"):
        return "voice"
    prefix = intent.split(".", 1)[0]
    if prefix in _CAPABILITY_BY_INTENT_PREFIX:
        return _CAPABILITY_BY_INTENT_PREFIX[prefix]
    if "/images" in path:
        return "image"
    if "/videos" in path:
        return "video"
    if "/audio" in path:
        return "audio"
    if "/embeddings" in path:
        return "embeddings"
    return prefix or "other"


def _usage_model(receipt: Mapping[str, Any]) -> str:
    model = _string_first(
        receipt.get("resolved_model"),
        receipt.get("requested_model"),
        receipt.get("model"),
        _nested(receipt, "metadata", "model"),
    )
    return display_model_id(model) if model else ""


def _capability_label(capability: str) -> str:
    return {
        "chat": "Chat",
        "image": "Image",
        "video": "Video",
        "audio": "Audio",
        "voice": "Voice",
        "transcribe": "Transcription",
        "embeddings": "Embeddings",
        "rerank": "Rerank",
        "decide": "Decisions",
    }.get(capability, capability.title())


def _receipt_is_paid_remote(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("remote") is True:
        return True
    if _nested(receipt, "cost", "cost_class") == "paid-remote":
        return True
    decisions = _nested(receipt, "metadata", "decisions", "remote")
    return decisions is True


def _audit_everyone(actor: _Actor, action: str, path: Path) -> None:
    admin_store.record_audit(
        action,
        actor_kind="user",
        actor_id=actor.user_id,
        actor_label=str(actor.user.get("email") or actor.user_id),
        target_kind="user_scope",
        target_id="everyone",
        metadata={"scope": "everyone"},
        path=path,
    )


def _bounded_int(raw: str | None, default: int, max_value: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_value))


def _encode_cursor(created_at: int, asset_id: str) -> str:
    raw = json.dumps({"created_at": created_at, "asset_id": asset_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(raw: str | None) -> tuple[int, str] | None:
    if not raw:
        return None
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return int(data["created_at"]), str(data["asset_id"])
    except Exception:
        raise PublicFailure("invalid_cursor", 400, param="cursor")


def _date_to_epoch(raw: str | None, *, end: bool = False) -> int | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise PublicFailure("invalid_date", 400, expected="YYYY-MM-DD")
    offset = 86399 if end else 0
    return int(parsed.timestamp()) + offset


def _nested(data: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _string_first(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _json(content: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"cache-control": "no-store"},
    )


def _error_response(exc: PublicFailure | UserPortalError) -> JSONResponse:
    if isinstance(exc, PublicFailure):
        return _json(exc.body(), status_code=exc.status_code)
    return _json({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)
