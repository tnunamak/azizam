"""Signed-in user media library and usage summary APIs.

This module is the user-facing projection over existing durable facts:
media jobs, generated asset handles, receipts, and the admin store's App
ownership. It does not execute media work and it does not read live services.
"""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
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
SignedUrlBuilder = Callable[[Request, str], str]
AssetPathResolver = Callable[[str], Path | None]
AssetMediaTypeResolver = Callable[[str, Path | None], str]
AssetDeleter = Callable[[str], bool]
AssetCatalogProvider = Callable[[], Iterable[str]]
AssetMetadataReader = Callable[[str], Mapping[str, Any]]
PathProvider = Path | Callable[[], Path]

MEDIA_DEFAULT_LIMIT = 24
MEDIA_MAX_LIMIT = 100
USAGE_RECEIPT_LIMIT = 50
USAGE_AGGREGATE_LIMIT = 100000

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
    async def list_media(request: Request) -> JSONResponse:
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
    async def media_detail(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            actor = _actor_from_request(request, session_provider, admin_path)
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
            return _json({"asset": item})
        except (_ApiError, UserPortalError) as exc:
            return _error_response(exc)

    @router.delete("/media/{asset_id}", include_in_schema=False)
    async def delete_media(asset_id: str, request: Request) -> JSONResponse:
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
    async def media_download(asset_id: str, request: Request) -> JSONResponse:
        try:
            admin_path = _resolve_path(admin_db_path)
            jobs_path = _resolve_path(media_jobs_path)
            receipt_path = _resolve_path(receipts_path)
            actor = _actor_from_request(request, session_provider, admin_path)
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
            return _json({"asset_id": asset_id, "download_url": item["download_url"]})
        except (_ApiError, UserPortalError) as exc:
            return _error_response(exc)

    @router.get("/usage/summary", include_in_schema=False)
    async def usage_summary(request: Request) -> JSONResponse:
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
        if app.get("id") and not app.get("disabled_at")
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
        limit=1000,
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
            embedded = _embedded_provenance(path) if path is not None and path.exists() else {}
            media_type = str(asset.get("media_type") or asset_media_type_resolver(asset_id, path))
            created_at = _asset_created_at(path, job, receipt)
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
                "download_url": signed_url_builder(request, asset_id),
                "provenance": _provenance(asset, job, receipt, embedded),
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
            created_at = int(path.stat().st_ctime)
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
                "download_url": signed_url_builder(request, asset_id),
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
    user_ids = set(scope.user_ids)
    deleted: set[str] = set()
    for event in admin_store.list_audit(
        action_prefix="media.asset.delete",
        limit=1000,
        path=path,
    ):
        if str(event.get("actor_id") or "") in user_ids and event.get("target_id"):
            deleted.add(str(event["target_id"]))
    return deleted


def _embedded_provenance(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        if path.suffix.lower() == ".png":
            chunks = media_metadata.png_text_chunks(content)
            raw = chunks.get(provenance.PROVENANCE_KEY)
        else:
            tags = media_metadata.read_media_tags(content, path.suffix.lower())
            raw = tags.get(provenance.PROVENANCE_KEY)
    except Exception:
        raw = None
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


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


def _asset_created_at(path: Path | None, job: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> int:
    if path is not None and path.exists():
        return int(path.stat().st_ctime)
    accepted_ms = receipt.get("accepted_at_ms") if receipt else None
    if isinstance(accepted_ms, int):
        return int(accepted_ms / 1000)
    return int(job.get("created_at") or time.time())


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
        "seed": _first_present(asset.get("seed"), _nested(job, "metadata", "seed"), embedded.get("seed")),
        "size": {
            "width": _first_present(asset.get("width"), _nested(job, "metadata", "width"), _nested(embedded, "parameters", "width")),
            "height": _first_present(asset.get("height"), _nested(job, "metadata", "height"), _nested(embedded, "parameters", "height")),
        },
        "created_at": int(job.get("created_at") or 0),
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
    request: Request,
    *,
    actor: _Actor,
    admin_db_path: Path,
    media_jobs_path: Path,
    receipts_path: Path,
) -> dict[str, Any]:
    period = str(request.query_params.get("period") or "month")
    if period != "month":
        raise _ApiError("unsupported_period", "Only period=month is supported", 400)
    app_ids, scope_label = _usage_app_scope(request, actor, admin_db_path)
    since_s = _seconds_since_month_start()
    receipts = receipt_store.recent(
        limit=USAGE_AGGREGATE_LIMIT,
        audience="internal",
        app_ids=app_ids,
        since_s=since_s,
        outer_only=True,
        path=receipts_path,
    )
    jobs = media_jobs.recent(
        limit=USAGE_AGGREGATE_LIMIT,
        audience="internal",
        status=media_jobs.STATUS_SUCCEEDED,
        since_s=since_s,
        path=media_jobs_path,
    )
    receipt_app_ids = None if app_ids is None else set(app_ids)
    media_count = 0
    for job in jobs:
        receipt = receipt_store.get(str(job.get("request_id") or ""), audience="internal", path=receipts_path)
        if receipt_app_ids is None or (receipt and str(receipt.get("app_id") or "") in receipt_app_ids):
            media_count += sum(1 for _ in _iter_job_assets(job))
    by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"date": "", "capabilities": defaultdict(int)})
    by_model: dict[str, dict[str, Any]] = {}
    spend = 0.0
    spend_available = False
    for receipt in receipts:
        day = _day_key(receipt)
        capability = _capability(receipt)
        by_day[day]["date"] = day
        by_day[day]["capabilities"][capability] += 1
        model = str(receipt.get("resolved_model") or receipt.get("requested_model") or "unknown")
        row = by_model.setdefault(model, {"model": model, "requests": 0, "remote_spend": 0.0})
        row["requests"] += 1
        cost = _receipt_cost(receipt)
        if cost is not None:
            spend_available = True
            spend += cost
            row["remote_spend"] += cost
    receipt_list = receipt_store.recent(
        limit=USAGE_RECEIPT_LIMIT,
        audience="client",
        app_ids=app_ids,
        since_s=since_s,
        outer_only=True,
        path=receipts_path,
    )
    if app_ids is None:
        receipt_list = [
            receipt_store.get(str(r.get("request_id")), audience="client", path=receipts_path) or r
            for r in receipts[:USAGE_RECEIPT_LIMIT]
        ]
    if scope_label == "everyone":
        _audit_everyone(actor, "usage.summary.everyone", admin_db_path)
    return {
        "schema_version": 1,
        "period": "month",
        "scope": scope_label,
        "requests": len(receipts),
        "media_count": media_count,
        "remote_spend": round(spend, 6) if spend_available else 0.0,
        "remote_spend_available": spend_available,
        "remote_spend_source": "receipts.litellm.response_cost" if spend_available else "not_available",
        "remote_spend_coverage": (
            "receipt_litellm_cost_only"
            if spend_available
            else "provider_spend_ledger_not_present"
        ),
        "truncated": len(receipts) >= USAGE_AGGREGATE_LIMIT,
        "by_day": [
            {"date": day, **dict(data["capabilities"])}
            for day, data in sorted(by_day.items())
        ],
        "by_model": sorted(by_model.values(), key=lambda row: (-int(row["requests"]), str(row["model"]))),
        "receipts": receipt_list,
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
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _capability(receipt: Mapping[str, Any]) -> str:
    intent = str(receipt.get("intent") or "")
    prefix = intent.split(".", 1)[0]
    if prefix in _CAPABILITY_BY_INTENT_PREFIX:
        return _CAPABILITY_BY_INTENT_PREFIX[prefix]
    path = str(receipt.get("path") or "")
    if "/images" in path:
        return "image"
    if "/videos" in path:
        return "video"
    if "/audio" in path:
        return "audio"
    if "/embeddings" in path:
        return "embeddings"
    return prefix or "other"


def _day_key(receipt: Mapping[str, Any]) -> str:
    accepted_ms = receipt.get("accepted_at_ms")
    if isinstance(accepted_ms, int):
        ts = int(accepted_ms / 1000)
    else:
        ts = int(receipt.get("_stored_at") or time.time())
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _seconds_since_month_start() -> int:
    now = time.localtime()
    start = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst))
    return max(1, int(time.time() - start))


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
        parsed = time.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise _ApiError("invalid_date", "Dates must use YYYY-MM-DD", 400)
    offset = 86399 if end else 0
    return int(time.mktime(parsed)) + offset


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
