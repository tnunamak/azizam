"""Execution descriptors are validated, immutable registry data."""

import ast
import copy
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import workflow_openapi as subject  # noqa: E402

ROWS = ROOT / "registry/workflow_execution_routes.yml"


def record(intent, *, bindings=None):
    return SimpleNamespace(id=f"{intent}.demo", intent=intent, contract={
        "inputs": {}, "bindings": bindings or {}, "typical_seconds": 31,
    })


def execution(intent, **kwargs):
    (row,) = subject.workflow_catalog([record(intent, **kwargs)])
    assert row["short_id"] == "demo"
    return row["execution"]


def document():
    return yaml.safe_load(ROWS.read_text())


def load_document(tmp_path, data):
    path = tmp_path / "test-execution-routes.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return subject.load_execution_routes(path)


@pytest.mark.parametrize("intent,sync,submit,recommended", [
    ("image.generate", "/v1/images/generations", "/gateway/v1/media/jobs", "async"),
    ("image.edit", "/v1/images/edits", None, "sync"),
    ("audio.generate", "/v1/audio/generate", "/gateway/v1/media/jobs", "async"),
    ("mesh.generate", "/v1/3d/generations/glb", "/v1/3d/generations/glb", "async"),
    ("video.generate", None, "/v1/videos", "async"),
    ("video.edit", "/gateway/v1/workflows/demo/run", "/v1/videos", "async"),
    ("speech.synthesis", None, None, "sync"),
    ("future.unknown", None, None, "sync"),
])
def test_shipped_rows_keep_public_route_pins(intent, sync, submit, recommended):
    value = execution(intent)
    assert value["recommended"] == recommended
    assert (value["sync"]["path"] if value["sync"] else None) == sync
    assert (value["async"]["submit"]["path"] if value["async"] else None) == submit


@pytest.mark.parametrize("intent,kind,statuses,terminal,poll,content,cancel", [
    ("image.generate", "media_job", ["queued", "running", "succeeded", "failed", "canceled"],
     ["succeeded", "failed", "canceled"], "/gateway/v1/media/jobs/{job_id}",
     "/gateway/v1/media/jobs/{job_id}/content", ("POST", "/gateway/v1/media/jobs/{job_id}/cancel")),
    ("mesh.generate", "respond_async", ["queued", "running", "succeeded", "failed", "canceled"],
     ["succeeded", "failed", "canceled"], "/gateway/v1/media/jobs/{job_id}",
     "/gateway/v1/media/jobs/{job_id}/content", ("POST", "/gateway/v1/media/jobs/{job_id}/cancel")),
    ("video.edit", "video", ["queued", "in_progress", "completed", "failed"],
     ["completed", "failed"], "/v1/videos/{video_id}",
     "/v1/videos/{video_id}/content", ("DELETE", "/v1/videos/{video_id}")),
])
def test_lifecycle_words_are_literal(intent, kind, statuses, terminal, poll, content, cancel):
    value = execution(intent)["async"]
    assert value["kind"] == kind
    assert value["statuses"] == statuses
    assert value["terminal"] == terminal
    assert value["poll"] == {"method": "GET", "path": poll}
    assert value["content"]["method"] == "GET"
    assert value["content"]["path"] == content
    assert (value["cancel"]["method"], value["cancel"]["path"]) == cancel
    assert value["fields"] == {}
    if kind == "media_job":
        assert value["submit"]["body"] == {"intent": "image.generate", "request": "<the sync request body>"}
        assert value["submit"]["model"] == "demo"
    elif kind == "respond_async":
        assert value["submit"]["headers"] == {"Prefer": "respond-async"}
        assert "model" not in value["submit"]
        assert value["content"]["output"] == "model/gltf-binary (GLB bytes)"
        assert value["cancel"]["note"] == "the engine cannot stop mid-run: the job ends as canceled when the run returns"
        assert value["output"] == "202 with a media job (Location, Preference-Applied: respond-async)"
    else:
        assert value["submit"]["model"] == "video-demo"


def test_native_override_keeps_its_declared_alternate():
    value = execution("image.edit", bindings={"gateway": {"route": "/synthetic/demo"}})
    assert value["recommended"] == "sync"
    assert value["async"] is None
    assert value["sync"] == {
        "method": "POST", "path": "/synthetic/demo", "content_type": "multipart/form-data",
        "fields": {}, "output": "JSON (see the route's response schema)",
        "alternate": {"method": "POST", "path": "/v1/images/edits", "model": "demo",
                      "note": "options go in extensions['com.azizam.workflow']"},
    }


def test_new_intent_is_only_a_data_row(tmp_path, monkeypatch):
    data = document()
    data["rows"].append({
        "id": "new-demo", "intent": "new.demo",
        "sync": {"method": "POST", "path": "/v1/audio/generate", "content_type": "application/json",
                 "model_binding": "short_id", "mapping": "sync", "output": "new output"},
    })
    monkeypatch.setattr(subject, "_EXECUTION_ROUTES", load_document(tmp_path, data))
    assert execution("new.demo") == {
        "recommended": "sync",
        "sync": {"method": "POST", "path": "/v1/audio/generate", "content_type": "application/json",
                 "model": "demo", "fields": {}, "output": "new output"},
        "async": None,
    }


