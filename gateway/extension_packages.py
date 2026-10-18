"""Extension package parser and validator.

Data objects can be enabled into the runtime registry immediately. Code
objects are parsed and indexed for review, but execution stays behind explicit
trusted adapter/translator machinery instead of importing arbitrary package
code into the gateway process.
"""

from __future__ import annotations

import copy
import hashlib
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
from gateway import engine_redaction, frontend_mount, service_registry


MANIFEST_NAME = "extension.yml"
PACKAGE_KIND = "extension_package"
EXTENSION_PACKAGE_INDEX_SETTING_KEY = "extension_package_index"
DATA_OBJECT_KINDS = frozenset(
    {"services", "protocol_frontends", "capabilities", "workflows"}
)
CODE_OBJECT_KINDS = frozenset({"adapters", "translators"})
SUPPORTED_OBJECT_KINDS = DATA_OBJECT_KINDS | CODE_OBJECT_KINDS
CODE_OBJECT_MODES = frozenset({"metadata-only", "bundled-python", "sidecar"})
CODE_BEARING_ADAPTER_KEYS = frozenset(
    {
        "adapter",
        "adapter_class",
        "adapter_module",
        "class",
        "code",
        "command",
        "entrypoint",
        "entrypoints",
        "handler",
        "module",
        "python",
    }
)


def public_package_source(source: Any) -> Any:
    """A package source as a response shows it: a git URL without its user name, password and query."""
    return engine_redaction.redact_config(source)


def public_package_index(index: Mapping[str, Any]) -> dict[str, Any]:
    """The package index as a response shows it. The stored index keeps what the owner entered."""
    out = normalize_extension_package_index(index)
    out["sources"] = [public_package_source(source) for source in out["sources"]]
    for package in out["packages"].values():
        if "source" in package:
            package["source"] = public_package_source(package["source"])
    return out


def public_package_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """An enable, remove or list result with its package sources and reviewed source made public."""
    out = dict(result)
    if isinstance(out.get("extension_packages"), Mapping):
        out["extension_packages"] = public_package_index(out["extension_packages"])
    if isinstance(out.get("extension_package"), Mapping):
        package = dict(out["extension_package"])
        if "source" in package:
            package["source"] = public_package_source(package["source"])
        out["extension_package"] = package
    if isinstance(out.get("review"), Mapping):
        out["review"] = public_package_review(out["review"])
    return out


def public_package_review(review: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(review)
    if "resolved_source" in out:
        out["resolved_source"] = public_package_source(out["resolved_source"])
    return out


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
        source.get("resolved_ref")
        or source.get("commit")
        or source.get("ref")
        or source.get("tag")
        or source.get("branch")
        or ""
    ).strip()
    subdir = str(source.get("subdir") or source.get("path_in_repo") or "").strip()
    try:
        if install_root is None:
            with tempfile.TemporaryDirectory(prefix="azizam-extension-") as tmp:
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


def source_reads_gateway_host(source: Mapping[str, Any]) -> bool:
    """True when a package source can read files on the gateway host.

    A directory path does, and so does a git URL that is not plain ``https://``
    (a local path, ``file://``, ``ssh://``). Only the owner may name those.
    """
    source_type = str(source.get("type") or source.get("kind") or "local_directory")
    if source_type == "local_directory":
        return True
    if source_type in {"git", "github", "git_url"}:
        return not git_url_is_https(_git_source_url(source))
    return False


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


def git_url_is_https(url: str) -> bool:
    """True for a plain ``https://`` URL: the only kind that cannot reach the gateway host's files."""
    return url.lower().startswith("https://") and "::" not in url


def _check_git_arguments(url: str, ref: str) -> None:
    """Refuse a URL or ref that git would read as an option or as a remote helper."""
    for label, value in (("url", url), ("ref", ref)):
        if value.startswith("-"):
            raise ValueError(f"git extension package {label} cannot start with '-'")
    if "::" in url:
        raise ValueError("git extension package url cannot name a remote helper")


def _clone_git_source(url: str, ref: str, target: Path) -> str:
    _check_git_arguments(url, ref)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "clone", "--depth", "1", "--", url, str(target)])
    if ref:
        _run_git(["git", "-C", str(target), "fetch", "--depth", "1", "origin", "--", ref])
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
        "adapters": sorted(
            str(record.get("id"))
            for record in review.get("adapters") or []
            if isinstance(record, Mapping) and record.get("id")
        ),
        "translators": sorted(
            str(record.get("id"))
            for record in review.get("translators") or []
            if isinstance(record, Mapping) and record.get("id")
        ),
        "code_objects": copy.deepcopy(list(review.get("code_objects") or [])),
        "requires_code_trust": bool(review.get("requires_code_trust")),
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


