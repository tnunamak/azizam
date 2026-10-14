"""Round 4: extension enable and the service `lifecycle` block are not side doors.

An administrator never handles the owner's credentials and never makes the gateway
run something. Two holes from the round-4 review:

* B3-a: `POST /extensions/enable` let a package carry its own key reference and
  `credential_origin`, so the owner's key went to the package's host.
* B3-b: the record's top-level `lifecycle` block was not scanned. `driver_config.admin_key_env`
  is sent to the record's host, and the `command` driver runs shell commands as the gateway user.
"""
import pytest

from gateway import service_registry
from tests.test_admin_cannot_act_as_owner import (  # noqa: F401
    OWNER_KEY_ENV,
    _compiled,
    _etag,
    _service_body,
    _services,
)
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)


def _package(tmp_path, service_yaml, name="evilpkg"):
    """Write a one-service extension package to disk and return its source."""
    package_dir = tmp_path / name
    (package_dir / "objects").mkdir(parents=True)
    (package_dir / "extension.yml").write_text(
        f"schema_version: 1\nkind: extension_package\nid: {name}\nversion: 0.1.0\n"
        "objects:\n  services:\n    - objects/service.yml\n", encoding="utf-8")
    (package_dir / "objects" / "service.yml").write_text(service_yaml, encoding="utf-8")
    return {"type": "local_directory", "path": str(package_dir)}


def _enable(client, source):
    return client.post("/admin/api/extensions/enable",
                       headers={**csrf_for(client), "If-Match": _etag(client)}, json={"source": source})


_EVIL_PACKAGE_SERVICE = """
id: evil
name: Evil
capabilities: [openai]
config:
  protocol: openai-compatible
  host: attacker.example
  port: 443
  scheme: https
  api_key_env: OPENAI_API_KEY
  credential_origin: https://attacker.example:443
  models:
    - {id: m, upstream_model: m}
"""


def test_an_administrator_cannot_enable_a_package_that_brings_its_own_credential_and_origin(gateway, tmp_path):
    _, _, _, admin = owner_and_admin(gateway)
    refused = _enable(admin, _package(tmp_path, _EVIL_PACKAGE_SERVICE))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "evil" not in _services(gateway)
    assert OWNER_KEY_ENV not in _compiled(gateway, admin)


def test_an_administrator_cannot_enable_a_package_that_brings_only_a_credential_origin(gateway, tmp_path):
    _, _, _, admin = owner_and_admin(gateway)
    only_origin = _EVIL_PACKAGE_SERVICE.replace("  api_key_env: OPENAI_API_KEY\n", "")
    refused = _enable(admin, _package(tmp_path, only_origin))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"


def test_an_administrator_can_enable_a_package_with_no_credential(gateway, tmp_path):
    _, _, _, admin = owner_and_admin(gateway)
    plain = ("id: plainsvc\nname: Plain\nconfig:\n  protocol: openai-compatible\n"
             "  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n")
    enabled = _enable(admin, _package(tmp_path, plain, name="plainpkg"))
    assert enabled.status_code == 200, enabled.text
    assert "plainsvc" in _services(gateway)


def test_the_owner_can_enable_a_package_with_a_credential(gateway, tmp_path):
    owner, _, _, _ = owner_and_admin(gateway)
    own_host = _EVIL_PACKAGE_SERVICE.replace("attacker.example", "engine.example")
    enabled = _enable(owner, _package(tmp_path, own_host))
    assert enabled.status_code == 200, enabled.text
    assert "evil" in _services(gateway)


_COMMAND_LIFECYCLE = {"driver": "command", "driver_config": {"start": ["sh", "-c", "id"]}}
_ADMIN_KEY_LIFECYCLE = {"driver": "http-load-unload",
                        "driver_config": {"admin_key_env": "OPENAI_API_KEY", "admin_header": "x-admin-key",
                                          "load_url": "/load", "unload_url": "/unload"}}
_DRIVEN = pytest.mark.parametrize("lifecycle", [_COMMAND_LIFECYCLE, _ADMIN_KEY_LIFECYCLE],
                                  ids=["command-driver", "admin-key-env"])


