"""The package lifecycle (origin/dot/package-uninstall) on records that hold references (decision D26).

An installed package keeps a proof that each record it wrote is unchanged, and removal deletes the records that still
match and detaches the ones an operator edited. Moving a record's secrets into the store rewrites the record, so a proof
made before the move would never match again: every removal would detach instead of remove. These tests install, migrate
to references and remove, check that the proofs are keyed (a bare hash of a record with a short secret can be guessed),
and drive the enable and remove routes of the real app to check the whole answer is projected and keeps its headers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import admin_store, extension_packages, record_carriers, record_integrity, runtime_secrets, secret_refs, secret_store, service_registry
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from tests.test_admin_owner_holes_round4 import _git_package
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401 - fixtures

OWNER = AdminContext(subject="owner-user", roles=frozenset({"owner"}), actor_kind="user", auth_method="key")
SHORT = "1234"  # a low-entropy secret: a bare hash of the record that holds it is a guess oracle
PROVENANCE = {"package_id": "pkg", "package_version": "1.0", "package_hash": "hash-1", "source": "imported"}


def _record(service_id: str, secret: str, **extra) -> dict:
    return {"id": service_id, "name": service_id, "kind": "engine", "enabled": True, "capabilities": ["chat"], "source": "imported",
            "provenance": dict(PROVENANCE), **extra,
            "config": {"host": f"{service_id}.invalid", "port": 8080, "protocol": "openai-compatible",
                       "models": [{"id": f"{service_id}-m", "upstream_model": "m"}],
                       "headers": [{"name": "X-Vendor", "value": secret}], "env": {"MODE": secret}}}


def _normalized(records: dict) -> dict:
    return service_registry.normalize_service_registry({"schema_version": 2, "services": records})["services"]


def _legacy_database(path: Path, *, edited: bool = True) -> dict:
    """A schema 10 database: a package installed before D26 (its proof is a bare SHA-256 of each record, secrets inline)."""
    installed = _normalized({"pkg-svc": _record("pkg-svc", SHORT), "edited-svc": _record("edited-svc", SHORT)})
    stored = copy.deepcopy(installed)
    if edited:
        stored["edited-svc"]["config"]["port"] = 9090  # an operator changed this one after the install
    index = {"schema_version": 1, "sources": [], "updated_at": 1, "packages": {"pkg": {
        "id": "pkg", "name": "pkg", "version": "1.0", "enabled": True, "package_hash": "hash-1", "source": {"type": "git"},
        "services": ["pkg-svc", "edited-svc"],
        **extension_packages._legacy_digest_proof({"services": installed})}}}
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=10")
        conn.execute("DELETE FROM operator_settings WHERE key IN ('service_registry','extension_package_index')")
        for key, value in (("service_registry", {"schema_version": 2, "services": stored}), ("extension_package_index", index)):
            conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES (?,?,0)", (key, json.dumps(value)))
            conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES (?, 1)", (key,))
    return index


def _stored(path: Path, key: str) -> dict:
    with sqlite3.connect(path) as conn:
        return json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()[0])


def _secret_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_secrets'").fetchone():
            return 0
        return conn.execute("SELECT count(*) FROM provider_secrets WHERE id LIKE 'sref_%'").fetchone()[0]


# --- install, migrate to references, remove --------------------------------------------------------------------


def test_a_package_installed_before_the_move_is_removed_not_detached_after_it(tmp_path) -> None:
    path = tmp_path / "admin.db"
    _legacy_database(path)

    admin_store.init_db(path)  # schema 11: the secrets move to the store, every proof is made again

    registry = _stored(path, "service_registry")["services"]
    assert SHORT not in json.dumps(registry), "the move left a secret in a record"
    assert _secret_count(path) > 0
    runtime_secrets.configure(path)
    try:
        before = _secret_count(path)
        removed = AdminSQLiteStore(path).remove_extension_package(package_id="pkg", actor=OWNER)
    finally:
        runtime_secrets.configure(None)

    actions = {item["id"]: item["action"] for item in removed["affected_records"]}
    assert actions == {"pkg-svc": "removed", "edited-svc": "detached"}, actions
    after = _stored(path, "service_registry")["services"]
    assert "pkg-svc" not in after and "edited-svc" in after
    assert "package_id" not in after["edited-svc"]["provenance"], "an operator's edit is kept and no longer belongs to the package"
    assert "pkg" not in _stored(path, "extension_package_index")["packages"]
    # the removed record's entries are gone with it; the kept record keeps its own
    assert 0 < _secret_count(path) < before
    assert all(secret_refs.configured(ref, db_path=path) for ref in record_carriers.refs_in(after["edited-svc"]))


def test_an_unedited_package_is_wholly_removed_after_the_move(tmp_path) -> None:
    path = tmp_path / "admin.db"
    _legacy_database(path, edited=False)
    admin_store.init_db(path)

    removed = AdminSQLiteStore(path).remove_extension_package(package_id="pkg", actor=OWNER)

    assert {item["action"] for item in removed["affected_records"]} == {"removed"}
    assert not [key for key in _stored(path, "service_registry")["services"]]
    assert _secret_count(path) == 0, "a removed package left secrets in the store"


def test_the_proofs_after_the_move_are_keyed_and_the_bare_digests_are_gone(tmp_path) -> None:
    path = tmp_path / "admin.db"
    _legacy_database(path)
    admin_store.init_db(path)

    package = _stored(path, "extension_package_index")["packages"]["pkg"]

    assert package["installed_records_version"] == extension_packages.INSTALLED_RECORDS_VERSION
    entries = package["installed_records"]["services"]
    assert all("sha256" not in entry and set(entry) == {"kind", "id", "mac"} for entry in entries.values())
    assert entries["pkg-svc"]["mac"] and entries["edited-svc"]["mac"] is None, "an edited record gets a proof that matches nothing"
    # a bare hash of the record as the install wrote it (the guess oracle for a short secret) is nowhere in the index
    installed = _normalized({"pkg-svc": _record("pkg-svc", SHORT)})["pkg-svc"]
    canonical = json.dumps(installed, sort_keys=True, separators=(",", ":"), default=str).encode()
    assert hashlib.sha256(canonical).hexdigest() not in json.dumps(_stored(path, "extension_package_index"))


def test_the_proof_depends_on_the_gateways_own_key(tmp_path) -> None:
    record = _normalized({"pkg-svc": _record("pkg-svc", SHORT)})["pkg-svc"]
    first, second = tmp_path / "one" / "admin.db", tmp_path / "two" / "admin.db"
    keys = [record_integrity.install_key(first), record_integrity.install_key(second)]

    macs = [extension_packages.installed_record_digest(record, key) for key in keys]

    assert keys[0] != keys[1] and macs[0] != macs[1]
    assert all(mac != extension_packages.installed_record_digest(record) for mac in macs), "a keyed proof is not the bare hash"
    assert (first.parent / secret_store.DEFAULT_KEY_FILE).exists()


def test_an_edit_after_the_move_is_still_detected_by_the_keyed_proof(tmp_path) -> None:
    path = tmp_path / "admin.db"
    _legacy_database(path, edited=False)
    admin_store.init_db(path)
    registry = _stored(path, "service_registry")
    registry["services"]["pkg-svc"]["config"]["port"] = 9999  # an operator edit after the move
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))

    removed = AdminSQLiteStore(path).remove_extension_package(package_id="pkg", actor=OWNER)

    assert {item["id"]: item["action"] for item in removed["affected_records"]} == {"pkg-svc": "detached", "edited-svc": "removed"}


def test_a_migration_with_no_package_leaves_the_key_file_alone(tmp_path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=10")

    admin_store.init_db(path)

    assert not (tmp_path / secret_store.DEFAULT_KEY_FILE).exists()


def test_the_migration_is_idempotent_for_the_proofs(tmp_path) -> None:
    path = tmp_path / "admin.db"
    _legacy_database(path)
    admin_store.init_db(path)
    once = _stored(path, "extension_package_index")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=10")

    admin_store.init_db(path)

    assert _stored(path, "extension_package_index") == once


# --- a package installed after the move ----------------------------------------------------------------------------

YAML = ("id: pkgsvc\nname: Plain\nconfig:\n  protocol: openai-compatible\n  host: 192.168.1.80\n  port: 8000\n"
        "  headers:\n    - {name: X-Vendor, value: " + SHORT + "}\n  models:\n    - {id: m, upstream_model: m}\n")


def test_a_package_installed_after_the_move_keeps_a_keyed_proof_of_the_stored_form_and_is_removed(tmp_path, monkeypatch) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=path)
    source = _git_package(monkeypatch, tmp_path, YAML, name="pkg")
    store = AdminSQLiteStore(path)
    generation = admin_store.get_service_registry_with_generation(path=path)["operator_settings_generation"]

    store.enable_extension_package(source=source, actor=OWNER, expected_generation=generation)

    stored = _stored(path, "service_registry")["services"]["pkgsvc"]
    assert SHORT not in json.dumps(stored) and record_carriers.refs_in(stored), "the install stored the secret in the record"
    package = _stored(path, "extension_package_index")["packages"]["pkg"]
    entry = package["installed_records"]["services"]["pkgsvc"]
    assert package["installed_records_version"] == 3 and set(entry) == {"kind", "id", "mac"}
    assert entry["mac"] == extension_packages.installed_record_digest(stored, record_integrity.install_key(path)), "digest of the stored form"

    removed = store.remove_extension_package(package_id="pkg", actor=OWNER)

    assert [(item["id"], item["action"]) for item in removed["affected_records"]] == [("pkgsvc", "removed")]
    assert "pkgsvc" not in _stored(path, "service_registry")["services"] and _secret_count(path) == 0


# --- the answer of the enable and remove routes ---------------------------------------------------------------------

REGISTRY = "/admin/api/operator/service-registry"


def _headers(client) -> dict:
    return {**csrf_for(client), "If-Match": client.get(REGISTRY).headers["etag"]}


def _credentialed_source(monkeypatch, tmp_path) -> dict:
    source = _git_package(monkeypatch, tmp_path, YAML, name="pkg")
    return {**source, "url": "https://canpkguser:canpkgpass@git.invalid/pkg.git?access_token=canpkgquery"}


def test_the_enable_and_remove_answers_are_projected_whole_and_keep_their_etag_and_no_store(gateway, tmp_path, monkeypatch) -> None:
    owner, *_ = owner_and_admin(gateway)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)

    enabled = owner.post("/admin/api/extensions/enable", headers=_headers(owner), json={"source": _credentialed_source(monkeypatch, tmp_path)})

    assert enabled.status_code == 200, enabled.text
    assert enabled.headers["cache-control"] == "no-store" and enabled.headers["etag"].startswith('"os-')
    body = enabled.json()
    assert {"extension_package", "extension_packages", "review", "service_registry", "operator_settings_generation"} <= set(body)
    for secret in ("canpkguser", "canpkgpass", "canpkgquery", SHORT):
        assert secret not in enabled.text, secret
    source = body["extension_package"]["source"]
    assert source["url"] == "https://git.invalid/pkg.git" and source["url_entries"]["userinfo"]["configured"] is True
    assert "sref_" not in enabled.text

    removed = owner.request("DELETE", "/admin/api/extensions/pkg", headers=_headers(owner))

    assert removed.status_code == 200, removed.text
    assert removed.headers["cache-control"] == "no-store" and removed.headers["etag"].startswith('"os-')
    assert [(item["id"], item["action"]) for item in removed.json()["affected_records"]] == [("pkgsvc", "removed")]
    assert "pkg" not in removed.json()["extension_packages"]["packages"]
    assert "sref_" not in removed.text


def test_remove_keeps_its_404_and_412_and_the_etag_survives_a_disable(gateway, tmp_path, monkeypatch) -> None:
    owner, *_ = owner_and_admin(gateway)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    assert owner.post("/admin/api/extensions/enable", headers=_headers(owner),
                      json={"source": _credentialed_source(monkeypatch, tmp_path)}).status_code == 200

    unknown = owner.request("DELETE", "/admin/api/extensions/nothing", headers=_headers(owner))
    stale = owner.request("DELETE", "/admin/api/extensions/pkg", headers={**csrf_for(owner), "If-Match": '"os-1"'})
    disabled = owner.request("DELETE", "/admin/api/extensions/pkg?mode=disable", headers=_headers(owner))

    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "extension_package_not_found"
    assert stale.status_code == 412, stale.text
    assert disabled.status_code == 200 and disabled.headers["etag"].startswith('"os-') and disabled.headers["cache-control"] == "no-store"
    assert [item["action"] for item in disabled.json()["affected_records"]] == ["disabled"]


def test_a_projected_response_carries_only_the_headers_registered_for_it() -> None:
    from gateway import egress
    from gateway.projection import ProjectionViolation

    egress.projected_response({"operator_settings_generation": 3}, headers={"ETag": '"os-3"', "Cache-Control": "no-store"})
    with pytest.raises(ProjectionViolation):
        egress.projected_response({"operator_settings_generation": 3}, headers={"X-Anything": "1"})