def test_exact_intent_wins_over_prefix(tmp_path, monkeypatch):
    data = document()
    data["rows"].append({
        "id": "new-prefix", "intent_prefix": "image.",
        "sync": {"method": "POST", "path": "/v1/audio/generate", "content_type": "application/json",
                 "model_binding": "none", "mapping": "sync"},
    })
    monkeypatch.setattr(subject, "_EXECUTION_ROUTES", load_document(tmp_path, data))
    assert execution("image.generate")["sync"]["path"] == "/v1/images/generations"
    assert execution("image.new")["sync"]["path"] == "/v1/audio/generate"


@pytest.mark.parametrize("fault,origin", [
    ("root-key", "<root>"),
    ("row-key", "generate-image"),
    ("descriptor-key", "generate-image"),
    ("lifecycle-key", "media-job"),
    ("endpoint-key", "media-job"),
    ("unknown-lifecycle", "generate-image"),
    ("unknown-binding", "generate-image"),
    ("duplicate-intent", "duplicate-image"),
    ("duplicate-prefix", "duplicate-video"),
    ("unknown-path", "generate-image"),
])
def test_invalid_catalog_names_file_and_row(tmp_path, fault, origin):
    data = document()
    row = data["rows"][0]
    if fault == "root-key":
        data["typo"] = True
    elif fault == "row-key":
        row["typo"] = True
    elif fault == "descriptor-key":
        row["sync"]["typo"] = True
    elif fault == "lifecycle-key":
        data["lifecycles"]["media-job"]["typo"] = True
    elif fault == "endpoint-key":
        data["lifecycles"]["media-job"]["poll"]["typo"] = True
    elif fault == "unknown-lifecycle":
        row["async"]["lifecycle"] = "missing"
    elif fault == "unknown-binding":
        row["sync"]["model_binding"] = "guess"
    elif fault == "duplicate-intent":
        duplicate = copy.deepcopy(row)
        duplicate["id"] = "duplicate-image"
        data["rows"].append(duplicate)
    elif fault == "duplicate-prefix":
        duplicate = copy.deepcopy(next(item for item in data["rows"] if item.get("intent_prefix") == "video."))
        duplicate["id"] = "duplicate-video"
        data["rows"].append(duplicate)
    else:
        row["sync"]["path"] = "/not-a-declared-route"
    with pytest.raises(ValueError) as caught:
        load_document(tmp_path, data)
    assert "test-execution-routes.yml" in str(caught.value)
    assert origin in str(caught.value)


def test_canonical_alias_is_accepted(tmp_path, monkeypatch):
    data = document()
    data["rows"][0]["sync"]["path"] = "/openai/v1/images/generations"
    monkeypatch.setattr(subject, "_EXECUTION_ROUTES", load_document(tmp_path, data))
    assert execution("image.generate")["sync"]["path"] == "/openai/v1/images/generations"


def test_discovery_only_opt_in_is_not_published(tmp_path, monkeypatch):
    data = document()
    data["rows"][0]["sync"].update(path="/preview/{short_id}", discovery_only=True)
    monkeypatch.setattr(subject, "_EXECUTION_ROUTES", load_document(tmp_path, data))
    value = execution("image.generate")["sync"]
    assert value["path"] == "/preview/demo"
    assert "discovery_only" not in value


def test_loaded_rows_are_deeply_read_only_and_results_are_fresh():
    rows = subject._EXECUTION_ROUTES
    with pytest.raises(TypeError):
        rows["exact"] = {}
    with pytest.raises(TypeError):
        rows["exact"]["image.generate"]["sync"]["path"] = "/changed"
    with pytest.raises(TypeError):
        rows["lifecycles"]["media-job"]["statuses"][0] = "changed"
    with pytest.raises(TypeError):
        rows["exact"]["mesh.generate"]["async"]["headers"]["Prefer"] = "changed"
    value = execution("image.generate")
    value["async"]["statuses"].append("changed")
    value["async"]["submit"]["body"]["intent"] = "changed"
    assert execution("image.generate")["async"]["statuses"] == ["queued", "running", "succeeded", "failed", "canceled"]
    assert execution("image.generate")["async"]["submit"]["body"]["intent"] == "image.generate"


def test_public_projection_does_not_reload_files(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("execution must use the import-time snapshot")
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert execution("image.generate")["sync"]["path"] == "/v1/images/generations"


def test_route_choice_has_no_intent_literals():
    for name in ("_execution", "_execution_row", "_execution_descriptor"):
        tree = ast.parse(inspect.getsource(getattr(subject, name)))
        words = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        assert not [word for word in words if word.startswith(("image.", "audio.", "mesh.", "video.", "speech."))]
