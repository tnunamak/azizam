from __future__ import annotations

import subprocess
from pathlib import Path

from gateway.extension_packages import (
    EXTENSION_PACKAGE_INDEX_SETTING_KEY,
    _git_source_url,
    empty_extension_package_index,
    imported_registry_records,
    merge_package_index,
    normalize_extension_package_index,
    package_index_record,
    remove_extension_package,
    review_local_directory_package,
    validate_extension_package_source,
)


def _write_package(tmp_path: Path, manifest: str, files: dict[str, str] | None = None) -> Path:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "extension.yml").write_text(manifest, encoding="utf-8")
    for name, payload in (files or {}).items():
        path = package_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    return package_dir


def _write_package_at(
    package_dir: Path,
    manifest: str,
    files: dict[str, str] | None = None,
) -> Path:
    package_dir.mkdir(parents=True)
    (package_dir / "extension.yml").write_text(manifest, encoding="utf-8")
    for name, payload in (files or {}).items():
        path = package_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    return package_dir


def _commit_package_repo(package_dir: Path) -> str:
    subprocess.run(["git", "init"], cwd=package_dir, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=package_dir, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "package",
        ],
        cwd=package_dir,
        check=True,
        stdout=subprocess.PIPE,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=package_dir,
        text=True,
    ).strip()


