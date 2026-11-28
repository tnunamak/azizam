from __future__ import annotations

import runpy
import shutil
import subprocess
from pathlib import Path
from runpy import run_path

from gateway.extension_packages import (
    imported_registry_records,
    package_index_record,
    validate_extension_package_source,
)


ROOT = Path(__file__).resolve().parent.parent
CODE_PACKAGE = ROOT / "extensions" / "bundled" / "demo-code-package"
IMAGE_PACKAGE = ROOT / "extensions" / "bundled" / "demo-image-package"


def test_bundled_package_manifest_names_distinguish_the_two_demo_packages() -> None:
    import yaml

    names = {}
    for package in (CODE_PACKAGE, IMAGE_PACKAGE):
        manifest = yaml.safe_load((package / "extension.yml").read_text(encoding="utf-8"))
        names[manifest["id"]] = manifest["name"]
    assert names == {
        "test-demo-code-extension-package": "Test Demo Code Extension Package",
        "test-demo-image-extension-package": "Test Demo Image Extension Package",
    }


def test_bundled_demo_image_describes_a_consistent_text_to_image_workflow() -> None:
    import yaml

    def metadata(relative_path: str):
        return yaml.safe_load((IMAGE_PACKAGE / relative_path).read_text(encoding="utf-8"))

    manifest = metadata("extension.yml")
    workflow_path, = manifest["objects"]["workflows"]
    capability_path, = manifest["objects"]["capabilities"]
    workflow = metadata(workflow_path)
    capability = metadata(capability_path)
    assert workflow["display_name"] == "Test Demo Package Image"
    assert workflow["id"] == capability["workflow"]
    assert workflow["intent"] == capability["intent"] == "image.generate"
    assert capability["input_modalities"] == ["text"]
    assert workflow["inputs"]["prompt"]["required"] is True
    assert capability["output_modalities"] == ["image"]
    assert workflow["outputs"]["image"]["media_type"] == "image/png"



def _bundled_image_capability() -> dict:
    from yaml import safe_load

    package = ROOT / "extensions" / "bundled" / "demo-image-package"
    return safe_load((package / "objects" / "capability.yml").read_text(encoding="utf-8"))


def test_bundled_image_capability_declares_a_non_streaming_text_to_image_job() -> None:
    capability = _bundled_image_capability()

    assert capability["input_modalities"] == ["text"]
    assert capability["output_modalities"] == ["image"]
    assert capability["execution"] == "job"
    assert capability["streaming"] is False


def test_bundled_image_capability_reports_its_sizing_hints() -> None:
    capability = _bundled_image_capability()
    resources = capability["resources"]

    assert capability["resource_tier"] == "batch"
    assert resources["gpu_required"] is True
    assert resources["vram_gb_estimate"] == 4
    assert resources["duration_class"] == "short"
    assert resources["max_parallel"] == 1


def test_demo_adapter_health_returns_ready_status() -> None:
    adapter = runpy.run_path(str(CODE_PACKAGE / "code" / "demo_adapter.py"))["DemoAdapter"]

    assert adapter().health() == {"status": "ok"}


def test_demo_translator_supplies_defaults_and_preserves_messages() -> None:
    translate = runpy.run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["request_to_canonical"]
    messages = [{"role": "user", "content": "fixture message"}]

    assert translate({}) == {"model": "default", "messages": []}
    assert translate({"model": "fixture-model", "messages": messages}) == {
        "model": "fixture-model",
        "messages": messages,
    }


def test_demo_translator_defaults_only_an_omitted_model_not_explicit_null() -> None:
    translate = runpy.run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["request_to_canonical"]

    assert translate({}) == {"model": "default", "messages": []}
    assert translate({"model": None}) == {"model": None, "messages": []}


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


def test_demo_translator_fills_defaults_without_sharing_message_lists() -> None:
    from runpy import run_path

    translate = run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["request_to_canonical"]
    first = translate({})
    assert first == {"model": "default", "messages": []}
    first["messages"].append({"role": "user", "content": "fixture"})
    assert translate({}) == {"model": "default", "messages": []}


def test_demo_translator_wraps_response_without_overwriting_nested_fields() -> None:
    from runpy import run_path

    translate = run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["response_from_canonical"]
    body = {"response": "fixture text", "count": 2}
    assert translate(body) == {"response": {"response": "fixture text", "count": 2}}
    assert body == {"response": "fixture text", "count": 2}


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


def _load_demo_translator():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "bundled_demo_translator_fixture", CODE_PACKAGE / "code" / "demo_translator.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundled_demo_translator_routes_missing_request_fields_to_defaults() -> None:
    translator = _load_demo_translator()

    assert translator.request_to_canonical({}) == {"model": "default", "messages": []}
    assert translator.request_to_canonical({"model": "fixture-model", "messages": []}) == {
        "model": "fixture-model",
        "messages": [],
    }


