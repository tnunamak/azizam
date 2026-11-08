import copy
import proxy
from gateway import registry_snapshot
from tests.test_workflow_override import *  # noqa
from tests.test_workflow_override import _put, _with_workflow, _owner_manifest, _bundled_manifest


def test_probe(live, client):
    names = registry_snapshot.dynamic_alias_names(proxy.config.get("model_aliases"))
    assert names, "no dynamic aliases in config"
    name = names[0][0]
    # a stored workflow whose alias is a dynamic alias
    manifest = {**_bundled_manifest(), "id": "owner.alias.clash", "aliases": [name]}
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), manifest)
    r = _put(client, live, registry)
    # a service model with that id
    reg2 = copy.deepcopy(proxy.ADMIN_STORE.get_service_registry())
    reg2.setdefault("services", {})["x"] = {"name": "x", "config": {"models": [{"id": name, "kind": "chat"}]}}
    r2 = _put(client, live, reg2)
    assert 0, (name, r.status_code, r.text[:300], r2.status_code, r2.text[:300])
