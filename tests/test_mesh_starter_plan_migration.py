"""The starter plan gains 3D models once; a plan an owner made or changed never does.

A database from before the mesh capability has a starter plan that grants every capability it knew. Opening it
again must give that plan ``mesh`` and nothing else. The step is recorded, so a later edit is never undone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gateway import admin_store
from gateway.access_plans import CAPABILITIES
from tests.support.access import plan_document

OLD_CAPABILITIES = tuple(capability for capability in CAPABILITIES if capability != "mesh")


def _stored(path: Path, plan_id: str) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        (raw,) = conn.execute("SELECT plan_json FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(raw)


def _forget_mesh_step(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM schema_migration_steps WHERE name = ?", (admin_store.MESH_STARTER_STEP,))
        conn.commit()
    finally:
        conn.close()


def _store_before_mesh(path: Path, name: str, **plan_kwargs) -> str:
    """A plan as a database from before mesh stored it: the old capability set, no mesh key."""
    document = plan_document(name, caps=OLD_CAPABILITIES, **plan_kwargs)
    assert "mesh" not in document["can_use"]
    return admin_store.create_access_plan(document, path=path)["id"]


def test_the_starter_plan_that_grants_everything_else_gains_3d_models_once(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    starter = _store_before_mesh(path, admin_store.STARTER_PLAN_NAME)
    _forget_mesh_step(path)

    admin_store.init_db(path)

    assert _stored(path, starter)["can_use"]["mesh"] == {"items": "all"}
    assert set(_stored(path, starter)["can_use"]) == set(CAPABILITIES)


def test_a_custom_plan_that_allows_images_does_not_gain_3d_models(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    images = admin_store.create_access_plan(plan_document("Images only", caps=("image",)), path=path)["id"]
    everything_custom = _store_before_mesh(path, "Everything for family")
    _forget_mesh_step(path)

    admin_store.init_db(path)

    assert "mesh" not in _stored(path, images)["can_use"]
    assert "mesh" not in _stored(path, everything_custom)["can_use"]


def test_a_starter_plan_the_owner_narrowed_does_not_gain_3d_models(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    narrowed = admin_store.create_access_plan(
        plan_document(admin_store.STARTER_PLAN_NAME, caps=tuple(c for c in OLD_CAPABILITIES if c != "music")),
        path=path,
    )["id"]
    _forget_mesh_step(path)

    admin_store.init_db(path)

    assert "mesh" not in _stored(path, narrowed)["can_use"]


def test_the_full_old_starter_with_one_capability_picked_does_not_gain_3d_models(tmp_path):
    """Same name and every old capability, but one of them is a picked list: not the starter, so no mesh."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    document = plan_document(admin_store.STARTER_PLAN_NAME, caps=OLD_CAPABILITIES)
    document["can_use"]["image"] = {"items": ["krea2-turbo"]}
    picked = admin_store.create_access_plan(document, path=path)["id"]
    _forget_mesh_step(path)

    admin_store.init_db(path)
    assert "mesh" not in _stored(path, picked)["can_use"]

    admin_store.init_db(path)
    assert "mesh" not in _stored(path, picked)["can_use"]
    assert _stored(path, picked)["can_use"]["image"] == {"items": ["krea2-turbo"]}


def test_the_step_runs_once_so_a_later_edit_is_kept(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    starter = _store_before_mesh(path, admin_store.STARTER_PLAN_NAME)
    _forget_mesh_step(path)
    admin_store.init_db(path)
    conn = sqlite3.connect(str(path))
    try:
        (raw,) = conn.execute("SELECT plan_json FROM access_plans WHERE id = ?", (starter,)).fetchone()
        document = json.loads(raw)
        del document["can_use"]["mesh"]
        conn.execute("UPDATE access_plans SET plan_json = ? WHERE id = ?",
                     (json.dumps(document, sort_keys=True), starter))
        conn.commit()
    finally:
        conn.close()

    admin_store.init_db(path)

    assert "mesh" not in _stored(path, starter)["can_use"]


def test_a_fresh_database_seeds_its_starter_plan_with_3d_models(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    owner = admin_store.upsert_oidc_user("https://issuer.example.test", "owner", email="owner@example.test", path=path)

    plan_id = admin_store.seed_first_owner_plan(owner["id"], path=path)

    assert plan_id is not None
    assert _stored(path, plan_id)["can_use"]["mesh"] == {"items": "all"}
