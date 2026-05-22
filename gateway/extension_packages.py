"""Data-only extension package parser and validator."""

from __future__ import annotations

import hashlib
import copy
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from gateway.capabilities import Capability, ResourceEstimate, summarize_capability
from gateway.discovery import WorkflowRecord, summarize_workflow
from gateway.manifests import ManifestError, manifest_identity, validate_manifest
from gateway import service_registry


MANIFEST_NAME = "extension.yml"
PACKAGE_KIND = "extension_package"
EXTENSION_PACKAGE_INDEX_SETTING_KEY = "extension_package_index"
SUPPORTED_OBJECT_KINDS = frozenset(
    {"services", "protocol_frontends", "capabilities", "workflows"}
)
CODE_BEARING_ADAPTER_KEYS = frozenset(
    {
        "adapter",
        "adapter_class",
        "adapter_module",
        "class",
        "code",
        "entrypoint",
        "handler",
        "module",
        "python",
    }
)


def empty_extension_package_index() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sources": [],
        "packages": {},
        "updated_at": 0,
    }


def normalize_extension_package_index(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return empty_extension_package_index()

    packages = value.get("packages")
    if not isinstance(packages, Mapping):
        packages = {}

    sources = value.get("sources")
    if not isinstance(sources, list):
        sources = []

    return {
        "schema_version": int(value.get("schema_version") or 1),
        "sources": [dict(source) for source in sources if isinstance(source, Mapping)],
        "packages": {
            str(package_id): dict(package)
            for package_id, package in packages.items()
            if isinstance(package, Mapping)
        },
        "updated_at": int(value.get("updated_at") or 0),
    }


def validate_extension_package_source(
    source: Mapping[str, Any],
    *,
    install_root: str | Path | None = None,
) -> dict[str, Any]:
    source_type = str(source.get("type") or source.get("kind") or "local_directory")
    if source_type != "local_directory":
        if source_type in {"git", "github", "git_url"}:
            return review_git_package_source(source, install_root=install_root)
        return _source_error(source, f"unsupported extension package source type: {source_type}")

    path = source.get("path") or source.get("directory")
    if not path:
        return _source_error(source, "local_directory extension package source requires path")

    try:
        return review_local_directory_package(Path(str(path)))
    except ValueError as exc:
        return _source_error(source, str(exc))


def review_git_package_source(
    source: Mapping[str, Any],
    *,
    install_root: str | Path | None = None,
) -> dict[str, Any]:
    url = _git_source_url(source)
    if not url:
        return _source_error(source, "git extension package source requires url or repo")

    ref = str(
        source.get("ref")
        or source.get("commit")
        or source.get("tag")
        or source.get("branch")
        or ""
    ).strip()
    subdir = str(source.get("subdir") or source.get("path_in_repo") or "").strip()
    try:
        if install_root is None:
            with tempfile.TemporaryDirectory(prefix="ai-gateway-extension-") as tmp:
                clone_dir = Path(tmp) / "repo"
                resolved_ref = _clone_git_source(url, ref, clone_dir)
                review = review_local_directory_package(_package_dir_from_clone(clone_dir, subdir))
                review["resolved_source"] = _resolved_git_source(
                    source,
                    url=url,
                    ref=ref,
                    resolved_ref=resolved_ref,
                    subdir=subdir,
                    installed_path=None,
                )
                return review

        install_base = Path(install_root)
        install_base.mkdir(parents=True, exist_ok=True)
        clone_dir = install_base / _source_cache_key(source, url=url, ref=ref, subdir=subdir)
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        resolved_ref = _clone_git_source(url, ref, clone_dir)
        package_dir = _package_dir_from_clone(clone_dir, subdir)
        review = review_local_directory_package(package_dir)
        review["resolved_source"] = _resolved_git_source(
            source,
            url=url,
            ref=ref,
            resolved_ref=resolved_ref,
            subdir=subdir,
            installed_path=package_dir,
        )
        return review
    except ValueError as exc:
        return _source_error(source, str(exc))


def _git_source_url(source: Mapping[str, Any]) -> str:
    url = str(source.get("url") or source.get("repo_url") or "").strip()
    if url:
        return url
    repo = str(source.get("repo") or source.get("repository") or "").strip()
    if not repo:
        return ""
    if repo.startswith(("http://", "https://", "ssh://", "git@", "/")):
        return repo
    return f"https://github.com/{repo}.git"


def _clone_git_source(url: str, ref: str, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "clone", "--depth", "1", url, str(target)])
    if ref:
        _run_git(["git", "-C", str(target), "fetch", "--depth", "1", "origin", ref])
        _run_git(["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"])
    return _run_git(["git", "-C", str(target), "rev-parse", "HEAD"]).strip()


