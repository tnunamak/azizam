"""Where jobs are created, and which of them record an envelope (S2.1b).

``test_every_job_create_site_is_accounted_for`` finds every ``media_jobs.create`` call in the gateway by reading the
code. A new call site fails the test until someone decides whether it pins definitions or why it does not.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

import proxy
from tests.support.graph_service import GRAPH_SERVICE_ID
from gateway import media_jobs
from gateway.definition_store import DefinitionStore

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]

# function -> why it records no envelope. A site that does record is not listed.
UNPINNED = {
    "_protocol_image_result": "an engine offering, not a manifest workflow: no contract or template to pin until S2.5",
    "store_creator": "the raw media-jobs API: an outside adapter reports the work, the gateway runs no workflow",
    "_submit": "the Make route picks its workflow at dispatch, after the job row exists",
    "_create_indexed": "indexes a response that is already finished; nothing runs",
}
PINNED = {"_create", "_identity_submit_job", "_submit_audio_media_job", "_submit_video_media_job", "gateway_image_generations_media_job"}


def create_sites() -> dict[str, bool]:
    """function name -> whether its ``media_jobs.create`` call passes ``envelope`` (or ``prepare``, which builds it
    inside the job's own transaction: the generic run core)."""
    found: dict[str, bool] = {}
    for path in SCANNED:
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create"
                        and "media_jobs" in getattr(node.func.value, "id", getattr(node.func.value, "attr", ""))):
                    found[function.name] = found.get(function.name, False) or any(k.arg in ("envelope", "prepare") for k in node.keywords)
    return found


def test_every_job_create_site_is_accounted_for():
    sites = create_sites()
    recording = {name for name, passes in sites.items() if passes}
    assert recording == PINNED, f"sites that pass an envelope: {sorted(recording)}"
    # The enclosing function of a nested helper can repeat a name (``store_creator`` sits inside the builder).
    assert {name for name, passes in sites.items() if not passes} - {"_build_media_job_providers"} == set(UNPINNED)


def test_no_production_code_inserts_a_job_row_another_way():
    """``upsert`` writes a job row without an envelope; only tests and seed scripts may call it."""
    callers = []
    for path in SCANNED:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "upsert"
                    and "media_jobs" in getattr(node.func.value, "id", getattr(node.func.value, "attr", ""))):
                callers.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert callers == []


def pinned(job_id: str | None, db: Path):
    return DefinitionStore(db).envelope(job_id) if job_id else None


def test_a_manifest_workflow_pins_its_contract_binding_and_graph():
    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get("audio.generate.tangoflux")
    built = proxy._manifest_job_envelope(record.manifest, proxy._AUDIO_GRAPH_CAPABILITY)
    assert built is not None
    envelope = built.envelope
    assert envelope.source_kind == "workflow" and envelope.source_id == "audio.generate.tangoflux"
    assert envelope.template is not None and len(built.blobs) == 3
    template = json.loads(built.blobs[2])
    assert template == json.loads((ROOT / "workflows/audio/tangoflux.json").read_text())
    binding = json.loads(built.blobs[1])
    assert binding["map"]["prompt"] and binding["workflow"] == "workflows/audio/tangoflux.json"
    assert envelope.service_id == GRAPH_SERVICE_ID and envelope.interface_version == 2
    assert envelope.adapter_id and envelope.protocol_id
    assert envelope.generated == {} and envelope.bundle is None


def test_a_manifest_without_the_graph_binding_pins_nothing():
    assert proxy._manifest_job_envelope({"id": "x", "inputs": {}, "bindings": {}}, proxy._GRAPH_CAPABILITY) is None
    assert proxy._manifest_job_envelope(None, proxy._GRAPH_CAPABILITY) is None


def test_a_manifest_whose_bindings_are_not_a_mapping_pins_nothing_and_does_not_raise():
    assert proxy._manifest_job_envelope({"id": "x", "inputs": {}, "bindings": ["not", "a", "mapping"]}, proxy._GRAPH_CAPABILITY) is None


def test_a_graph_relative_to_its_package_pins_as_the_run_resolves_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package = tmp_path / "pkg"
    (package / "graphs").mkdir(parents=True)
    (package / "graphs" / "pkg-only.json").write_bytes(b'{"1": {"class_type": "X", "inputs": {}}}')
    (package / "manifest.yml").write_text("id: pkg.wf", encoding="utf-8")
    record = type("Record", (), {"source_path": str(package / "manifest.yml")})()
    registry = type("Registry", (), {"get": staticmethod(lambda wid: record if wid == "pkg.wf" else None)})()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
    manifest = {"id": "pkg.wf", "inputs": {}, "outputs": {},
                "bindings": {proxy.DERIVED._GRAPH_BINDING: {"workflow": "package://graphs/pkg-only.json"}}}
    assert proxy._resolve_workflow_artifact_path(record, "package://graphs/pkg-only.json").exists(), "the run finds it"
    built = proxy._manifest_job_envelope(manifest, proxy._GRAPH_CAPABILITY)
    assert built is not None and built.envelope.template is not None


