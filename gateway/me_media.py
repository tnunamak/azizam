"""Signed-in user media library and usage summary APIs.

This module is the user-facing projection over existing durable facts:
media jobs, generated asset handles, receipts, and the admin store's App
ownership. It does not execute media work and it does not read live services.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, media_jobs, media_metadata, provenance, receipt_store
from gateway.admin_oidc import AdminSession
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
    "chat": "chat",
    "image": "image",
    "video": "video",
    "audio": "audio",
    "speech": "audio",
    "stt": "transcribe",
    "transcription": "transcribe",
    "embedding": "embeddings",
    "rerank": "rerank",
    "decision": "decide",
}

_ASSET_SUFFIXES_BY_KIND = {
    "img": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "aud": (".wav", ".mp3", ".flac", ".ogg", ".m4a"),
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


class _ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code


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
    admin_db_path: PathProvider = admin_store.DB_PATH,
    media_jobs_path: PathProvider = media_jobs.DB_PATH,
    receipts_path: PathProvider = receipt_store.DB_PATH,
) -> APIRouter:
    router = APIRouter(prefix="/me/api", tags=["me"])

    @router.get("/media", include_in_schema=False)
    def list_media(request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
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
            )
            if scope.audit_everyone:
                _audit_everyone(actor, "media.library.everyone", admin_path)
            return _json(result)
        except (_ApiError, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/media/{asset_id}", include_in_schema=False)
    def media_detail(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
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
            )
            if item is None:
                raise _ApiError("asset_not_found", "Asset not found for this account", 404)
            if scope.audit_everyone:
                _audit_everyone(actor, "media.detail.everyone", admin_path)
            return _json({"asset": item})
        except (_ApiError, UserPortalError) as exc:
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
            actor = _actor_from_session(session, admin_path)
            scope = _MediaScope(app_ids=_owned_app_ids(actor.user_id, admin_path), user_ids=[actor.user_id])
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
            )
            if item is None:
                raise _ApiError("asset_not_found", "Asset not found for this account", 404)
            physical_removed = asset_deleter(asset_id)
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
        except (_ApiError, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/media/{asset_id}/download", include_in_schema=False)
    def media_download(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
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
            )
            if item is None:
                raise _ApiError("asset_not_found", "Asset not found for this account", 404)
            mode = request.query_params.get("metadata", "none")
            if mode not in {"none", "full"}:
                raise _ApiError("invalid_metadata", "Metadata must be full or none", 400)
            if scope.audit_everyone:
                _audit_everyone(actor, "media.download.everyone", admin_path)
            return _json({"asset_id": asset_id, "download_url": signed_url_builder(request, asset_id, mode)})
        except (_ApiError, UserPortalError) as exc:
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
        except (_ApiError, UserPortalError) as exc:
            return _error_response(exc)

    return router


def _resolve_path(provider: PathProvider) -> Path:
    return provider() if callable(provider) else provider


@dataclass(frozen=True)
class _MediaScope:
    app_ids: list[str] | None
    person_id: str | None = None
    user_ids: list[str] | None = None
    audit_everyone: bool = False


def _actor_from_request(
    request: Request,
    session_provider: SessionProvider,
    admin_db_path: Path,
) -> _Actor:
    session = session_provider(request)
    if session is None:
        raise _ApiError("not_authenticated", "Sign in to view this resource", 401)
    return _actor_from_session(session, admin_db_path)


def _actor_from_session(session: AdminSession | None, admin_db_path: Path) -> _Actor:
    if session is None:
        raise _ApiError("not_authenticated", "Sign in to view this resource", 401)
    user = admin_store.get_user(session.subject, path=admin_db_path)
    if user is None:
        raise _ApiError("session_user_not_found", "Signed-in user was not found", 401)
    role = _live_role_for_user(str(user["id"]), admin_db_path)
    return _Actor(user=user, role=role)


def create_asset_catalog_provider(cache_dir: Path) -> AssetCatalogProvider:
    def catalog() -> list[str]:
        ids: set[str] = set()
        if not cache_dir.exists():
            return []
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

    return catalog


def conservative_asset_deleter(
    *,
    metadata_reader: AssetMetadataReader,
    asset_path_resolver: AssetPathResolver,
    metadata_path_resolver: Callable[[str], Path | None],
) -> AssetDeleter:
    def delete(asset_id: str) -> bool:
        metadata = metadata_reader(asset_id)
        owners = {
            str(owner)
            for owner in metadata.get("owner_key_hashes") or []
            if isinstance(owner, str)
        }
        if len(owners) > 1:
            return False
        removed = False
        for path in (asset_path_resolver(asset_id), metadata_path_resolver(asset_id)):
            if path is not None and path.exists():
                path.unlink()
                removed = True
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
    roles = {
        str(role.get("role") or "")
        for role in admin_store.list_admin_roles(
            subject_kind="user", subject_id=user_id, path=path
        )
    }
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
    scope = str(request.query_params.get("scope") or request.query_params.get("owner") or "me")
    if scope == "everyone":
        if not actor.owner:
            raise _ApiError("owner_required", "Only owners can view everyone's media", 403)
        return _MediaScope(app_ids=None, user_ids=None, audit_everyone=True)
    person = request.query_params.get("person")
    if person:
        if not actor.owner:
            raise _ApiError("owner_required", "Only owners can filter by person", 403)
        if person == "all":
            return _MediaScope(app_ids=None, person_id="all", user_ids=None, audit_everyone=True)
        return _MediaScope(app_ids=_owned_app_ids(person, admin_db_path), person_id=person, user_ids=[person])
    return _MediaScope(app_ids=_owned_app_ids(actor.user_id, admin_db_path), user_ids=[actor.user_id])


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
) -> dict[str, Any]:
    limit = _bounded_int(request.query_params.get("limit"), MEDIA_DEFAULT_LIMIT, MEDIA_MAX_LIMIT)
    cursor = _decode_cursor(request.query_params.get("cursor"))
    all_items = _media_items(
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
    )
    filtered = [_item for _item in all_items if _matches_media_filters(_item, request)]
    if cursor:
        filtered = [
            item for item in filtered
            if (int(item.get("created_at") or 0), str(item.get("asset_id") or "")) < cursor
        ]
    page = filtered[: limit + 1]
    next_cursor = None
    if len(page) > limit:
        last = page[limit - 1]
        next_cursor = _encode_cursor(int(last.get("created_at") or 0), str(last.get("asset_id") or ""))
        page = page[:limit]
    return {
        "schema_version": 1,
        "authenticated": True,
        "scope": "everyone" if scope.app_ids is None else "me",
        "assets": page,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "models": sorted({item["model"] for item in all_items if item.get("model")}),
    }


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
) -> dict[str, Any] | None:
    for item in _media_items(
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
    ):
        if item.get("asset_id") == asset_id:
            return item
    return None


def _media_items(
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
) -> list[dict[str, Any]]:
    jobs = media_jobs.recent(
        limit=media_jobs.count(path=media_jobs_path),
        audience="internal",
        status=media_jobs.STATUS_SUCCEEDED,
        path=media_jobs_path,
    )
    receipts = _receipts_by_request_id(
        [str(job.get("request_id") or "") for job in jobs],
        path=receipts_path,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    deleted = _deleted_asset_ids(scope, admin_db_path)
    for job in jobs:
        receipt = receipts.get(str(job.get("request_id") or ""))
        if not _receipt_in_scope(receipt, scope):
            continue
        for asset in _iter_job_assets(job):
            asset_id = str(asset.get("asset_id") or asset.get("id") or "")
            if not asset_id or asset_id in seen or asset_id in deleted:
                continue
            seen.add(asset_id)
            kind = _asset_kind(asset_id, asset)
            path = asset_path_resolver(asset_id)
            if path is None or not path.is_file():
                continue
            embedded = _embedded_provenance(path)
            media_type = str(asset.get("media_type") or asset_media_type_resolver(asset_id, path))
            created_at = _asset_created_at(path, job, receipt, embedded)
            prompt = _string_first(
                _prompt_for_asset(asset, job, receipt),
                _nested(embedded, "parameters", "prompt"),
                embedded.get("prompt"),
            )
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
            out.append({
                "asset_id": asset_id,
                "id": asset_id,
                "kind": kind,
                "media_type": media_type,
                "size_bytes": int(path.stat().st_size) if path and path.exists() else None,
                "created_at": created_at,
                "updated_at": int(job.get("updated_at") or created_at),
                "job_id": job.get("job_id"),
                "request_id": job.get("request_id"),
                "status": job.get("status"),
                "prompt": prompt,
                "model": model,
                "workflow": workflow,
                "seed": _first_present(asset.get("seed"), _nested(job, "metadata", "seed"), _nested(receipt, "metadata", "seed"), embedded.get("seed")),
                "width": _first_present(asset.get("width"), _nested(job, "metadata", "width"), _nested(embedded, "parameters", "width")),
                "height": _first_present(asset.get("height"), _nested(job, "metadata", "height"), _nested(embedded, "parameters", "height")),
                "duration_s": _first_present(asset.get("duration_s"), asset.get("duration"), _nested(job, "metadata", "duration_s"), _nested(embedded, "parameters", "duration_s")),
                "poster_asset_id": asset.get("poster_asset_id"),
                "download_url": signed_url_builder(request, asset_id, "none"),
                "workflow_download_url": signed_url_builder(request, asset_id, "full") if embedded.get("workflow_available") else None,
                "generation_ms": receipt.get("latency_ms") if receipt else None,
                "provenance": _provenance(asset, job, receipt, embedded, created_at=created_at),
            })
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
            embedded = _embedded_provenance(path)
            kind = _asset_kind(asset_id, metadata)
            created_at = _asset_created_at(path, {}, None, embedded)
            seen.add(asset_id)
            out.append({
                "asset_id": asset_id,
                "id": asset_id,
                "kind": kind,
                "media_type": asset_media_type_resolver(asset_id, path),
                "size_bytes": int(path.stat().st_size),
                "created_at": created_at,
                "updated_at": int(metadata.get("updated_at") or path.stat().st_mtime),
                "job_id": None,
                "request_id": embedded.get("request_id"),
                "status": "available",
                "prompt": _string_first(_nested(embedded, "parameters", "prompt"), embedded.get("prompt")),
                "model": _string_first(embedded.get("resolved_model"), embedded.get("requested_model")),
                "workflow": _string_first(embedded.get("workflow_id"), embedded.get("workflow")),
                "seed": embedded.get("seed"),
                "width": _nested(embedded, "parameters", "width"),
                "height": _nested(embedded, "parameters", "height"),
                "duration_s": _nested(embedded, "parameters", "duration_s"),
                "poster_asset_id": None,
                "download_url": signed_url_builder(request, asset_id, "none"),
                "workflow_download_url": signed_url_builder(request, asset_id, "full") if embedded.get("workflow_available") else None,
                "generation_ms": None,
                "provenance": {
                    "prompt": _string_first(_nested(embedded, "parameters", "prompt"), embedded.get("prompt")),
                    "model": _string_first(embedded.get("resolved_model"), embedded.get("requested_model")),
                    "workflow": _string_first(embedded.get("workflow_id"), embedded.get("workflow")),
                    "seed": embedded.get("seed"),
                    "size": {
                        "width": _nested(embedded, "parameters", "width"),
                        "height": _nested(embedded, "parameters", "height"),
                    },
                    "created_at": created_at,
                    "request_id": embedded.get("request_id"),
                    "job_id": None,
                    "source": "asset_cache",
                    "complete": bool(embedded),
                },
            })
    return sorted(out, key=lambda item: (int(item.get("created_at") or 0), str(item.get("asset_id") or "")), reverse=True)


def _receipts_by_request_id(request_ids: Iterable[str], *, path: Path) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for request_id in {value for value in request_ids if value}:
        receipt = receipt_store.get(request_id, audience="internal", path=path)
        if receipt is not None:
            out[request_id] = receipt
    return out


def _credential_hashes_for_scope(scope: _MediaScope, path: Path) -> set[str] | None:
    if scope.user_ids is None:
        return None
    app_ids: set[str] = set(scope.app_ids or [])
    hashes: set[str] = set()
    for credential in admin_store.list_credentials(path=path):
        app_id = str(credential.get("app_id") or credential.get("subject_id") or "")
        if credential.get("subject_kind") == "app" and app_id in app_ids:
            key_hash = credential.get("hash")
            if isinstance(key_hash, str) and key_hash:
                hashes.add(key_hash)
    return hashes


def _deleted_asset_ids(scope: _MediaScope, path: Path) -> set[str]:
    if scope.user_ids is None:
        return set()
    deleted: set[str] = set()
    for user_id in scope.user_ids:
        cursor: dict[str, Any] = {}
        while True:
            events = admin_store.list_audit(
                actor_id=user_id, action_prefix="media.asset.delete",
                limit=500, path=path, **cursor,
            )
            deleted.update(str(event["target_id"]) for event in events if event.get("target_id"))
            if len(events) < 500:
                break
            last = events[-1]
            cursor = {"before_created_at": last["created_at"], "before_id": last["id"]}
    return deleted


def _embedded_provenance(path: Path) -> dict[str, Any]:
    # Images use PNG text or EXIF. Audio/video use the existing container-tag
    # contract. Limit byte reads; large files can still be downloaded normally.
    try:
        if path.suffix.lower() == ".png":
            if path.stat().st_size > 32 * 1024 * 1024:
                return {}
            chunks = media_metadata.png_text_chunks(path.read_bytes())
            raw = chunks.get(provenance.PROVENANCE_KEY)
            workflow = chunks.get("workflow")
        elif path.suffix.lower() in {".jpg", ".jpeg", ".webp"}:
            from PIL import Image
            with Image.open(path) as image:
                exif = image.getexif()
                description = str(exif.get(0x010E, ""))
                raw = next((line.split(":", 1)[1] for line in description.splitlines()
                            if line.startswith(provenance.PROVENANCE_KEY + ":")), None)
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
            raw = tags.get(provenance.PROVENANCE_KEY)
            if raw is None:
                for line in str(tags.get("comment", "")).splitlines():
                    if line.startswith(provenance.PROVENANCE_KEY + "="):
                        raw = line.split("=", 1)[1]
            workflow = None
        data = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(data, dict):
            return {}
        data = provenance.sanitize_provenance(data)
        data["workflow_available"] = bool(workflow) and path.suffix.lower() == ".png"
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired, media_metadata.MediaMetadataError):
        return {}


def _receipt_in_scope(receipt: Mapping[str, Any] | None, scope: _MediaScope) -> bool:
    if scope.app_ids is None:
        return True
    if receipt is None:
        return False
    return str(receipt.get("app_id") or "") in set(scope.app_ids)


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
) -> int:
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
    return int(path.stat().st_mtime) if path and path.exists() else 0


def _prompt_for_asset(asset: Mapping[str, Any], job: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> str | None:
    return _string_first(
        asset.get("prompt"),
        _nested(job, "metadata", "prompt"),
        _nested(job, "metadata", "parameters", "prompt"),
        _nested(receipt, "metadata", "prompt"),
        _nested(receipt, "metadata", "parameters", "prompt"),
    )


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
    created_at: int,
) -> dict[str, Any]:
    embedded = embedded or {}
    return {
        "prompt": _string_first(
            _prompt_for_asset(asset, job, receipt),
            _nested(embedded, "parameters", "prompt"),
            embedded.get("prompt"),
        ),
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
        raise _ApiError("unsupported_period", "Only period=month is supported", 400)
    app_ids, scope_label = _usage_app_scope(request, actor, admin_db_path)
    now = datetime.now(timezone.utc)
    start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    clauses = ["created_at >= ?", "parent_request_id IS NULL"]
    params: list[Any] = [start]
    if app_ids is not None:
        clauses.append("app_id IN (" + ",".join("?" for _ in app_ids) + ")" if app_ids else "0")
        params.extend(app_ids)
    # Aggregate every retained row in the period, rather than a recent-feed cap.
    with sqlite3.connect(str(receipts_path)) as conn:
        rows = conn.execute(
            "SELECT receipt_json, created_at FROM receipts WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC, request_id DESC", params,
        ).fetchall()
    receipts = []
    for raw, stored_at in rows:
        value = json.loads(raw)
        if isinstance(value, dict):
            value["_stored_at"] = stored_at
            receipts.append(value)
    by_day: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    spend = 0.0
    measured = 0
    for receipt in receipts:
        day = datetime.fromtimestamp(receipt["_stored_at"], timezone.utc).strftime("%Y-%m-%d")
        capability = _capability(receipt)
        daily = by_day.setdefault(day, {"date": day})
        daily[capability] = daily.get(capability, 0) + 1
        model = str(receipt.get("resolved_model") or receipt.get("requested_model") or "Unknown model")
        row = by_model.setdefault(model, {"model": model, "requests": 0, "remote_spend": None,
                                          "capability": capability})
        if row["capability"] != capability:
            row["capability"] = "other"
        row["requests"] += 1
        cost = _receipt_cost(receipt)
        if cost is not None:
            measured += 1
            spend += cost
            row["remote_spend"] = (row["remote_spend"] or 0) + cost
    scoped_users = None if app_ids is None else [request.query_params.get("person") or actor.user_id]
    media_scope = _MediaScope(app_ids=app_ids, user_ids=scoped_users)
    hashes = _credential_hashes_for_scope(media_scope, admin_db_path)
    made: set[str] = set()
    receipt_ids = {str(r["request_id"]) for r in receipts}
    for job in media_jobs.recent(limit=media_jobs.count(path=media_jobs_path), audience="internal",
                                 status=media_jobs.STATUS_SUCCEEDED, path=media_jobs_path):
        if str(job.get("request_id")) in receipt_ids:
            made.update(str(a.get("asset_id") or a.get("id")) for a in _iter_job_assets(job))
    if asset_catalog_provider and asset_metadata_reader:
        for asset_id in asset_catalog_provider():
            metadata = asset_metadata_reader(asset_id)
            owners = set(metadata.get("owner_key_hashes") or [])
            if hashes is not None and not (owners & hashes):
                continue
            path = asset_path_resolver(asset_id)
            if path is not None and path.exists():
                embedded = _embedded_provenance(path)
                if _asset_created_at(path, {}, None, embedded) >= start:
                    made.add(asset_id)
    receipt_list = []
    for internal in receipts[:USAGE_RECEIPT_LIMIT]:
        visible = receipt_store.get(internal["request_id"], audience="client", path=receipts_path)
        if visible:
            visible["remote_spend"] = _receipt_cost(internal)
            visible["capability"] = _capability(internal)
            receipt_list.append(visible)
    if scope_label == "everyone":
        _audit_everyone(actor, "usage.summary.everyone", admin_db_path)
    coverage_start = max(start, int(time.time()) - receipt_store.RETAIN_S)
    return {
        "schema_version": 1, "period": "month", "scope": scope_label,
        "requests": len(receipts), "media_count": len(made),
        "remote_spend": round(spend, 6), "remote_spend_available": measured > 0,
        "remote_spend_source": "receipt costs" if measured else "not_available",
        "remote_spend_coverage": "Recorded receipt costs only; provider spend ledger is not installed.",
        "spend_measured_requests": measured, "spend_total_requests": len(receipts),
        "retention_days": receipt_store.RETAIN_S // 86400,
        "coverage_start": datetime.fromtimestamp(coverage_start, timezone.utc).isoformat(),
        "coverage_note": "Requests cover retained receipts; media totals also include retained assets.",
        "truncated": False, "receipts_limit": USAGE_RECEIPT_LIMIT,
        "by_day": [by_day[day] for day in sorted(by_day)],
        "by_model": sorted(by_model.values(), key=lambda row: (-row["requests"], row["model"])),
        "receipts": receipt_list,
        "people": [{"id": u["id"], "name": u.get("display_name") or u.get("email") or "Person"}
                   for u in admin_store.list_users(path=admin_db_path)] if actor.owner else [],
    }


def _usage_app_scope(request: Request, actor: _Actor, admin_db_path: Path) -> tuple[list[str] | None, str]:
    person = request.query_params.get("person")
    if not person:
        return _owned_app_ids(actor.user_id, admin_db_path), "me"
    if not actor.owner:
        raise _ApiError("owner_required", "Only owners can filter usage by person", 403)
    if person == "all":
        return None, "everyone"
    return _owned_app_ids(person, admin_db_path), "person"


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
        raise _ApiError("invalid_cursor", "Cursor is invalid", 400)


def _date_to_epoch(raw: str | None, *, end: bool = False) -> int | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise _ApiError("invalid_date", "Dates must use YYYY-MM-DD", 400)
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


def _error_response(exc: _ApiError | UserPortalError) -> JSONResponse:
    status_code = getattr(exc, "status_code", 500)
    code = getattr(exc, "code", "internal_error")
    message = getattr(exc, "message", "Request failed")
    return _json({"error": {"code": code, "message": message}}, status_code=status_code)