def _run_git(cmd: list[str]) -> str:
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is required to import git extension sources") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git operation timed out while importing extension source") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ValueError(f"git operation failed: {message[:400]}") from exc
    return completed.stdout


def _package_dir_from_clone(clone_dir: Path, subdir: str) -> Path:
    if not subdir:
        return clone_dir
    raw = Path(subdir)
    if raw.is_absolute():
        raise ValueError("git extension package subdir must be relative")
    package_dir = (clone_dir / raw).resolve()
    try:
        package_dir.relative_to(clone_dir.resolve())
    except ValueError as exc:
        raise ValueError("git extension package subdir cannot traverse outside repo") from exc
    return package_dir


def _source_cache_key(
    source: Mapping[str, Any],
    *,
    url: str,
    ref: str,
    subdir: str,
) -> str:
    package_hint = str(source.get("id") or source.get("name") or "package")
    safe_hint = service_registry.normalize_id(package_hint) or "package"
    digest = hashlib.sha256(
        repr({"url": url, "ref": ref, "subdir": subdir}).encode("utf-8")
    ).hexdigest()[:16]
    return f"{safe_hint}-{digest}"


def _resolved_git_source(
    source: Mapping[str, Any],
    *,
    url: str,
    ref: str,
    resolved_ref: str,
    subdir: str,
    installed_path: Path | None,
) -> dict[str, Any]:
    resolved = {
        "type": "git",
        "url": url,
        "ref": ref,
        "resolved_ref": resolved_ref,
    }
    if subdir:
        resolved["subdir"] = subdir
    if installed_path is not None:
        resolved["path"] = str(installed_path)
    for key in ("id", "name"):
        if source.get(key):
            resolved[key] = str(source[key])
    return resolved


