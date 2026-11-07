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
    # The mesh job's create runs in ``admitted``, nested in ``_run_job`` in ``register_mesh_routes``; each enclosing
    # function is a site to the scan.
    "register_mesh_routes": "the 3D engine route: its manifest names an engine service, no graph template to pin; engine offerings are pinned in S2.5",
    "_run_job": "the 3D engine route: its manifest names an engine service, no graph template to pin; engine offerings are pinned in S2.5",
    "admitted": "the 3D engine route: its manifest names an engine service, no graph template to pin; engine offerings are pinned in S2.5",
}
PINNED = {"_identity_submit_job", "_submit_audio_media_job", "_submit_video_media_job", "gateway_image_generations_media_job"}


def create_sites() -> dict[str, bool]:
    """function name -> whether its ``media_jobs.create`` call passes ``envelope``."""
    found: dict[str, bool] = {}
    for path in SCANNED:
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create"
                        and "media_jobs" in getattr(node.func.value, "id", getattr(node.func.value, "attr", ""))):
                    found[function.name] = found.get(function.name, False) or any(k.arg == "envelope" for k in node.keywords)
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
    assert envelope.service_id == proxy._GRAPH_BACKEND_ID and envelope.interface_version == 1
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
    record = type("Record", (), {"source_path": str(package / "manifest.yml")})()
    registry = type("Registry", (), {"get": staticmethod(lambda wid: record if wid == "pkg.wf" else None)})()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
    manifest = {"id": "pkg.wf", "inputs": {}, "outputs": {},
                "bindings": {proxy.DERIVED._GRAPH_BINDING: {"workflow": "package://graphs/pkg-only.json"}}}
    assert proxy._resolve_workflow_artifact_path(record, "package://graphs/pkg-only.json").exists(), "the run finds it"
    built = proxy._manifest_job_envelope(manifest, proxy._GRAPH_CAPABILITY)
    assert built is not None and built.envelope.template is not None


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
