from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from gateway.extension_packages import (
    imported_registry_records,
    package_index_record,
    validate_extension_package_source,
)


ROOT = Path(__file__).resolve().parent.parent
CODE_PACKAGE = ROOT / "extensions" / "bundled" / "demo-code-package"


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


def test_bundled_code_package_stages_adapters_and_translators() -> None:
    review = validate_extension_package_source(
        {"type": "local_directory", "path": str(CODE_PACKAGE)}
    )

    assert review["ok"] is True
    assert review["errors"] == []
    assert review["package"]["id"] == "test-demo-code-extension-package"
    assert review["object_counts"] == {
        "services": 0,
        "protocol_frontends": 0,
        "capabilities": 0,
        "workflows": 0,
        "adapters": 2,
        "translators": 2,
    }
    assert review["requires_code_trust"] is True
    assert sorted(item["id"] for item in review["code_objects"]) == [
        "test-demo-http-adapter",
        "test-demo-metadata-translator",
        "test-demo-protocol-translator",
        "test-demo-sidecar-adapter",
    ]
    adapters = {record["id"]: record for record in review["adapters"]}
    translators = {record["id"]: record for record in review["translators"]}
    assert adapters["test-demo-http-adapter"]["entrypoints"] == {
        "default": "code.demo_adapter:DemoAdapter"
    }
    assert adapters["test-demo-sidecar-adapter"]["mode"] == "sidecar"
    assert adapters["test-demo-sidecar-adapter"]["entrypoints"] == {
        "default": "python -m demo_sidecar_adapter"
    }
    assert translators["test-demo-protocol-translator"]["entrypoints"] == {
        "request_to_canonical": "code.demo_translator:request_to_canonical",
        "response_from_canonical": "code.demo_translator:response_from_canonical",
    }
    assert translators["test-demo-metadata-translator"]["mode"] == "metadata-only"
    assert translators["test-demo-metadata-translator"]["entrypoints"] == {}
    assert review["warnings"] == [
        "adapters.test-demo-http-adapter: code object is staged for review; "
        "the gateway does not execute package code automatically",
        "adapters.test-demo-sidecar-adapter: code object is staged for review; "
        "the gateway does not execute package code automatically",
        "translators.test-demo-protocol-translator: code object is staged for review; "
        "the gateway does not execute package code automatically",
    ]


def test_code_package_hash_includes_referenced_code_files(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    shutil.copytree(CODE_PACKAGE, package_dir)
    before = validate_extension_package_source(
        {"type": "local_directory", "path": str(package_dir)}
    )

    (package_dir / "code" / "demo_adapter.py").write_text(
        "\"\"\"Mutated fixture adapter code.\"\"\"\n\n"
        "class DemoAdapter:\n"
        "    def health(self):\n"
        "        return {'status': 'changed'}\n",
        encoding="utf-8",
    )
    after = validate_extension_package_source(
        {"type": "local_directory", "path": str(package_dir)}
    )

    assert before["ok"] is True
    assert after["ok"] is True
    assert before["package_hash"] != after["package_hash"]


def test_git_code_package_is_indexed_but_not_imported_as_registry_data(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "repo"
    shutil.copytree(CODE_PACKAGE, package_dir)
    commit = _commit_package_repo(package_dir)

    review = validate_extension_package_source(
        {"type": "git", "url": str(package_dir), "ref": commit},
        install_root=tmp_path / "installed",
    )
    records = imported_registry_records(review)
    index_record = package_index_record(
        review,
        source={"type": "git", "url": str(package_dir), "ref": commit},
    )

    assert review["ok"] is True
    assert review["resolved_source"]["type"] == "git"
    assert review["resolved_source"]["resolved_ref"] == commit
    assert records == {
        "services": {},
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }
    assert index_record["adapters"] == [
        "test-demo-http-adapter",
        "test-demo-sidecar-adapter",
    ]
    assert index_record["translators"] == [
        "test-demo-metadata-translator",
        "test-demo-protocol-translator",
    ]
    assert index_record["requires_code_trust"] is True