def test_valid_local_directory_package_parses_data_objects(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: demo-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
  protocol_frontends:
    - path: objects/frontend.yml
""",
        {
            "objects/service.yml": """
id: demo-service
name: Demo Service
source: provisioned
capabilities: [chat]
config:
  base_url: http://localhost:9999
""",
            "objects/frontend.yml": """
id: demo-api
name: Demo API
protocol: demo
source: builtin
routes:
  - /demo/v1
intents:
  - demo.run
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["errors"] == []
    assert review["ok"] is True
    assert review["warnings"] == []
    assert review["package"] == {
        "schema_version": 1,
        "kind": "extension_package",
        "id": "demo-package",
        "version": "0.1.0",
    }
    assert review["object_counts"] == {
        "services": 1,
        "protocol_frontends": 1,
        "capabilities": 0,
        "workflows": 0,
    }
    assert review["services"][0]["id"] == "demo-service"
    assert review["services"][0]["config"]["base_url"] == "http://localhost:9999"
    assert review["protocol_frontends"][0]["routes"] == ["/demo/v1"]
    assert len(review["package_hash"]) == 64


def test_missing_manifest_returns_validation_error(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert review["object_counts"] == {
        "services": 0,
        "protocol_frontends": 0,
        "capabilities": 0,
        "workflows": 0,
    }
    assert review["errors"] == ["extension.yml: file not found"]


def test_rejects_referenced_path_traversal(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: bad-package
version: 0.1.0
objects:
  services:
    - ../outside.yml
""",
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert review["services"] == []
    assert "../outside.yml: path traversal is not allowed" in review["errors"]


def test_unknown_object_kinds_are_reported_as_deferred_warnings(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: future-package
version: 0.1.0
objects:
  widgets:
    - objects/widget.yml
""",
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is True
    assert review["errors"] == []
    assert review["warnings"] == [
        "extension.yml: objects.widgets is deferred; unknown object kind"
    ]


def test_local_directory_package_parses_capability_and_workflow_objects(
    tmp_path: Path,
) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: data-package
version: 0.1.0
objects:
  capabilities:
    - objects/capability.yml
  workflows:
    - objects/workflow.yml
""",
        {
            "objects/capability.yml": """
kind: capability
id: local-image-capability
intent: image.generate
backend: comfyui.local
workflow: image.generate.demo
input_modalities: [text]
output_modalities: [image]
execution: job
streaming: false
exposure: opt_in
trust_tier: experimental
resource_tier: batch
tags: [demo]
resources:
  gpu_required: true
  vram_gb_estimate: 8
caveats:
  - review-only package object
""",
            "objects/workflow.yml": """
schema_version: 1
version: 0.1.0
id: image.generate.demo
intent: image.generate
source: package
trusted: false
inputs:
  prompt:
    type: string
    required: true
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/demo.json
    map:
      prompt:
        - node: "6"
          input: text
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is True
    assert review["errors"] == []
    assert review["warnings"] == []
    assert review["object_counts"] == {
        "services": 0,
        "protocol_frontends": 0,
        "capabilities": 1,
        "workflows": 1,
    }

    capability = review["capabilities"][0]
    assert capability["id"] == "local-image-capability"
    assert capability["intent"] == "image.generate"
    assert capability["backend"] == "comfyui.local"
    assert capability["workflow"] == "image.generate.demo"
    assert capability["resources"] == {
        "gpu_required": True,
        "vram_gb_estimate": 8,
    }

    workflow = review["workflows"][0]
    assert workflow["id"] == "image.generate.demo"
    assert workflow["intent"] == "image.generate"
    assert workflow["supported_backends"] == ["comfyui"]
    assert workflow["bindings"][0]["backend"] == "comfyui"
    assert workflow["bindings"][0]["mapped_inputs"] == ["prompt"]
    assert workflow["bindings"][0]["map"] == {
        "prompt": [{"node": "6", "input": "text"}]
    }


def test_workflow_package_uri_artifact_must_exist(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: missing-artifact-package
version: 0.1.0
objects:
  workflows:
    - objects/workflow.yml
""",
        {
            "objects/workflow.yml": """
schema_version: 1
version: 0.1.0
id: image.generate.missing-artifact
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: package://workflows/missing.json
    map:
      prompt:
        - node: "6"
          input: text
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert review["errors"] == [
        "objects/workflow.yml: bindings.comfyui.workflow: "
        "package artifact not found: package://workflows/missing.json"
    ]


def test_workflow_package_uri_artifact_cannot_traverse(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: traversal-artifact-package
version: 0.1.0
objects:
  workflows:
    - objects/workflow.yml
""",
        {
            "objects/workflow.yml": """
schema_version: 1
version: 0.1.0
id: image.generate.traversal-artifact
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: package://../outside.json
    map:
      prompt:
        - node: "6"
          input: text
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert "../outside.json: path traversal is not allowed" in review["errors"]


def test_package_hash_includes_workflow_package_uri_artifact(tmp_path: Path) -> None:
    manifest = """
schema_version: 1
kind: extension_package
id: artifact-hash-package
version: 0.1.0
objects:
  workflows:
    - objects/workflow.yml
"""
    workflow_manifest = """
schema_version: 1
version: 0.1.0
id: image.generate.artifact-hash
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: package://workflows/demo.json
    map:
      prompt:
        - node: "6"
          input: text
"""
    package_dir = _write_package(
        tmp_path,
        manifest,
        {
            "objects/workflow.yml": workflow_manifest,
            "workflows/demo.json": '{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "one"}}}',
        },
    )
    before = review_local_directory_package(package_dir)

    (package_dir / "workflows" / "demo.json").write_text(
        '{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "two"}}}',
        encoding="utf-8",
    )
    after = review_local_directory_package(package_dir)

    assert before["ok"] is True
    assert after["ok"] is True
    assert before["package_hash"] != after["package_hash"]


def test_invalid_capability_and_workflow_objects_return_validation_errors(
    tmp_path: Path,
) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: invalid-data-package
version: 0.1.0
objects:
  capabilities:
    - objects/capability.yml
  workflows:
    - objects/workflow.yml
""",
        {
            "objects/capability.yml": """
kind: capability
id: bad-capability
intent: image.generate
backend: comfyui.local
execution: forever
""",
            "objects/workflow.yml": """
schema_version: 1
id: bad-workflow
intent: image.generate
inputs: {}
outputs: {}
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert review["capabilities"] == []
    assert review["workflows"] == []
    assert review["object_counts"] == {
        "services": 0,
        "protocol_frontends": 0,
        "capabilities": 0,
        "workflows": 0,
    }
    assert (
        "objects/capability.yml: invalid capability: execution must be one of"
        in review["errors"][0]
    )
    assert review["errors"][1] == (
        "objects/workflow.yml: invalid workflow manifest: "
        "manifest missing required field: bindings"
    )


def test_code_bearing_adapter_is_an_error(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: code-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
""",
        {
            "objects/service.yml": """
id: coded-service
name: Coded Service
adapter:
  module: gateway.adapters.demo
config: {}
""",
        },
    )

    review = review_local_directory_package(package_dir)

    assert review["ok"] is False
    assert review["services"][0]["id"] == "coded-service"
    assert review["errors"] == [
        "services.coded-service: code-bearing adapters are not allowed in data-only packages"
    ]


def test_secret_looking_service_config_is_split_via_service_registry(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: secret-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
""",
        {
            "objects/service.yml": """
id: secret-service
name: Secret Service
config:
  api_key: sk-test
  api_key_env: SECRET_SERVICE_API_KEY
  base_url: http://localhost:9999
""",
        },
    )

    review = review_local_directory_package(package_dir)

    service = review["services"][0]
    assert review["errors"] == []
    assert service["config"] == {
        "api_key_env": "SECRET_SERVICE_API_KEY",
        "base_url": "http://localhost:9999",
    }
    assert service["secret_refs"] == {"api_key": "<external-secret>"}


def test_validate_extension_package_source_accepts_local_directory(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: source-package
version: 0.1.0
objects: {}
""",
    )

    review = validate_extension_package_source(
        {"type": "local_directory", "path": str(package_dir)}
    )

    assert review["ok"] is True
    assert review["package"]["id"] == "source-package"


def test_validate_extension_package_source_rejects_non_local_source() -> None:
    review = validate_extension_package_source({"type": "archive", "path": "repo"})

    assert review["ok"] is False
    assert review["errors"] == ["unsupported extension package source type: archive"]


def test_validate_extension_package_source_accepts_git_source(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: git-package
version: 0.1.0
objects: {}
""",
    )
    commit = _commit_package_repo(package_dir)
    install_root = tmp_path / "installed"

    review = validate_extension_package_source(
        {"type": "git", "url": str(package_dir), "ref": commit},
        install_root=install_root,
    )

    assert review["ok"] is True
    assert review["package"]["id"] == "git-package"
    assert review["resolved_source"]["type"] == "git"
    assert review["resolved_source"]["resolved_ref"] == commit
    assert str(install_root) in review["resolved_source"]["path"]


def test_validate_extension_package_source_imports_git_subdir_package(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    package_dir = _write_package_at(
        repo_dir / "packages" / "demo",
        """
schema_version: 1
kind: extension_package
id: git-subdir-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
""",
        {
            "objects/service.yml": """
id: git-subdir-service
name: Git Subdir Service
config:
  base_url: http://localhost:9999
""",
        },
    )
    commit = _commit_package_repo(repo_dir)
    install_root = tmp_path / "installed"

    review = validate_extension_package_source(
        {
            "type": "git",
            "url": str(repo_dir),
            "ref": commit,
            "subdir": "packages/demo",
        },
        install_root=install_root,
    )

    assert review["ok"] is True
    assert review["package"]["id"] == "git-subdir-package"
    assert review["services"][0]["id"] == "git-subdir-service"
    assert review["resolved_source"]["subdir"] == "packages/demo"
    assert Path(review["resolved_source"]["path"]).name == package_dir.name


def test_validate_extension_package_source_rejects_git_subdir_traversal(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    _write_package_at(
        repo_dir / "package",
        """
schema_version: 1
kind: extension_package
id: traversal-package
version: 0.1.0
objects: {}
""",
    )
    commit = _commit_package_repo(repo_dir)

    review = validate_extension_package_source(
        {
            "type": "git",
            "url": str(repo_dir),
            "ref": commit,
            "subdir": "../package",
        },
        install_root=tmp_path / "installed",
    )

    assert review["ok"] is False
    assert review["errors"] == [
        "git extension package subdir cannot traverse outside repo"
    ]


def test_git_repo_shorthand_normalizes_to_github_url() -> None:
    assert (
        _git_source_url({"type": "git", "repo": "owner/name"})
        == "https://github.com/owner/name.git"
    )


def test_validate_extension_package_source_records_resolved_commit_for_branch(
    tmp_path: Path,
) -> None:
    repo_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: git-branch-package
version: 0.1.0
objects: {}
""",
    )
    commit = _commit_package_repo(repo_dir)
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        text=True,
    ).strip()
    assert branch

    review = validate_extension_package_source(
        {"type": "git", "url": str(repo_dir), "branch": branch},
        install_root=tmp_path / "installed",
    )

    assert review["ok"] is True
    assert review["resolved_source"]["ref"] == branch
    assert review["resolved_source"]["resolved_ref"] == commit


def test_extension_package_index_helpers_are_stable() -> None:
    assert EXTENSION_PACKAGE_INDEX_SETTING_KEY == "extension_package_index"
    assert empty_extension_package_index() == {
        "schema_version": 1,
        "sources": [],
        "packages": {},
        "updated_at": 0,
    }

    normalized = normalize_extension_package_index(
        {
            "schema_version": "2",
            "sources": [{"type": "local_directory", "path": "/tmp/pkg"}, "bad"],
            "packages": {"demo": {"id": "demo"}, "bad": []},
            "updated_at": "123",
        }
    )

    assert normalized == {
        "schema_version": 2,
        "sources": [{"type": "local_directory", "path": "/tmp/pkg"}],
        "packages": {"demo": {"id": "demo"}},
        "updated_at": 123,
    }


def test_imported_registry_records_add_package_provenance(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: imported-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
""",
        {
            "objects/service.yml": """
id: imported-service
name: Imported Service
config:
  base_url: http://localhost:9999
""",
        },
    )
    review = review_local_directory_package(package_dir)

    records = imported_registry_records(review)

    service = records["services"]["imported-service"]
    assert service["source"] == "imported"
    assert service["provenance"]["package_id"] == "imported-package"
    assert service["provenance"]["package_version"] == "0.1.0"
    assert service["provenance"]["package_hash"] == review["package_hash"]


def test_imported_registry_records_include_workflows_and_capabilities(
    tmp_path: Path,
) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: imported-data-package
version: 0.1.0
objects:
  capabilities:
    - objects/capability.yml
  workflows:
    - objects/workflow.yml
""",
        {
            "objects/capability.yml": """
kind: capability
id: imported-image-capability
intent: image.generate
backend: comfyui
workflow: image.generate.imported
input_modalities: [text]
output_modalities: [image]
execution: job
resources:
  gpu_required: true
""",
            "objects/workflow.yml": """
schema_version: 1
version: 0.1.0
id: image.generate.imported
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/imported.json
    map:
      prompt:
        - node: "6"
          input: text
""",
        },
    )
    review = review_local_directory_package(package_dir)

    records = imported_registry_records(review)

    workflow = records["workflows"]["image.generate.imported"]
    capability = records["capabilities"]["imported-image-capability"]
    assert workflow["source"] == "imported"
    assert workflow["provenance"]["package_id"] == "imported-data-package"
    assert capability["source"] == "imported"
    assert capability["provenance"]["package_hash"] == review["package_hash"]


def test_package_index_record_and_merge(tmp_path: Path) -> None:
    package_dir = _write_package(
        tmp_path,
        """
schema_version: 1
kind: extension_package
id: indexed-package
version: 0.1.0
objects: {}
""",
    )
    review = review_local_directory_package(package_dir)
    source = {"type": "local_directory", "path": str(package_dir)}

    record = package_index_record(review, source=source)
    index = merge_package_index(empty_extension_package_index(), review, source=source)

    assert record["id"] == "indexed-package"
    assert record["enabled"] is True
    assert record["source"] == source
    assert index["packages"]["indexed-package"]["package_hash"] == review["package_hash"]
    assert index["sources"] == [source]

    removed = remove_extension_package(index, "indexed-package")

    assert removed["packages"] == {}
    assert removed["updated_at"] >= index["updated_at"]


def test_remove_extension_package_rejects_unknown_package() -> None:
    index = empty_extension_package_index()

    try:
        remove_extension_package(index, "missing-package")
    except KeyError as exc:
        assert "missing-package" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected KeyError")


def _lifecycle_fixture():
    import copy
    from gateway.extension_packages import DATA_OBJECT_KINDS
    registry = {kind: {kind + "-one": {
        "id": kind + "-one", "enabled": True, "name": kind,
        "provenance": {"package_id": "pkg", "package_version": "1", "package_hash": "hash"},
    }} for kind in sorted(DATA_OBJECT_KINDS)}
    index = {"packages": {"pkg": {
        "id": "pkg", "enabled": True, "version": "1", "package_hash": "hash",
        "installed_records": copy.deepcopy(registry),
    }}}
    return index, registry


def test_package_lifecycle_removes_unchanged_all_kinds():
    import copy
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    before = copy.deepcopy((index, registry))
    result, rows, affected = change_package_lifecycle(index, registry, "pkg")
    assert result["packages"] == {}
    assert all(not rows[kind] for kind in registry)
    assert len(affected) == 4
    assert {item["action"] for item in affected} == {"removed"}
    assert (index, registry) == before


def test_package_lifecycle_detaches_any_edited_field():
    from gateway.extension_packages import change_package_lifecycle
    for key, value in (("name", "edited"), ("enabled", False), ("custom", {"value": 1})):
        index, registry = _lifecycle_fixture()
        registry["services"]["services-one"][key] = value
        _, rows, affected = change_package_lifecycle(index, registry, "pkg")
        kept = rows["services"]["services-one"]
        assert kept[key] == value
        assert kept["provenance"] == {}
        assert {"kind": "services", "id": "services-one", "action": "detached"} in affected


def test_package_lifecycle_detaches_unproven_legacy_and_copy():
    import copy
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    del index["packages"]["pkg"]["installed_records"]
    registry["services"]["copy"] = copy.deepcopy(registry["services"]["services-one"])
    registry["services"]["copy"]["id"] = "copy"
    _, rows, affected = change_package_lifecycle(index, registry, "pkg")
    assert len(rows["services"]) == 2
    assert all(item["action"] == "detached" for item in affected)


def test_package_lifecycle_disable_enable_restores_each_state():
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    registry["services"]["services-one"]["enabled"] = False
    index, rows, _ = change_package_lifecycle(index, registry, "pkg", mode="disable")
    index, rows, _ = change_package_lifecycle(index, rows, "pkg", mode="disable")
    rows["workflows"]["workflows-one"]["custom"] = "preserve me"
    index, rows, _ = change_package_lifecycle(index, rows, "pkg", mode="enable")
    assert index["packages"]["pkg"]["enabled"] is True
    assert rows["services"]["services-one"]["enabled"] is False
    assert rows["workflows"]["workflows-one"]["enabled"] is True
    assert rows["workflows"]["workflows-one"]["custom"] == "preserve me"


def test_package_lifecycle_remove_disabled_is_not_operator_edit():
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    index, rows, _ = change_package_lifecycle(index, registry, "pkg", mode="disable")
    index, rows, affected = change_package_lifecycle(index, rows, "pkg")
    assert index["packages"] == {}
    assert {item["action"] for item in affected} == {"removed"}


def test_package_lifecycle_refuses_plan_reference_without_mutation():
    import copy
    import pytest
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    before = copy.deepcopy((index, registry))
    with pytest.raises(ValueError, match="access plan"):
        change_package_lifecycle(index, registry, "pkg", references={
            "access plan p": {"direct_engine_access": {"engines": ["services-one"]}},
        })
    assert (index, registry) == before


def test_package_lifecycle_removes_an_alias_the_package_owns_with_its_base():
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    models = {"models": [{"id": "base"}, {"id": "alias", "base_model_id": "base"}]}
    # The package installed these models, so the snapshot holds them too.
    registry["services"]["services-one"]["config"] = models
    index["packages"]["pkg"]["installed_records"]["services"]["services-one"]["config"] = dict(models)
    result, rows, affected = change_package_lifecycle(index, registry, "pkg")
    assert result["packages"] == {}
    assert rows["services"] == {}
    assert {"kind": "services", "id": "services-one", "action": "removed"} in affected


def test_package_lifecycle_restores_enabled_on_an_edited_record_the_package_disabled():
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    index, registry, _ = change_package_lifecycle(index, registry, "pkg", mode="disable")
    assert registry["services"]["services-one"]["enabled"] is False
    registry["services"]["services-one"]["name"] = "edited by the operator"
    _, rows, affected = change_package_lifecycle(index, registry, "pkg")
    kept = rows["services"]["services-one"]
    assert kept["name"] == "edited by the operator"
    assert kept["enabled"] is True
    assert kept["provenance"] == {}
    assert {"kind": "services", "id": "services-one", "action": "detached"} in affected


def test_package_lifecycle_refuses_reference_from_surviving_record():
    import pytest
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    registry["capabilities"]["external"] = {"id": "external", "backend": "services-one"}
    with pytest.raises(ValueError, match="capabilities/external"):
        change_package_lifecycle(index, registry, "pkg")


def test_package_lifecycle_refuses_an_edited_row_that_still_names_a_removed_record():
    # The operator's edit survives the removal as a detached row, so its reference would dangle.
    import pytest
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    registry["services"]["services-one"].update({"name": "edited by the operator", "backend": "workflows-one"})
    with pytest.raises(ValueError, match="services/services-one"):
        change_package_lifecycle(index, registry, "pkg")


def test_package_lifecycle_detaches_an_edited_row_that_names_only_its_own_model():
    from gateway.extension_packages import change_package_lifecycle
    index, registry = _lifecycle_fixture()
    own = {"models": [{"id": "own-model"}]}
    registry["services"]["services-one"].update({"name": "edited", "config": own})
    _, rows, affected = change_package_lifecycle(index, registry, "pkg")
    assert rows["services"]["services-one"]["config"] == own
    assert {"kind": "services", "id": "services-one", "action": "detached"} in affected


def _installed_lifecycle_store(tmp_path, monkeypatch):
    from gateway import admin_store
    from gateway.admin_api import AdminContext
    from gateway.admin_runtime import AdminSQLiteStore
    package = _write_package(tmp_path, """schema_version: 1
kind: extension_package
id: lifecycle
version: "1"
objects:
  services: [service.yml]
""", {"service.yml": """id: lifecycle-service
name: Lifecycle service
config:
  base_url: http://localhost:9999
"""})
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    store = AdminSQLiteStore(path=db)
    monkeypatch.setattr(store, "write_litellm_config", lambda registry: None)
    actor = AdminContext(subject="owner", roles=frozenset({"owner"}))
    source = {"type": "local_directory", "path": str(package)}
    installed = store.enable_extension_package(source=source, actor=actor, expected_generation=0)
    return store, actor, source, installed


def test_package_lifecycle_runtime_index_registry_audit_agree(tmp_path, monkeypatch):
    from gateway import admin_store
    store, actor, source, installed = _installed_lifecycle_store(tmp_path, monkeypatch)
    disabled = store.remove_extension_package(package_id="lifecycle", actor=actor, mode="disable")
    assert disabled["extension_package"]["enabled"] is False
    assert disabled["service_registry"]["services"]["lifecycle-service"]["enabled"] is False
    enabled = store.enable_extension_package(
        source=source, actor=actor,
        expected_generation=disabled["operator_settings_generation"],
    )
    assert enabled["service_registry"]["services"]["lifecycle-service"]["enabled"] is True
    removed = store.remove_extension_package(package_id="lifecycle", actor=actor)
    assert "lifecycle" not in store.get_extension_package_index()["packages"]
    assert "lifecycle-service" not in store.get_service_registry()["services"]
    assert removed["affected_records"] == [
        {"kind": "services", "id": "lifecycle-service", "action": "removed"},
    ]
    # The durable audit representation is checked through the store API.
    audits = admin_store.list_audit(path=store.path)
    assert any(
        row.get("action") == "operator.extension_package.remove"
        and row.get("metadata", {}).get("affected_records") == removed["affected_records"]
        for row in audits
    )


def test_package_lifecycle_runtime_stale_generation_is_atomic(tmp_path, monkeypatch):
    import pytest
    from gateway import admin_store
    store, actor, _, installed = _installed_lifecycle_store(tmp_path, monkeypatch)
    before_index = store.get_extension_package_index()
    before_registry = store.get_service_registry()
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        store.remove_extension_package(
            package_id="lifecycle", actor=actor, expected_generation=0,
        )
    assert store.get_extension_package_index() == before_index
    assert store.get_service_registry() == before_registry


def test_the_stored_index_keeps_a_digest_of_each_installed_record(tmp_path, monkeypatch):
    import json
    import re
    store, _actor, _source, _installed = _installed_lifecycle_store(tmp_path, monkeypatch)
    from gateway import admin_store
    stored = admin_store.get_operator_setting(EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=store.path)["value"]
    entry = stored["packages"]["lifecycle"]["installed_records"]["services"]["lifecycle-service"]
    assert set(entry) == {"kind", "id", "sha256"}
    assert entry["kind"] == "services" and entry["id"] == "lifecycle-service"
    assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    assert "localhost:9999" not in json.dumps(stored)


def test_an_index_from_before_the_digests_is_migrated_once_and_keeps_its_matching(tmp_path):
    import json
    from gateway import admin_store
    from gateway.extension_packages import installed_record_digest
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    record = {
        "id": "old-service", "enabled": True, "name": "Old",
        "config": {"base_url": "http://localhost:9998"},
        "provenance": {"package_id": "old", "package_version": "1", "package_hash": "h"},
    }
    legacy = {"packages": {"old": {
        "id": "old", "version": "1", "enabled": True, "package_hash": "h",
        "installed_records": {"services": {"old-service": record}},
    }}, "sources": [], "updated_at": 1}
    admin_store.put_operator_setting(EXTENSION_PACKAGE_INDEX_SETTING_KEY, legacy, path=db)

    admin_store.init_db(db)
    once = admin_store.get_operator_setting(EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=db)["value"]
    admin_store.init_db(db)
    again = admin_store.get_operator_setting(EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=db)["value"]

    assert again == once
    assert "localhost:9998" not in json.dumps(once)
    entry = once["packages"]["old"]["installed_records"]["services"]["old-service"]
    assert entry == {"kind": "services", "id": "old-service", "sha256": installed_record_digest(record)}
