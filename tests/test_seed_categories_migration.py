"""seed_categories migration: starter categories as data, idempotent, dry-run by default."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway import admin_store, category_enforcement
from gateway.category_presets import load_presets
from gateway.category_store import CategoryStore
from gateway.policy import PolicyRequest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrations/seed_categories.py"
SPEC = importlib.util.spec_from_file_location("seed_categories", SCRIPT)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)

OWNER = "usr_owner"
WORKFLOW = "image.generate.not-ready"


@pytest.fixture
def data_dir(tmp_path):
    admin = tmp_path / "admin.db"
    admin_store.init_db(admin)
    with sqlite3.connect(admin) as conn:
        conn.execute("INSERT INTO users (id, issuer, subject, created_at) VALUES (?, 'i', 's', 1)", (OWNER,))
        conn.execute("INSERT INTO admin_role_assignments (role, subject_kind, subject_id, granted_at) "
                     "VALUES ('owner', 'user', ?, 1)", (OWNER,))
    return tmp_path


def run(data_dir, *extra, apply=True):
    args = ["--data-dir", str(data_dir), "--unchecked", *extra]
    return migration.main(args + (["--apply"] if apply else []))


def active(data_dir):
    return CategoryStore(data_dir / "categories.db").get_active()


def test_creates_every_bundled_preset_as_the_page_would_and_files_the_workflow(data_dir):
    assert run(data_dir, "--put", f"experimental={WORKFLOW}") == 0
    body = active(data_dir).body
    presets = load_presets()
    assert sorted(body["categories"]) == sorted(preset.id for preset in presets)
    for preset in presets:
        assert body["categories"][preset.id] == preset.category_row()
    service = body["services"][WORKFLOW + "@r1"]
    assertion = service["members"][0]["assertions"][0]
    assert (assertion["category"], assertion["accepted"], assertion["accepting_owner"]) == (
        "experimental", True, OWNER)


def test_the_migrated_data_hides_the_item_from_everyone_but_the_owner(data_dir):
    from dataclasses import dataclass

    @dataclass
    class Who:
        person_id: str | None
        app_id: str | None = "app"

    run(data_dir, "--put", f"experimental={WORKFLOW}")
    category_enforcement.configure(data_dir / "categories.db", owner_id=lambda: OWNER)
    try:
        request = PolicyRequest(intent="image.generate", workflow=WORKFLOW)
        assert category_enforcement.visible(Who(OWNER), request)
        assert not category_enforcement.visible(Who("usr_family"), request)
        assert not category_enforcement.visible(Who(None), request)
    finally:
        category_enforcement.configure(None)


def test_a_second_run_changes_nothing(data_dir, capsys):
    run(data_dir, "--put", f"experimental={WORKFLOW}")
    first = active(data_dir)
    capsys.readouterr()
    assert run(data_dir, "--put", f"experimental={WORKFLOW}") == 0
    assert "nothing to change" in capsys.readouterr().out
    assert active(data_dir).revision == first.revision
    assert CategoryStore(data_dir / "categories.db").revisions() == [first.revision]


def test_an_owner_edit_survives_a_second_run(data_dir):
    run(data_dir)
    store = CategoryStore(data_dir / "categories.db")
    current = store.get_active()
    body = current.body
    body["categories"]["adult"]["visible_to"] = ["plan_a"]
    body["categories"]["experimental"]["label"] = "Labs"
    row = store.create_candidate(body, current.revision, OWNER, "owner edit")
    store.activate(row.revision, current.revision)
    run(data_dir)
    after = active(data_dir).body["categories"]
    assert after["adult"]["visible_to"] == ["plan_a"]
    assert after["experimental"]["label"] == "Labs"


def test_only_the_named_presets_are_added(data_dir):
    run(data_dir, "--preset", "adult")
    assert sorted(active(data_dir).body["categories"]) == ["adult"]


def test_an_unknown_preset_is_refused(data_dir, capsys):
    assert run(data_dir, "--preset", "no-such-preset") == 2
    assert "no bundled preset" in capsys.readouterr().err
    assert not (data_dir / "categories.db").exists()


def test_dry_run_writes_nothing(data_dir, capsys):
    assert run(data_dir, "--put", f"experimental={WORKFLOW}", apply=False) == 0
    assert "dry run" in capsys.readouterr().out
    assert not (data_dir / "categories.db").exists()


def test_other_rules_in_the_snapshot_are_left_alone(data_dir):
    store = CategoryStore(data_dir / "categories.db")
    plan = {"id": "kids", "grants": [], "require": [], "deny": [], "missing": {}, "mode": "v3", "legacy_tags": []}
    row = store.create_candidate(
        {"id": "owner-rules", "categories": {}, "people": {"mia": [plan]}, "apps": {}, "services": {},
         "completeness_gate": True, "retained": {}}, None, OWNER, "existing rules")
    store.activate(row.revision, None)
    run(data_dir)
    assert active(data_dir).body["people"] == {"mia": [plan]}


def test_an_unknown_workflow_is_refused_unless_unchecked(data_dir, capsys):
    assert migration.main(["--data-dir", str(data_dir), "--put", "experimental=image.generate.no-such-thing",
                           "--apply"]) == 2
    assert "not workflows in this catalog" in capsys.readouterr().err
    assert not (data_dir / "categories.db").exists()


def test_an_unknown_category_is_refused(data_dir):
    with pytest.raises(SystemExit, match="no category 'nope'"):
        run(data_dir, "--put", f"nope={WORKFLOW}")


def test_the_owner_lookup_matches_the_gateways(data_dir):
    assert migration._owner_id(data_dir / "admin.db") == admin_store.instance_owner_id(path=data_dir / "admin.db") == OWNER