def remove_extension_package(
    index: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    """Return a new index with the named package removed.

    Raises ``KeyError`` if ``package_id`` is not present.
    """
    out = normalize_extension_package_index(index)
    if package_id not in out["packages"]:
        raise KeyError(f"extension package not found: {package_id!r}")
    del out["packages"][package_id]
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
    adapters: list[dict[str, Any]] = []
    translators: list[dict[str, Any]] = []
    hash_files: dict[str, bytes] = {}

    manifest_path = root / MANIFEST_NAME
    manifest = _load_yaml_file(manifest_path, errors, hash_files, root)
    if not isinstance(manifest, Mapping):
        return _review(
            manifest={},
            object_counts=_object_counts(
                services,
                protocol_frontends,
                capabilities,
                workflows,
                adapters,
                translators,
            ),
            services=[],
            protocol_frontends=[],
            capabilities=[],
            workflows=[],
            adapters=[],
            translators=[],
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
            errors.extend(f"{raw_ref}: {problem}" for problem in frontend_mount.package_row_problems(record))
            errors.extend(f"{raw_ref}: {problem}" for problem in frontend_mount.unroutable_paths(record))
            if service_registry.normalize_id(record.get("id")) in service_registry.builtin_frontend_ids():
                errors.append(f"{raw_ref}: protocol frontend {record.get('id')!r} is a builtin frontend; a package cannot replace it")
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
            normalized = _normalize_workflow(record, raw_ref, root, errors, hash_files)
            if normalized is not None:
                workflows.append(normalized)

    for raw_ref in _object_refs(objects.get("adapters"), "objects.adapters", errors):
        record = _load_referenced_object(root, raw_ref, "adapter", errors, hash_files)
        if isinstance(record, Mapping):
            normalized = _normalize_code_object(
                record,
                raw_ref,
                "adapters",
                root,
                hash_files,
                errors,
                warnings,
            )
            if normalized is not None:
                adapters.append(normalized)

    for raw_ref in _object_refs(objects.get("translators"), "objects.translators", errors):
        record = _load_referenced_object(root, raw_ref, "translator", errors, hash_files)
        if isinstance(record, Mapping):
            normalized = _normalize_code_object(
                record,
                raw_ref,
                "translators",
                root,
                hash_files,
                errors,
                warnings,
            )
            if normalized is not None:
                translators.append(normalized)

    return _review(
        manifest=manifest,
        object_counts=_object_counts(
            services,
            protocol_frontends,
            capabilities,
            workflows,
            adapters,
            translators,
        ),
        services=services,
        protocol_frontends=protocol_frontends,
        capabilities=capabilities,
        workflows=workflows,
        adapters=adapters,
        translators=translators,
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
    adapters: list[dict[str, Any]],
    translators: list[dict[str, Any]],
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
        "adapters": adapters,
        "translators": translators,
        "code_objects": _code_object_summaries(adapters, translators),
        "requires_code_trust": _requires_code_trust(adapters, translators),
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
        "adapters": [],
        "translators": [],
        "code_objects": [],
        "requires_code_trust": False,
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
    hash_files: dict[str, bytes],
) -> dict[str, Any] | None:
    manifest = copy.deepcopy(dict(record))
    manifest.pop("kind", None)
    try:
        validate_manifest(manifest)
        identity = manifest_identity(manifest)
    except (ManifestError, ValueError) as exc:
        errors.append(f"{ref}: invalid workflow manifest: {exc}")
        return None

    _hash_workflow_package_artifacts(manifest, ref, root, errors, hash_files)
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


def _normalize_code_object(
    record: Mapping[str, Any],
    ref: str,
    collection: str,
    root: Path,
    hash_files: dict[str, bytes],
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    raw = copy.deepcopy(dict(record))
    raw_id = str(raw.get("id") or "").strip()
    if not raw_id:
        errors.append(f"{ref}: code object requires id")
        return None

    mode = str(raw.get("mode") or "metadata-only").strip()
    if mode not in CODE_OBJECT_MODES:
        errors.append(
            f"{ref}: mode must be one of {', '.join(sorted(CODE_OBJECT_MODES))}"
        )
        return None

    interfaces = raw.get("interfaces", raw.get("interface"))
    normalized_interfaces = service_registry.normalize_string_list(interfaces)
    if not normalized_interfaces:
        errors.append(f"{ref}: code object requires interface or interfaces")
        return None

    permissions = raw.get("permissions")
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, Mapping):
        errors.append(f"{ref}: permissions must be a mapping when present")
        return None

    entrypoints = _normalize_entrypoints(raw)
    if mode != "metadata-only" and not entrypoints:
        errors.append(f"{ref}: {mode} code object requires entrypoint, entrypoints, or module")
        return None

    package_path = _normalize_code_package_path(raw, ref, root, hash_files, errors)

    normalized = {
        "id": raw_id,
        "kind": collection[:-1],
        "name": str(raw.get("name") or raw_id),
        "mode": mode,
        "language": str(raw.get("language") or "python"),
        "interfaces": normalized_interfaces,
        "entrypoints": entrypoints,
        "permissions": copy.deepcopy(dict(permissions)),
        "trust_tier": str(raw.get("trust_tier") or "untrusted"),
        "description": str(raw.get("description") or ""),
    }
    if package_path:
        normalized["package_path"] = package_path
    if _is_executable_code_object(normalized):
        warnings.append(
            f"{collection}.{raw_id}: code object is staged for review; "
            "the gateway does not execute package code automatically"
        )
    return normalized


def _hash_workflow_package_artifacts(
    manifest: Mapping[str, Any],
    ref: str,
    root: Path,
    errors: list[str],
    hash_files: dict[str, bytes],
) -> None:
    bindings = manifest.get("bindings")
    if not isinstance(bindings, Mapping):
        return
    for backend, binding in bindings.items():
        if not isinstance(binding, Mapping):
            continue
        workflow_artifact = binding.get("workflow")
        if workflow_artifact is not None:
            _hash_package_artifact_value(
                workflow_artifact,
                f"{ref}: bindings.{backend}.workflow",
                root,
                errors,
                hash_files,
            )


def _hash_package_artifact_value(
    value: Any,
    field: str,
    root: Path,
    errors: list[str],
    hash_files: dict[str, bytes],
) -> None:
    if isinstance(value, str):
        _hash_package_uri(value, field, root, errors, hash_files)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _hash_package_artifact_value(
                item,
                f"{field}.{key}",
                root,
                errors,
                hash_files,
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _hash_package_artifact_value(
                item,
                f"{field}[{index}]",
                root,
                errors,
                hash_files,
            )


def _hash_package_uri(
    uri: str,
    field: str,
    root: Path,
    errors: list[str],
    hash_files: dict[str, bytes],
) -> None:
    if not uri.startswith("package://"):
        return
    rel = uri.removeprefix("package://").lstrip("/")
    if not rel:
        errors.append(f"{field}: package URI must include a path")
        return
    path = _resolve_package_path(root, rel, errors)
    if path is None:
        return
    if path.is_file():
        _hash_code_file(path, root, hash_files, errors)
        return
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            _hash_code_file(child, root, hash_files, errors)
        return
    errors.append(f"{field}: package artifact not found: {uri}")


def _normalize_code_package_path(
    record: Mapping[str, Any],
    ref: str,
    root: Path,
    hash_files: dict[str, bytes],
    errors: list[str],
) -> str:
    raw_path = record.get("package_path")
    if raw_path in (None, ""):
        return ""
    if not isinstance(raw_path, str):
        errors.append(f"{ref}: package_path must be a relative path")
        return ""

    path = _resolve_package_path(root, raw_path, errors)
    if path is None:
        return ""
    if path.is_file():
        _hash_code_file(path, root, hash_files, errors)
        return raw_path
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            if "__pycache__" in child.parts or child.name.endswith(".pyc"):
                continue
            _hash_code_file(child, root, hash_files, errors)
        return raw_path.rstrip("/")

    errors.append(f"{raw_path}: package_path must point at a file or directory")
    return ""


def _hash_code_file(
    path: Path,
    root: Path,
    hash_files: dict[str, bytes],
    errors: list[str],
) -> None:
    try:
        hash_files[_display_path(path, root)] = path.read_bytes()
    except OSError as exc:
        errors.append(f"{_display_path(path, root)}: failed to read code file: {exc}")


def _normalize_entrypoints(record: Mapping[str, Any]) -> dict[str, str]:
    entrypoints = record.get("entrypoints")
    if isinstance(entrypoints, Mapping):
        return {
            str(key): str(value)
            for key, value in entrypoints.items()
            if str(key).strip() and str(value).strip()
        }

    for key in ("entrypoint", "module", "handler", "command"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return {"default": value.strip()}
    return {}


def _is_executable_code_object(record: Mapping[str, Any]) -> bool:
    return str(record.get("mode") or "metadata-only") != "metadata-only" or bool(
        record.get("entrypoints")
    )


def _code_object_summaries(
    adapters: list[dict[str, Any]],
    translators: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for collection, records in (("adapters", adapters), ("translators", translators)):
        for record in records:
            out.append(
                {
                    "id": record.get("id"),
                    "kind": collection[:-1],
                    "mode": record.get("mode"),
                    "interfaces": copy.deepcopy(list(record.get("interfaces") or [])),
                    "permissions": copy.deepcopy(dict(record.get("permissions") or {})),
                    "trust_tier": record.get("trust_tier"),
                }
            )
    return out


def _requires_code_trust(
    adapters: list[dict[str, Any]],
    translators: list[dict[str, Any]],
) -> bool:
    return any(_is_executable_code_object(record) for record in [*adapters, *translators])


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
    adapters: list[dict[str, Any]] | None = None,
    translators: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    counts = {
        "services": len(services),
        "protocol_frontends": len(protocol_frontends),
        "capabilities": len(capabilities),
        "workflows": len(workflows),
    }
    if adapters:
        counts["adapters"] = len(adapters)
    if translators:
        counts["translators"] = len(translators)
    return counts


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