@pytest.mark.parametrize("source_kind", ["directory", "manifest"])
def test_package_artifact_ignores_project_shadow_and_keeps_plain_resolution(
    tmp_path: Path, source_kind: str,
):
    package = tmp_path / "package"
    relative = "tests/test_job_envelope_sites.py"
    artifact = package / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("reviewed package artifact", encoding="utf-8")
    source = package
    if source_kind == "manifest":
        source = package / "manifest.yml"
        source.write_text("id: package.workflow", encoding="utf-8")
    record = type("Record", (), {"source_path": source})()

    # This same relative file really exists in the project, without writing there.
    assert (ROOT / relative).is_file()
    assert proxy._resolve_workflow_artifact_path(record, relative) == ROOT / relative
    assert proxy._resolve_workflow_artifact_path(record, f"package://{relative}") == artifact


@pytest.mark.parametrize("escape", ["parent", "symlink"])
def test_package_artifact_refuses_paths_outside_its_root(tmp_path: Path, escape: str):
    package = tmp_path / "package"
    package.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside the reviewed package", encoding="utf-8")
    relative = "../outside.json"
    if escape == "symlink":
        (package / "escape.json").symlink_to(outside)
        relative = "escape.json"
    record = type("Record", (), {"source_path": package})()

    with pytest.raises(ValueError, match="package root"):
        proxy._resolve_workflow_artifact_path(record, f"package://{relative}")


_PACKAGE_GRAPH = {"1": {"class_type": "PackageNode", "inputs": {}}}
_PROJECT_GRAPH = {"1": {"class_type": "ProjectNode", "inputs": {}}}


def _package_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str):
    """A package workflow whose graph names model ``shadowed``, and a built-in graph file of the same name."""
    package = tmp_path / "package"
    (package / "graphs").mkdir(parents=True)
    (package / "graphs" / "shadowed.json").write_text(json.dumps(_PACKAGE_GRAPH), encoding="utf-8")
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "shadowed.json").write_text(json.dumps(_PROJECT_GRAPH), encoding="utf-8")
    (tmp_path / "outside.json").write_text(json.dumps(_PROJECT_GRAPH), encoding="utf-8")
    manifest = {"id": "pkg.shadowed", "inputs": {}, "outputs": {},
                "bindings": {proxy.DERIVED._GRAPH_BINDING: {"workflow": artifact}}}
    record = type("Record", (), {"source_path": package, "manifest": manifest})()
    registry = type("Registry", (), {"get": staticmethod(lambda wid: record if wid == "pkg.shadowed" else None),
                                     "records": staticmethod(lambda: [record])})()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
    monkeypatch.setitem(proxy.runtime().derived, "COMFY_WORKFLOWS_DIR", builtin)
    monkeypatch.setattr(proxy, "validate_workflow_graph", lambda workflow: [])
    return manifest


def test_the_run_and_the_pin_both_use_the_package_graph_over_a_built_in_file_of_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    manifest = _package_model(tmp_path, monkeypatch, "package://graphs/shadowed.json")
    loaded = proxy.load_graph_workflow("shadowed")
    assert loaded is not None and loaded["1"]["class_type"] == "PackageNode"
    pinned: dict[str, bytes] = {}
    real_build = proxy.job_envelope.build_job_envelope

    def capture(**kwargs):
        pinned["template"] = kwargs["template"]
        return real_build(**kwargs)

    monkeypatch.setattr(proxy.job_envelope, "build_job_envelope", capture)
    proxy._manifest_job_envelope(manifest, proxy._GRAPH_CAPABILITY)
    assert json.loads(pinned["template"]) == _PACKAGE_GRAPH


def test_a_package_graph_the_resolver_refuses_makes_the_model_unavailable_without_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # The escaping path names model "shadowed" too, so a fallback to the built-in file would load it.
    _package_model(tmp_path, monkeypatch, "package://../builtin/shadowed.json")
    assert proxy.load_graph_workflow("shadowed") is None


def test_a_missing_package_folder_is_not_read_as_a_manifest_file(tmp_path: Path):
    (tmp_path / "packages" / "graphs").mkdir(parents=True)
    (tmp_path / "packages" / "graphs" / "demo.json").write_text("a sibling's bytes", encoding="utf-8")
    record = type("Record", (), {"source_path": tmp_path / "packages" / "missing-package"})()
    with pytest.raises(ValueError, match="no package root"):
        proxy._resolve_workflow_artifact_path(record, "package://graphs/demo.json")


