"""Set the instance presentation (section ``instance``) in a scratch admin.db, the way the owner's save does."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from gateway import admin_store, config_fold

_TEST_ACTOR = SimpleNamespace(subject="test-owner", actor_kind="user", audit_actor_id="test-owner", actor_label="test-owner")


def set_instance(path: Path, fields: Mapping[str, str]) -> None:
    """Write ``fields`` over the instance now in force (the release defaults on a fresh database)."""
    admin_store.init_db(path)
    current, revision = config_fold.instance_with_revision(path)
    config_fold.put_instance(path, {**current, **fields}, _TEST_ACTOR, expected_revision=revision)
