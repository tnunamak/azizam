"""Pure workflow dependency checks for ComfyUI-backed manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from gateway.manifests import ManifestError


@dataclass(frozen=True)
class RequirementIssue:
    kind: str
    name: str
    reason: str


@dataclass(frozen=True)
class WorkflowRequirementsResult:
    missing: list[RequirementIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "missing": [
                {"kind": issue.kind, "name": issue.name, "reason": issue.reason}
                for issue in self.missing
            ],
        }


def validate_workflow_requirements(
    manifest: dict[str, Any],
    backend_inventory: dict[str, Any] | None,
) -> WorkflowRequirementsResult:
    """Compare optional manifest requirements with an observed backend inventory.

    The return value is safe to surface to clients: it includes only requirement
    names from the manifest and generic reasons, not raw backend paths.
    """
    requirements = _requirements(manifest)
    inventory = backend_inventory or {}
    node_classes = _observed_node_classes(inventory)
    model_artifacts = _observed_model_artifacts(inventory)

    missing: list[RequirementIssue] = []
    for required_class in _required_custom_node_classes(requirements):
        if required_class not in node_classes:
            missing.append(
                RequirementIssue(
                    kind="custom_node",
                    name=required_class,
                    reason="custom node class is not present in backend inventory",
                )
            )

    for requirement in _required_model_artifacts(requirements):
        if not _model_requirement_satisfied(requirement, model_artifacts):
            missing.append(
                RequirementIssue(
                    kind="model",
                    name=_model_requirement_name(requirement),
                    reason=_model_missing_reason(requirement),
                )
            )

    return WorkflowRequirementsResult(missing=missing)


def _requirements(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a mapping")
    requirements = manifest.get("requires", {})
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ManifestError("manifest requires must be a mapping")
    allowed = {"custom_nodes", "models"}
    unknown = sorted(set(requirements) - allowed)
    if unknown:
        raise ManifestError(f"manifest requires has unknown field: {unknown[0]}")
    return requirements


def _required_custom_node_classes(requirements: dict[str, Any]) -> list[str]:
    entries = requirements.get("custom_nodes", [])
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ManifestError("manifest requires.custom_nodes must be a list")

    classes: list[str] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            class_name = entry
        elif isinstance(entry, dict):
            class_name = entry.get("class")
            if set(entry) - {"class"}:
                raise ManifestError(
                    f"manifest requires.custom_nodes[{index}] has unknown field"
                )
        else:
            raise ManifestError(
                f"manifest requires.custom_nodes[{index}] must be a string or mapping"
            )
        if not isinstance(class_name, str) or not class_name.strip():
            raise ManifestError(
                f"manifest requires.custom_nodes[{index}].class must be a non-empty string"
            )
        classes.append(class_name.strip())
    return classes


def _required_model_artifacts(requirements: dict[str, Any]) -> list[dict[str, str]]:
    entries = requirements.get("models", [])
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ManifestError("manifest requires.models must be a list")

    models: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"manifest requires.models[{index}] must be a mapping")
        allowed = {"role", "path", "pattern", "sha256"}
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise ManifestError(
                f"manifest requires.models[{index}] has unknown field: {unknown[0]}"
            )
        role = entry.get("role")
        path = entry.get("path")
        pattern = entry.get("pattern")
        sha256 = entry.get("sha256")
        if not isinstance(role, str) or not role.strip():
            raise ManifestError(
                f"manifest requires.models[{index}].role must be a non-empty string"
            )
        if (path is None) == (pattern is None):
            raise ManifestError(
                f"manifest requires.models[{index}] must set exactly one of path or pattern"
            )
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ManifestError(
                f"manifest requires.models[{index}].path must be a non-empty string"
            )
        if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
            raise ManifestError(
                f"manifest requires.models[{index}].pattern must be a non-empty string"
            )
        if sha256 is not None and (not isinstance(sha256, str) or not sha256.strip()):
            raise ManifestError(
                f"manifest requires.models[{index}].sha256 must be a non-empty string"
            )

        normalized = {"role": role.strip()}
        if path is not None:
            normalized["path"] = path.strip()
        if pattern is not None:
            normalized["pattern"] = pattern.strip()
        if sha256 is not None:
            normalized["sha256"] = sha256.strip().lower()
        models.append(normalized)
    return models


def _observed_node_classes(inventory: dict[str, Any]) -> set[str]:
    raw_classes = (
        inventory.get("node_classes")
        or inventory.get("custom_node_classes")
        or inventory.get("custom_nodes")
        or []
    )
    if isinstance(raw_classes, dict):
        raw_classes = raw_classes.get("classes", [])
    if not isinstance(raw_classes, list | set | tuple):
        return set()
    return {item.strip() for item in raw_classes if isinstance(item, str) and item.strip()}


def _observed_model_artifacts(inventory: dict[str, Any]) -> list[dict[str, str]]:
    raw_models = (
        inventory.get("models")
        or inventory.get("model_artifacts")
        or inventory.get("artifacts")
        or []
    )
    if isinstance(raw_models, dict):
        raw_models = raw_models.get("artifacts", [])
    if not isinstance(raw_models, list | tuple):
        return []

    artifacts: list[dict[str, str]] = []
    for item in raw_models:
        if isinstance(item, str):
            artifacts.append({"path": item, "name": _safe_path_name(item)})
            continue
        if not isinstance(item, dict):
            continue
        artifact: dict[str, str] = {}
        for key in ("role", "path", "name", "sha256"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                artifact[key] = value.strip()
        if artifact:
            artifacts.append(artifact)
    return artifacts


def _model_requirement_satisfied(
    requirement: dict[str, str],
    artifacts: list[dict[str, str]],
) -> bool:
    for artifact in artifacts:
        if artifact.get("role") != requirement["role"]:
            continue
        if not _artifact_matches_location(requirement, artifact):
            continue
        expected_hash = requirement.get("sha256")
        observed_hash = artifact.get("sha256", "").lower()
        if expected_hash and observed_hash != expected_hash:
            continue
        return True
    return False


def _artifact_matches_location(requirement: dict[str, str], artifact: dict[str, str]) -> bool:
    observed_path = artifact.get("path", "")
    observed_name = artifact.get("name", "") or _safe_path_name(observed_path)
    if "path" in requirement:
        expected_name = _safe_path_name(requirement["path"])
        return observed_path == requirement["path"] or observed_name == expected_name
    pattern = requirement["pattern"]
    return (
        fnmatch(observed_path, pattern)
        or fnmatch(observed_path, f"*/{pattern}")
        or fnmatch(observed_name, pattern)
    )


def _model_requirement_name(requirement: dict[str, str]) -> str:
    role = requirement["role"]
    if "path" in requirement:
        return f"{role}:{_safe_path_name(requirement['path'])}"
    return f"{role}:{requirement['pattern']}"


def _model_missing_reason(requirement: dict[str, str]) -> str:
    if "sha256" in requirement:
        return "matching model artifact with expected hash is not present in backend inventory"
    return "model artifact is not present in backend inventory"


def _safe_path_name(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name or path