def test_bundled_demo_translator_preserves_nested_response() -> None:
    translator = _load_demo_translator()
    response = {"id": "fixture-result", "choices": [{"index": 0}], "usage": {"total": 0}}

    assert translator.response_from_canonical(response) == {"response": response}
    assert response == {"id": "fixture-result", "choices": [{"index": 0}], "usage": {"total": 0}}


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


def test_code_package_review_and_index_never_execute_referenced_code(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    shutil.copytree(CODE_PACKAGE, package_dir)
    markers = []
    for filename in ("demo_adapter.py", "demo_translator.py"):
        marker = tmp_path / (filename + ".executed")
        markers.append(marker)
        code = package_dir / "code" / filename
        original = code.read_text(encoding="utf-8")
        code.write_text(
            original
            + "\nfrom pathlib import Path as _SentinelPath\n"
            + f"_SentinelPath({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )

    source = {"type": "local_directory", "path": str(package_dir)}
    review = validate_extension_package_source(source)
    assert review["ok"] is True, review["errors"]
    assert review["requires_code_trust"] is True
    index = package_index_record(review, source=source)
    records = imported_registry_records(review)

    assert index["requires_code_trust"] is True
    assert records == {
        "services": {},
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }
    assert all(not marker.exists() for marker in markers)


def test_demo_translator_defaults_do_not_share_a_messages_list() -> None:
    request = run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["request_to_canonical"]
    first = request({})
    assert first == {"model": "default", "messages": []}
    first["messages"].append({"role": "user", "content": "synthetic"})
    assert request({}) == {"model": "default", "messages": []}


def test_demo_translator_wraps_the_canonical_response_without_flattening() -> None:
    respond = run_path(str(CODE_PACKAGE / "code" / "demo_translator.py"))["response_from_canonical"]
    body = {"response": "synthetic", "choices": [], "complete": False}
    assert respond(body) == {"response": {"response": "synthetic", "choices": [], "complete": False}}
    assert body == {"response": "synthetic", "choices": [], "complete": False}


def test_bundled_image_capability_ids_and_intent_match_declared_objects() -> None:
    from yaml import safe_load

    package = ROOT / "extensions" / "bundled" / "demo-image-package"
    objects = package / "objects"
    capability = safe_load((objects / "capability.yml").read_text(encoding="utf-8"))
    service = safe_load((objects / "service.yml").read_text(encoding="utf-8"))
    workflow = safe_load((objects / "workflow.yml").read_text(encoding="utf-8"))
    frontend = safe_load((objects / "protocol_frontend.yml").read_text(encoding="utf-8"))

    assert capability["backend"] == service["id"]
    assert capability["workflow"] == workflow["id"]
    assert capability["intent"] == workflow["intent"]
    assert capability["intent"] in service["capabilities"]
    assert capability["intent"] in frontend["intents"]


def test_bundled_image_manifest_lists_unique_existing_object_documents() -> None:
    from yaml import safe_load

    package = ROOT / "extensions" / "bundled" / "demo-image-package"
    manifest = safe_load((package / "extension.yml").read_text(encoding="utf-8"))
    expected = {
        "services": "service",
        "protocol_frontends": "protocol_frontend",
        "capabilities": "capability",
        "workflows": None,  # Workflow schema v2 has no kind discriminator.
    }

    assert manifest["objects"].keys() == expected.keys()
    paths = [path for group in manifest["objects"].values() for path in group]
    assert len(paths) == len(set(paths)) == len(expected)
    for group, kind in expected.items():
        (path,) = manifest["objects"][group]
        document = safe_load((package / path).read_text(encoding="utf-8"))
        assert document["id"]
        if kind is not None:
            assert document["kind"] == kind


def test_bundled_translator_interfaces_resolve_to_declared_source_symbols() -> None:
    import ast
    from yaml import safe_load

    declaration = safe_load((CODE_PACKAGE / "objects" / "translator.yml").read_text(encoding="utf-8"))
    assert set(declaration["interfaces"]) == {
        f"protocol.{name}" for name in declaration["entrypoints"]
    }

    for name, target in declaration["entrypoints"].items():
        module, symbol = target.split(":")
        assert symbol == name
        source_path = CODE_PACKAGE.joinpath(*module.split(".")).with_suffix(".py")
        source = ast.parse(source_path.read_text(encoding="utf-8"))
        assert symbol in {node.name for node in source.body if isinstance(node, ast.FunctionDef)}