def test_an_unreadable_graph_file_pins_nothing_and_does_not_raise(caplog: pytest.LogCaptureFixture):
    # A distinctive id: log_scrub replaces any registered secret value in a log line, and another test on the same
    # worker may have registered a short one such as "x".
    manifest = {"id": "graph-envelope-probe", "inputs": {}, "outputs": {}, "bindings": {proxy.DERIVED._GRAPH_BINDING: {"workflow": "workflows/none.json"}}}
    assert proxy._manifest_job_envelope(manifest, proxy._GRAPH_CAPABILITY) is None
    assert "no envelope for graph-envelope-probe" in caplog.text


def test_an_image_manifest_pins():
    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get("image.generate.flux2-klein-4b")
    built = proxy._manifest_job_envelope(record.manifest, proxy._GRAPH_CAPABILITY)
    assert built is not None and built.envelope.source_id == "image.generate.flux2-klein-4b"


def test_a_configured_video_workflow_pins_its_config_entry_and_graph():
    name = proxy._video_default_workflow()
    built = proxy._video_job_envelope(name)
    assert built is not None
    assert built.envelope.source_id == name and built.envelope.template is not None
    assert json.loads(built.blobs[0])["workflow"]["file"] == proxy._video_workflow_table()[name]["file"]
    assert proxy._video_job_envelope("no-such-video-workflow") is None


def test_the_identity_scene_pins_its_graph():
    built = proxy._identity_job_envelope()
    assert built is not None
    assert built.envelope.source_id == proxy.IDENTITY_MANIFEST_ID and built.envelope.template is not None


def test_pinned_blobs_land_in_the_jobs_database(tmp_path: Path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    built = proxy._manifest_job_envelope(
        proxy.GATEWAY_WORKFLOW_REGISTRY.get("audio.generate.tangoflux").manifest, proxy._AUDIO_GRAPH_CAPABILITY,
    )
    job = media_jobs.create(media_jobs.MediaJob(intent="audio.generate", request_id="r1"), envelope=built, path=db)
    assert pinned(job.job_id, db) == built.envelope
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM definition_blob").fetchone()[0] == 3


def _graph_package(root: Path, name: str, graph: dict) -> Path:
    package = root / name
    (package / "graphs").mkdir(parents=True)
    (package / "graphs" / "shadowed.json").write_text(json.dumps(graph), encoding="utf-8")
    return package


def _registry_of(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: list[tuple[str, str, Path, str]]):
    """A registry of (id, intent, package folder, artifact) records, and an empty built-in graph folder."""
    records = [
        type("Record", (), {"source_path": folder, "manifest": {
            "id": rid, "intent": intent, "inputs": {}, "outputs": {},
            "bindings": {proxy.DERIVED._GRAPH_BINDING: {"workflow": artifact}}}})()
        for rid, intent, folder, artifact in rows
    ]
    by_id = {record.manifest["id"]: record for record in records}
    registry = type("Registry", (), {"get": staticmethod(by_id.get), "records": staticmethod(lambda: records)})()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
    (tmp_path / "builtin").mkdir(exist_ok=True)
    monkeypatch.setitem(proxy.runtime().derived, "COMFY_WORKFLOWS_DIR", tmp_path / "builtin")
    monkeypatch.setattr(proxy, "validate_workflow_graph", lambda workflow: [])
    return by_id


def _pinned_graph(monkeypatch: pytest.MonkeyPatch, manifest: dict) -> dict:
    pinned: dict[str, bytes] = {}
    real_build = proxy.job_envelope.build_job_envelope

    def capture(**kwargs):
        pinned["template"] = kwargs["template"]
        return real_build(**kwargs)

    monkeypatch.setattr(proxy.job_envelope, "build_job_envelope", capture)
    proxy._manifest_job_envelope(manifest, proxy._GRAPH_CAPABILITY)
    return json.loads(pinned["template"])


def _node(name: str) -> dict:
    return {"1": {"class_type": name, "inputs": {}}}


@pytest.mark.parametrize("audio_id", ["audio.generate.unrelated", "audio.generate.shadowed"])
def test_a_record_of_another_intent_with_the_same_graph_name_never_supplies_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audio_id: str,
):
    audio = _graph_package(tmp_path, "audio-pkg", _node("AudioNode"))
    image = _graph_package(tmp_path, "image-pkg", _node("ImageNode"))
    # The audio record is registered first, so a first-basename-match loader would take it.
    by_id = _registry_of(monkeypatch, tmp_path, [
        (audio_id, "audio.generate", audio, "package://graphs/shadowed.json"),
        ("image.generate.shadowed", "image.generate", image, "package://graphs/shadowed.json"),
    ])
    loaded = proxy.load_graph_workflow("shadowed", intent="image.generate")
    assert loaded is not None and loaded["1"]["class_type"] == "ImageNode"
    manifest = proxy._find_image_manifest("image.generate", "shadowed")
    assert manifest is not None and manifest["id"] == "image.generate.shadowed"
    assert _pinned_graph(monkeypatch, by_id["image.generate.shadowed"].manifest) == _node("ImageNode")


