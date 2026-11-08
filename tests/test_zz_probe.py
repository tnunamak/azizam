import copy
import proxy
from gateway import registry_snapshot
from tests.test_workflow_override import *  # noqa
from tests.test_workflow_override import _put, _with_workflow, _owner_manifest, _bundled_manifest


def test_probe(live, client):
    names = registry_snapshot.dynamic_alias_names(proxy.config.get("model_aliases"))
    name = names[0][0]
    manifest = {**_bundled_manifest(), "id": name}
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), manifest)
    r = _put(client, live, registry)
    assert 0, (name, r.status_code, r.text[:300])
