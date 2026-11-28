"""The registry write checks the names the snapshot builder holds, dynamic model aliases included (builder W, item 3)."""
from __future__ import annotations

import proxy
from gateway import registry_snapshot
from tests.test_workflow_override import (  # noqa: F401 - fixtures
    _bundled_manifest, _generation, _put, _with_workflow, client, live,
)


def test_a_workflow_named_like_a_dynamic_model_alias_is_refused_before_anything_is_saved(live, client):
    names = [name for name, _ in registry_snapshot.dynamic_alias_names(proxy.config.get("model_aliases"))]
    assert names, "the shipped config declares dynamic model aliases"
    before = _generation(live)
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), {**_bundled_manifest(), "id": names[0]})

    refused = _put(client, live, registry)

    assert (refused.status_code, refused.json()["error"]["code"]) == (422, "id_clash"), refused.text
    assert _generation(live) == before          # refused at the write, not saved and then unable to load