def test_two_packages_with_colliding_graph_names_each_load_and_pin_their_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    first = _graph_package(tmp_path, "first", _node("FirstNode"))
    second = _graph_package(tmp_path, "second", _node("SecondNode"))
    by_id = _registry_of(monkeypatch, tmp_path, [
        ("image.generate.alpha", "image.generate", first, "package://graphs/shadowed.json"),
        ("image.generate.shadowed", "image.generate", second, "package://graphs/shadowed.json"),
    ])
    loaded = proxy.load_graph_workflow("shadowed", intent="image.generate")
    assert loaded is not None and loaded["1"]["class_type"] == "SecondNode"
    assert _pinned_graph(monkeypatch, by_id["image.generate.shadowed"].manifest) == _node("SecondNode")


def test_a_refused_package_model_is_refused_not_absent_and_never_becomes_its_edit_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    package = _graph_package(tmp_path, "pkg", _node("PackageNode"))
    (tmp_path / "outside.json").write_text(json.dumps(_node("OutsideNode")), encoding="utf-8")
    _registry_of(monkeypatch, tmp_path, [
        ("image.edit.shadowed", "image.edit", package, "package://../outside.json"),
    ])
    (tmp_path / "builtin" / "shadowed-edit.json").write_text(json.dumps(_node("BuiltInEdit")), encoding="utf-8")
    assert proxy.load_graph_workflow("shadowed", intent="image.edit") is None
    assert proxy.graph_workflow_refused("shadowed", intent="image.edit")
    assert not proxy.graph_workflow_refused("no-such-model", intent="image.edit")
    assert proxy._select_edit_workflow("shadowed", image_count=1, has_mask=False) == "shadowed"
    assert proxy.load_graph_workflow("shadowed", intent="image.edit") is None


def test_edit_selection_reads_the_edit_record_not_another_intents_record_of_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    audio = _graph_package(tmp_path, "audio", _node("AudioNode"))
    refused = _graph_package(tmp_path, "pkg", _node("PackageNode"))
    (tmp_path / "outside.json").write_text(json.dumps(_node("OutsideNode")), encoding="utf-8")
    _registry_of(monkeypatch, tmp_path, [
        ("audio.generate.shadowed", "audio.generate", audio, "package://graphs/shadowed.json"),
        ("image.edit.shadowed-edit", "image.edit", tmp_path, "shadowed-edit.json"),
        ("image.edit.shadowed", "image.edit", refused, "package://../outside.json"),
    ])
    (tmp_path / "builtin" / "shadowed-edit.json").write_text(json.dumps(_node("BuiltInEdit")), encoding="utf-8")
    assert proxy._select_edit_workflow("shadowed", image_count=1, has_mask=False) == "shadowed"
    assert proxy.load_graph_workflow("shadowed", intent="image.edit") is None


def test_a_package_graph_that_reaches_a_symlink_loop_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package = _graph_package(tmp_path, "pkg", _node("PackageNode"))
    (package / "loop").symlink_to(package / "loop")
    _registry_of(monkeypatch, tmp_path, [
        ("image.generate.shadowed", "image.generate", package, "package://loop/shadowed.json"),
    ])
    (tmp_path / "builtin" / "shadowed.json").write_text(json.dumps(_node("BuiltIn")), encoding="utf-8")
    assert proxy.load_graph_workflow("shadowed", intent="image.generate") is None
    assert proxy.graph_workflow_refused("shadowed", intent="image.generate")


def test_derived_values_cannot_be_assigned_so_a_patch_never_outlives_its_test(monkeypatch: pytest.MonkeyPatch):
    """An assigned name would shadow the snapshot for every later test on the worker (it once hid a patched
    ``COMFY_WORKFLOWS_DIR`` from an unrelated test). A patch goes on the snapshot's own mapping instead."""
    original = proxy.DERIVED.COMFY_WORKFLOWS_DIR
    assert vars(proxy.DERIVED) == {}
    # A plain assignment, so the check does not depend on when ``monkeypatch`` records its undo entry.
    with pytest.raises(AttributeError, match="read-only"):
        proxy.DERIVED.COMFY_WORKFLOWS_DIR = Path("/nowhere")
    assert vars(proxy.DERIVED) == {}
    with monkeypatch.context() as scoped:
        scoped.setitem(proxy.runtime().derived, "COMFY_WORKFLOWS_DIR", Path("/patched"))
        assert proxy.DERIVED.COMFY_WORKFLOWS_DIR == Path("/patched")
    assert proxy.DERIVED.COMFY_WORKFLOWS_DIR == original
    assert vars(proxy.DERIVED) == {}
