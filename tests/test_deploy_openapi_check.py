"""The deploy post-check reads the OpenAPI pin the gate wrote (scripts/deploy_openapi_check.py).

Documents come from real FastAPI apps, so a reworded docstring moves exactly what FastAPI's own generator moves.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

from fastapi import FastAPI
import pytest

from scripts import deploy_openapi_check as check

RELOAD = "/admin/api/config/reload post"
REWORDED = "Reload the configuration from the admin store."


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


def _deploy(live_before: dict, live_after: dict, pin_before: dict, pin_after: dict):
    return check.compare(*(check.entries(document) for document in (live_before, live_after, pin_before, pin_after)))


def _names(lines: list[str]) -> list[str]:
    return [line.split(":")[0] for line in lines]


def test_a_reworded_docstring_that_the_gate_re_pinned_deploys_without_a_rollback():
    old, new = _document(), _document(reload_doc=REWORDED)
    failures, notes = _deploy(old, new, old, new)
    assert failures == []
    assert _names(notes) == ["pin moved " + RELOAD]


def test_an_openapi_change_the_pin_does_not_record_rolls_back():
    old, new = _document(), _document(reload_doc=REWORDED)
    assert _names(_deploy(old, new, old, old)[0]) == [RELOAD]


def test_a_live_document_that_differs_from_the_new_pin_rolls_back():
    old, pinned = _document(), _document(reload_doc=REWORDED)
    live_after = _document(reload_doc=REWORDED, models_doc="Models.")
    assert _names(_deploy(old, live_after, old, pinned)[0]) == ["/v1/models get"]


def test_operations_the_pin_adds_or_removes_must_appear_or_leave_live():
    old, new = _document(), _document(extra_path=True)
    assert _deploy(old, new, old, new)[0] == []
    assert _names(_deploy(old, old, old, new)[0]) == ["/v1/extra get"]
    assert _deploy(new, old, new, old)[0] == []
    assert _names(_deploy(new, new, new, old)[0]) == ["/v1/extra get"]


def test_an_operation_that_appears_only_after_the_deploy_rolls_back():
    old, new = _document(), _document(extra_path=True)
    assert _names(_deploy(old, new, old, old)[0]) == ["/v1/extra get"]


def test_a_top_level_change_the_pin_does_not_record_rolls_back():
    old = _document()
    live_after = copy.deepcopy(old)
    live_after["security"] = [{"bearer": []}]
    assert _names(_deploy(old, live_after, old, old)[0]) == ["document"]
    live_after = copy.deepcopy(old)
    live_after["info"]["version"] = "2"
    assert _names(_deploy(old, live_after, old, old)[0]) == ["document"]


def test_components_are_judged_one_by_one():
    pin = _document()
    live = copy.deepcopy(pin)
    live["components"] = {"schemas": {"OwnerWorkflow": {"type": "object"}}}  # live data the pin lacks
    moved_pin = copy.deepcopy(pin)
    moved_pin.setdefault("components", {}).setdefault("schemas", {})["NewPinned"] = {"type": "string"}
    live_after = copy.deepcopy(live)
    live_after["components"]["schemas"]["NewPinned"] = {"type": "string"}
    assert _deploy(live, live_after, pin, moved_pin)[0] == []
    live_after["components"]["schemas"]["OwnerWorkflow"]["title"] = "Changed"
    assert _names(_deploy(live, live_after, pin, moved_pin)[0]) == ["components schemas/OwnerWorkflow"]


def test_an_entry_live_data_shapes_holds_still_and_must_reach_the_new_pin_when_the_pin_moves_it():
    pin = _document()
    live = _document(models_doc="Models, with an owner's addition.")  # live differs from the pin before the deploy
    assert _deploy(live, live, pin, pin)[0] == []
    assert _names(_deploy(live, pin, pin, pin)[0]) == ["/v1/models get"]
    repinned = _document(models_doc="List the models.")
    assert _names(_deploy(live, live, pin, repinned)[0]) == ["/v1/models get"]
    assert _names(_deploy(live, _document(models_doc="Something else."), pin, repinned)[0]) == ["/v1/models get"]
    without = copy.deepcopy(live)
    del without["paths"]["/v1/models"]
    assert _names(_deploy(live, without, pin, repinned)[0]) == ["/v1/models get"]
    assert _deploy(live, repinned, pin, repinned)[0] == []


@pytest.mark.parametrize("document", [[], {}, {"openapi_code": 200, "openapi": ["/v1/x get 1111111111111111"]},
                                      {"paths": {"/v1/x": None}}, {"paths": {}, "components": {"schemas": []}}])
def test_anything_but_an_openapi_document_is_refused(document):
    with pytest.raises(ValueError):
        check.entries(document)


def _write(tmp_path: Path, name: str, value) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    return str(path)


def test_the_cli_reads_two_pins_and_exits_nonzero_on_an_unpinned_move(tmp_path: Path, capsys):
    old, new = _document(), _document(reload_doc=REWORDED)
    pre, post = _write(tmp_path, "pre.json", old), _write(tmp_path, "post.json", new)
    old_pin, new_pin = _write(tmp_path, "old-pin.json", old), _write(tmp_path, "new-pin.json", new)
    assert check.main(["--pre", pre, "--post", post, "--pins", old_pin, new_pin]) == 0
    assert "OPENAPI PIN PASS" in capsys.readouterr().out
    assert check.main(["--pre", pre, "--post", post, "--pins", old_pin, old_pin]) == 1
    assert "FAIL " + RELOAD in capsys.readouterr().out
    for broken in ("not json", {"openapi_code": 502}):
        assert check.main(["--pre", pre, "--post", _write(tmp_path, "broken.json", broken),
                           "--pins", old_pin, new_pin]) == 1
        assert "OPENAPI PIN FAIL: cannot read the inputs" in capsys.readouterr().out


def test_the_cli_reads_the_pin_at_each_commit(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    (repo / "tests" / "fixtures").mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                                   "PATH": "/usr/bin:/bin"}).stdout.strip()

    old, new = _document(), _document(reload_doc=REWORDED)
    git("init", "-q")
    commits = []
    for document in (old, new):
        (repo / check.PIN).write_text(json.dumps(document))
        git("add", "-A")
        git("commit", "-q", "-m", "pin")
        commits.append(git("rev-parse", "HEAD"))
    pre, post = _write(tmp_path, "pre.json", old), _write(tmp_path, "post.json", new)
    assert check.main(["--pre", pre, "--post", post, "--commits", *commits, "--repo", str(repo)]) == 0
    assert "OPENAPI PIN PASS" in capsys.readouterr().out
    assert check.main(["--pre", pre, "--post", post, "--commits", commits[0], commits[0], "--repo", str(repo)]) == 1
    assert check.main(["--pre", pre, "--post", post, "--commits", "0" * 40, commits[1], "--repo", str(repo)]) == 1
    assert "cannot read the inputs" in capsys.readouterr().out
