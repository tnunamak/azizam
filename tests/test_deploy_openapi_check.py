"""The deploy post-check reads the OpenAPI pin the gate wrote (scripts/deploy_openapi_check.py).

Documents come from real FastAPI apps, so a reworded docstring moves exactly what FastAPI's own generator moves.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi import FastAPI

from scripts import deploy_openapi_check as check

RELOAD = "/admin/api/config/reload post"


def _document(reload_doc: str = "Reload the configuration.", models_doc: str = "List models.",
              extra_path: bool = False) -> dict:
    app = FastAPI()

    @app.post("/admin/api/config/reload", description=reload_doc)
    def reload() -> dict:
        return {}

    @app.get("/v1/models", description=models_doc)
    def models(available: bool = False) -> dict:
        return {}

    if extra_path:
        @app.get("/v1/extra")
        def extra() -> dict:
            return {}

    return app.openapi()


def _hashes(document: dict) -> dict[str, str]:
    return check.entry_hashes(document)


def _deploy(live_before: dict, live_after: dict, pin_before: dict, pin_after: dict):
    return check.compare(_hashes(live_before), _hashes(live_after), _hashes(pin_before), _hashes(pin_after))


def test_a_reworded_docstring_that_the_gate_re_pinned_deploys_without_a_rollback():
    old, new = _document(), _document(reload_doc="Reload the configuration from the admin store.")
    failures, notes = _deploy(old, new, old, new)
    assert failures == []
    assert [line.split(":")[0] for line in notes] == ["pin moved " + RELOAD]


def test_an_openapi_change_the_pin_does_not_record_rolls_back():
    old, new = _document(), _document(reload_doc="Reload the configuration from the admin store.")
    failures, _notes = _deploy(old, new, old, old)
    assert [line.split(":")[0] for line in failures] == [RELOAD]


def test_a_live_document_that_differs_from_the_new_pin_rolls_back():
    old = _document()
    pinned = _document(reload_doc="Reload the configuration from the admin store.")
    live_after = _document(reload_doc="Reload the configuration from the admin store.", models_doc="Models.")
    failures, _notes = _deploy(old, live_after, old, pinned)
    assert [line.split(":")[0] for line in failures] == ["/v1/models get"]


def test_operations_the_pin_adds_or_removes_must_appear_or_leave_live():
    old, new = _document(), _document(extra_path=True)
    assert _deploy(old, new, old, new)[0] == []
    assert [line.split(":")[0] for line in _deploy(old, old, old, new)[0]] == ["/v1/extra get"]
    assert _deploy(new, old, new, old)[0] == []
    assert [line.split(":")[0] for line in _deploy(new, new, new, old)[0]] == ["/v1/extra get"]


def test_an_entry_live_data_shapes_must_hold_still_unless_the_pin_moves_it():
    pin = _document()
    live = copy.deepcopy(pin)
    live["components"] = {"schemas": {"OwnerWorkflow": {"type": "object"}}}  # an owner's workflow, not in the pin
    assert _deploy(live, live, pin, pin)[0] == []
    moved = copy.deepcopy(live)
    moved["components"]["schemas"]["OwnerWorkflow"]["title"] = "Changed"
    assert [line.split(":")[0] for line in _deploy(live, moved, pin, pin)[0]] == ["components"]
    repinned = copy.deepcopy(pin)
    repinned["components"] = {"schemas": {"NewPinned": {"type": "string"}}}
    assert _deploy(live, moved, pin, repinned)[0] == []


def test_the_cli_reads_the_deploy_kit_snapshot_and_exits_nonzero_on_an_unpinned_move(tmp_path: Path, capsys):
    old, new = _document(), _document(reload_doc="Reload the configuration from the admin store.")

    def snapshot(document: dict) -> Path:
        path = tmp_path / f"snap-{len(list(tmp_path.iterdir()))}.json"
        rows = sorted(f"{key} {value}" for key, value in check.entry_hashes(document).items())
        path.write_text(json.dumps({"openapi_code": 200, "openapi": rows}))
        return path

    def document(value: dict) -> Path:
        path = tmp_path / f"pin-{len(list(tmp_path.iterdir()))}.json"
        path.write_text(json.dumps(value))
        return path

    pre, post = snapshot(old), snapshot(new)
    assert check.main(["--pre", str(pre), "--post", str(post), "--pins", str(document(old)), str(document(new))]) == 0
    assert "OPENAPI PIN PASS" in capsys.readouterr().out
    assert check.main(["--pre", str(pre), "--post", str(post), "--pins", str(document(old)), str(document(old))]) == 1
    assert "FAIL " + RELOAD in capsys.readouterr().out
    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps({"openapi_code": 502, "openapi": []}))
    assert check.main(["--pre", str(pre), "--post", str(failed), "--pins", str(document(old)), str(document(new))]) == 1
    assert "cannot read the inputs" in capsys.readouterr().out