def _registry_with_lifecycle(lifecycle):
    return service_registry.normalize_service_registry({"services": {"engine": {
        "id": "engine", "name": "Engine", "kind": "provider", "lifecycle": lifecycle,
        "config": {"protocol": "openai-compatible", "host": "attacker.example", "port": 443,
                   "models": [{"id": "m", "upstream_model": "m"}]}}}})


@_DRIVEN
def test_an_administrator_cannot_put_a_registry_with_a_driven_lifecycle(gateway, lifecycle):
    _, _, _, admin = owner_and_admin(gateway)
    refused = admin.put("/admin/api/operator/service-registry",
                        headers={**csrf_for(admin), "If-Match": _etag(admin)},
                        json=_registry_with_lifecycle(lifecycle))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "engine" not in _services(gateway)


@_DRIVEN
def test_an_administrator_cannot_create_a_service_with_a_driven_lifecycle(gateway, lifecycle):
    _, _, _, admin = owner_and_admin(gateway)
    refused = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body(name="Evil", lifecycle=lifecycle))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "evil" not in _services(gateway)


@_DRIVEN
def test_an_administrator_cannot_edit_a_service_into_a_driven_lifecycle(gateway, lifecycle):
    _, _, _, admin = owner_and_admin(gateway)
    assert admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                      json=_service_body()).status_code == 201
    refused = admin.patch("/admin/api/operator/services/engine",
                          headers={**csrf_for(admin), "If-Match": _etag(admin)}, json={"lifecycle": lifecycle})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert not _services(gateway)["engine"]["lifecycle"].get("driver")


def test_an_administrator_cannot_enable_a_package_with_a_driven_lifecycle(gateway, tmp_path):
    _, _, _, admin = owner_and_admin(gateway)
    service = ("id: cmd\nname: Cmd\nlifecycle:\n  driver: command\n  driver_config:\n"
               "    start: [sh, -c, id]\nconfig:\n  protocol: openai-compatible\n  host: 192.168.1.80\n"
               "  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n")
    refused = _enable(admin, _package(tmp_path, service, name="cmdpkg"))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "owner_required"
    assert "cmd" not in _services(gateway)


def test_an_administrator_cannot_change_or_remove_the_owners_driven_lifecycle(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    created = owner.post("/admin/api/operator/services", headers={**csrf_for(owner), "If-Match": _etag(owner)},
                         json=_service_body(lifecycle=_COMMAND_LIFECYCLE))
    assert created.status_code == 201, created.text
    for change in ({"lifecycle": {"driver": "external"}}, {"lifecycle": _ADMIN_KEY_LIFECYCLE}):
        refused = admin.patch("/admin/api/operator/services/engine",
                              headers={**csrf_for(admin), "If-Match": _etag(admin)}, json=change)
        assert refused.status_code == 403, (change, refused.text)
    # Edits that leave the block alone stay open to administrators.
    allowed = admin.patch("/admin/api/operator/services/engine",
                          headers={**csrf_for(admin), "If-Match": _etag(admin)}, json={"note": "fine"})
    assert allowed.status_code == 200, allowed.text
    assert _services(gateway)["engine"]["lifecycle"]["driver"] == "command"
    # The owner may change it.
    changed = owner.patch("/admin/api/operator/services/engine",
                          headers={**csrf_for(owner), "If-Match": _etag(owner)},
                          json={"lifecycle": {"driver": "external"}})
    assert changed.status_code == 200, changed.text


def test_an_administrator_can_still_set_an_external_lifecycle(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    created = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)},
                         json=_service_body(lifecycle={"driver": "external"}))
    assert created.status_code == 201, created.text


@pytest.mark.parametrize("key", ["admin_key_env", "token_ref", "x_secret_id", "api_key", "vault_env"])
def test_credential_references_flag_any_reference_shaped_key_at_any_depth(key):
    record = {"config": {"nested": [{"deep": {key: "X"}}]}, "lifecycle": {"driver_config": {key: "X"}}}
    found = service_registry.credential_references(record)
    assert f"config.nested[0].deep.{key}" in found
    assert f"lifecycle.driver_config.{key}" in found


def test_credential_references_do_not_flag_plain_fields():
    record = {"config": {"host": "h", "port": 1, "models": [{"id": "m"}]},
              "lifecycle": {"type": "external", "driver_config": {"load_url": "/x"}}}
    assert service_registry.credential_references(record) == {}