def imported_registry_records(review: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    package = review.get("package") if isinstance(review, Mapping) else {}
    if not isinstance(package, Mapping):
        package = {}
    provenance = {
        "package_id": str(package.get("id") or ""),
        "package_version": str(package.get("version") or ""),
        "package_hash": str(review.get("package_hash") or ""),
    }
    services = {
        str(record.get("id")): _with_import_provenance(record, provenance)
        for record in review.get("services") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    protocol_frontends = {
        str(record.get("id")): _with_import_provenance(record, provenance)
        for record in review.get("protocol_frontends") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    workflows = {
        str(record.get("id")): _with_import_provenance(record, provenance)
        for record in review.get("workflows") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    capabilities = {
        str(record.get("id")): _with_import_provenance(record, provenance)
        for record in review.get("capabilities") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    return {
        "services": services,
        "protocol_frontends": protocol_frontends,
        "workflows": workflows,
        "capabilities": capabilities,
    }


def package_index_record(
    review: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    enabled: bool = True,
) -> dict[str, Any]:
    package = review.get("package") if isinstance(review, Mapping) else {}
    if not isinstance(package, Mapping):
        package = {}
    package_id = str(package.get("id") or review.get("id") or "")
    return {
        "id": package_id,
        "name": str(package.get("name") or package_id),
        "version": str(package.get("version") or ""),
        "enabled": bool(enabled),
        "source": copy.deepcopy(dict(review.get("resolved_source") or source)),
        "package_hash": str(review.get("package_hash") or ""),
        "object_counts": copy.deepcopy(dict(review.get("object_counts") or {})),
        "services": sorted(imported_registry_records(review)["services"]),
        "protocol_frontends": sorted(imported_registry_records(review)["protocol_frontends"]),
        "capabilities": sorted(
            str(record.get("id"))
            for record in review.get("capabilities") or []
            if isinstance(record, Mapping) and record.get("id")
        ),
        "workflows": sorted(
            str(record.get("id"))
            for record in review.get("workflows") or []
            if isinstance(record, Mapping) and record.get("id")
        ),
        "warnings": [str(item) for item in review.get("warnings") or []],
        "updated_at": int(time.time()),
    }


def merge_package_index(
    index: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    out = normalize_extension_package_index(index)
    record = package_index_record(review, source=source)
    package_id = record["id"]
    if not package_id:
        raise ValueError("extension package id is required")
    out["packages"][package_id] = record
    source_record = dict(source)
    if source_record and source_record not in out["sources"]:
        out["sources"].append(source_record)
    out["updated_at"] = int(time.time())
    return out


def review_local_directory_package(package_dir: str | Path) -> dict[str, Any]:
    """Return a validation review for a local data-only extension package."""

    root = Path(package_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"extension package directory does not exist: {package_dir}")

    warnings: list[str] = []
    errors: list[str] = []
    services: list[dict[str, Any]] = []
    protocol_frontends: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    hash_files: dict[str, bytes] = {}

    manifest_path = root / MANIFEST_NAME
    manifest = _load_yaml_file(manifest_path, errors, hash_files, root)
    if not isinstance(manifest, Mapping):
        return _review(
            manifest={},
            object_counts=_object_counts(services, protocol_frontends, capabilities, workflows),
            services=[],
            protocol_frontends=[],
            capabilities=[],
            workflows=[],
            warnings=warnings,
            errors=errors,
            hash_files=hash_files,
        )

    _validate_manifest_metadata(manifest, errors)

    objects = manifest.get("objects")
    if objects is None:
        objects = {}
    if not isinstance(objects, Mapping):
        errors.append("extension.yml: objects must be a mapping")
        objects = {}

    for object_kind in objects:
        if object_kind not in SUPPORTED_OBJECT_KINDS:
            warnings.append(
                f"extension.yml: objects.{object_kind} is deferred; unknown object kind"
            )

    for raw_ref in _object_refs(objects.get("services"), "objects.services", errors):
        record = _load_referenced_object(root, raw_ref, "service", errors, hash_files)
        if isinstance(record, Mapping):
            _check_code_bearing_adapter(record, "services", errors)
            services.append(_normalize_service(record))

    for raw_ref in _object_refs(
        objects.get("protocol_frontends"),
        "objects.protocol_frontends",
        errors,
    ):
        record = _load_referenced_object(root, raw_ref, "protocol_frontend", errors, hash_files)
        if isinstance(record, Mapping):
            _check_code_bearing_adapter(record, "protocol_frontends", errors)
            protocol_frontends.append(_normalize_protocol_frontend(record))

    for raw_ref in _object_refs(objects.get("capabilities"), "objects.capabilities", errors):
        record = _load_referenced_object(root, raw_ref, "capability", errors, hash_files)
        if isinstance(record, Mapping):
            normalized = _normalize_capability(record, raw_ref, errors)
            if normalized is not None:
                capabilities.append(normalized)

    for raw_ref in _object_refs(objects.get("workflows"), "objects.workflows", errors):
        record = _load_referenced_object(root, raw_ref, None, errors, hash_files)
        if isinstance(record, Mapping):
            normalized = _normalize_workflow(record, raw_ref, root, errors)
            if normalized is not None:
                workflows.append(normalized)

    return _review(
        manifest=manifest,
        object_counts=_object_counts(services, protocol_frontends, capabilities, workflows),
        services=services,
        protocol_frontends=protocol_frontends,
        capabilities=capabilities,
        workflows=workflows,
        warnings=warnings,
        errors=errors,
        hash_files=hash_files,
    )


def _review(
    *,
    manifest: Mapping[str, Any],
    object_counts: Mapping[str, int],
    services: list[dict[str, Any]],
    protocol_frontends: list[dict[str, Any]],
    capabilities: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
    warnings: list[str],
    errors: list[str],
    hash_files: Mapping[str, bytes],
) -> dict[str, Any]:
    package = {
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "id": manifest.get("id"),
        "version": manifest.get("version"),
    }
    return {
        "package": package,
        "package_hash": _package_hash(hash_files),
        "services": services,
        "protocol_frontends": protocol_frontends,
        "capabilities": capabilities,
        "workflows": workflows,
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
        "schema_version": package["schema_version"],
        "kind": package["kind"],
        "id": package["id"],
        "version": package["version"],
        "object_counts": dict(object_counts),
    }


def _source_error(source: Mapping[str, Any], error: str) -> dict[str, Any]:
    return {
        "package": {
            "schema_version": None,
            "kind": None,
            "id": source.get("id"),
            "version": None,
        },
        "package_hash": "",
        "services": [],
        "protocol_frontends": [],
        "capabilities": [],
        "workflows": [],
        "warnings": [],
        "errors": [error],
        "ok": False,
        "schema_version": None,
        "kind": None,
        "id": source.get("id"),
        "version": None,
        "object_counts": {
            "services": 0,
            "protocol_frontends": 0,
            "capabilities": 0,
            "workflows": 0,
        },
    }


def _validate_manifest_metadata(manifest: Mapping[str, Any], errors: list[str]) -> None:
    for key in ("schema_version", "kind", "id", "version"):
        if manifest.get(key) in (None, ""):
            errors.append(f"extension.yml: missing required field {key}")
    if manifest.get("kind") not in (None, PACKAGE_KIND):
        errors.append(f"extension.yml: kind must be {PACKAGE_KIND!r}")


def _object_refs(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"extension.yml: {field} must be a list")
        return []

    refs: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            refs.append(item)
            continue
        if isinstance(item, Mapping) and isinstance(item.get("path"), str):
            refs.append(item["path"])
            continue
        errors.append(f"extension.yml: {field}[{index}] must be a path string or mapping with path")
    return refs


def _load_referenced_object(
    root: Path,
    ref: str,
    expected_kind: str | None,
    errors: list[str],
    hash_files: dict[str, bytes],
) -> Mapping[str, Any] | None:
    path = _resolve_package_path(root, ref, errors)
    if path is None:
        return None
    record = _load_yaml_file(path, errors, hash_files, root)
    if not isinstance(record, Mapping):
        return None
    kind = record.get("kind")
    if expected_kind is not None and kind not in (None, expected_kind):
        errors.append(f"{ref}: kind must be {expected_kind!r} when present")
    return record


def _load_yaml_file(
    path: Path,
    errors: list[str],
    hash_files: dict[str, bytes],
    root: Path,
) -> Any:
    if not path.exists():
        errors.append(f"{_display_path(path, root)}: file not found")
        return None
    if not path.is_file():
        errors.append(f"{_display_path(path, root)}: expected a file")
        return None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        errors.append(f"{_display_path(path, root)}: failed to read file: {exc}")
        return None

    hash_files[_display_path(path, root)] = payload
    try:
        return yaml.safe_load(payload.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        errors.append(f"{_display_path(path, root)}: invalid YAML: {exc}")
        return None


def _resolve_package_path(root: Path, ref: str, errors: list[str]) -> Path | None:
    raw_path = Path(ref)
    if raw_path.is_absolute():
        errors.append(f"{ref}: absolute paths are not allowed")
        return None
    path = (root / raw_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        errors.append(f"{ref}: path traversal is not allowed")
        return None
    return path


def _normalize_service(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(record)
    service_id = service_registry.normalize_id(raw.get("id"))
    config = raw.get("config")
    if isinstance(config, Mapping):
        safe_config, secret_refs = service_registry.split_secret_refs(config)
        raw["config"] = safe_config
        raw["secret_refs"] = {**dict(raw.get("secret_refs") or {}), **secret_refs}
    return service_registry.normalize_service_record(service_id, raw)


def _with_import_provenance(
    record: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    raw = copy.deepcopy(dict(record))
    raw["source"] = "imported"
    existing = raw.get("provenance")
    if not isinstance(existing, Mapping):
        existing = {}
    raw["provenance"] = {**dict(existing), **dict(provenance)}
    return raw


def _normalize_protocol_frontend(record: Mapping[str, Any]) -> dict[str, Any]:
    frontend_id = service_registry.normalize_id(record.get("id"))
    return service_registry.normalize_protocol_frontend(frontend_id, record)


def _normalize_capability(
    record: Mapping[str, Any],
    ref: str,
    errors: list[str],
) -> dict[str, Any] | None:
    raw = copy.deepcopy(dict(record))
    raw.pop("kind", None)
    candidate_id = raw.pop("id", None)
    try:
        resources = raw.get("resources")
        if isinstance(resources, Mapping):
            raw["resources"] = ResourceEstimate(**dict(resources))
        capability = Capability(**raw)
    except (TypeError, ValueError) as exc:
        errors.append(f"{ref}: invalid capability: {exc}")
        return None

    normalized = summarize_capability(capability, audience="internal")
    if candidate_id not in (None, ""):
        normalized["id"] = str(candidate_id)
    else:
        normalized["id"] = _capability_candidate_id(capability)
    return normalized


def _normalize_workflow(
    record: Mapping[str, Any],
    ref: str,
    root: Path,
    errors: list[str],
) -> dict[str, Any] | None:
    manifest = copy.deepcopy(dict(record))
    manifest.pop("kind", None)
    try:
        validate_manifest(manifest)
        identity = manifest_identity(manifest)
    except (ManifestError, ValueError) as exc:
        errors.append(f"{ref}: invalid workflow manifest: {exc}")
        return None

    manifest_path = _resolve_package_path(root, ref, errors) or Path(ref)
    workflow = WorkflowRecord(
        manifest=manifest,
        identity=identity,
        source_path=root,
    )
    normalized = summarize_workflow(workflow, audience="internal")
    normalized["id"] = workflow.id
    normalized["manifest"] = manifest
    normalized["manifest_path"] = str(manifest_path)
    return normalized


def _check_code_bearing_adapter(
    record: Mapping[str, Any],
    collection: str,
    errors: list[str],
) -> None:
    adapter = record.get("adapter")
    has_code_adapter = isinstance(adapter, Mapping) or any(
        key in record for key in CODE_BEARING_ADAPTER_KEYS - {"adapter"}
    )
    if not has_code_adapter:
        return

    record_id = record.get("id") or "<unknown>"
    errors.append(
        f"{collection}.{record_id}: code-bearing adapters are not allowed in data-only packages"
    )


def _object_counts(
    services: list[dict[str, Any]],
    protocol_frontends: list[dict[str, Any]],
    capabilities: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
) -> dict[str, int]:
    return {
        "services": len(services),
        "protocol_frontends": len(protocol_frontends),
        "capabilities": len(capabilities),
        "workflows": len(workflows),
    }


def _capability_candidate_id(capability: Capability) -> str:
    parts = [capability.intent, capability.backend]
    if capability.model:
        parts.append(capability.model)
    if capability.workflow:
        parts.append(capability.workflow)
    return ":".join(parts)


def _package_hash(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
