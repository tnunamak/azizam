"""Decision D26: a record holds references, never a secret; one idempotent migration moves what older records held.

The tests plant a distinct text in every carrier (header value, environment value, command argument after a
credential flag, command line, URL user info and query, probe path query, package source URL), run the move, and
check three things: no planted text is left in the record, each is stored apart and resolves to exactly what the
legacy record sent, and a record with nothing to move comes back equal with nothing written.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import admin_store, record_carriers, runtime_secrets, secret_refs, secret_store

FIXTURE = Path(__file__).parent / "fixtures" / "live_shaped_registry.json"
S = {name: f"SEED-{name}" for name in (
    "header", "env", "argv-flag", "argv-eq", "argv-attached", "line", "user", "password", "query", "fragment",
    "probe", "package-user", "package-query")}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


class Recorder:
    """A ``put`` that stores into the database and counts."""

    def __init__(self, path: Path) -> None:
        self.path, self.calls = path, []

    def __call__(self, value: str, origin: str | None = None) -> str:
        self.calls.append((value, origin))
        return secret_refs.store(value, db_path=self.path, origin=origin)


def _legacy_record() -> dict:
    return {
        "id": "engine-a", "name": "Engine A", "kind": "engine", "enabled": True,
        "config": {
            "address": f"http://{S['user']}:{S['password']}@engine.invalid:8080/v1?token={S['query']}#{S['fragment']}",
            "host": "engine.invalid", "port": 8080,
            "headers": [{"name": "X-Vendor", "value": S["header"]}, {"name": "X-Empty", "value": ""}],
            "env": {"VENDOR_MODE": S["env"]},
        },
        "process": {
            "driver": "process", "port": {"fixed": 8080},
            "start": ["runner", "--api-key", S["argv-flag"], f"--token={S['argv-eq']}", f"-p{S['argv-attached']}", "--tokenizer", "plain"],
            "stop": f"stopper --password {S['line']} --now",
            "env": {"API_TOKEN": S["env"]},
            "health_endpoint": f"/health?access_token={S['probe']}",
        },
        "observations": {"ready": {"http": {"path": f"/ready?sig={S['probe']}", "expect": 200}}},
    }


def test_the_move_leaves_no_planted_text_in_the_record_and_stores_each_apart(db):
    record = _legacy_record()
    put = Recorder(db)

    moved, paths = record_carriers.externalize_service(record, put)

    text = json.dumps(moved)
    assert not [name for name, seed in S.items() if seed in text and not name.startswith("package")], text[:400]
    assert {"config.headers", "process.start", "process.stop", "process.env.API_TOKEN", "config.env.VENDOR_MODE",
            "config.address", "process.health_endpoint", "observations.ready.http.path"} <= set(paths)
    assert all(secret_refs.configured(ref, db_path=db) for ref in record_carriers.refs_in(moved))
    # a public part stays public: the literal flags, the header name, the endpoint without its credentials
    assert moved["process"]["start"][:2] == ["runner", "--api-key"] and "--tokenizer" in moved["process"]["start"]
    assert moved["config"]["headers"][0]["name"] == "X-Vendor"
    assert moved["config"]["address"] == "http://engine.invalid:8080/v1"


def test_what_the_runtime_resolves_is_exactly_what_the_legacy_record_sent(db):
    legacy = _legacy_record()
    moved, _ = record_carriers.externalize_service(legacy, Recorder(db))

    assert runtime_secrets.argv(moved["process"]["start"]) == legacy["process"]["start"]
    assert runtime_secrets.argv(moved["process"]["stop"]) == ["stopper", "--password", S["line"], "--now"]
    assert runtime_secrets.env_mapping(moved["process"]["env"]) == legacy["process"]["env"]
    assert runtime_secrets.env_mapping(moved["config"]["env"]) == legacy["config"]["env"]
    assert runtime_secrets.header_pairs(moved["config"]["headers"], origin="http://engine.invalid:8080") == {"X-Vendor": S["header"]}
    assert runtime_secrets.url(moved["config"], "address") == legacy["config"]["address"]
    assert runtime_secrets.url(moved["process"], "health_endpoint") == legacy["process"]["health_endpoint"]
    assert runtime_secrets.url(moved["observations"]["ready"]["http"], "path") == legacy["observations"]["ready"]["http"]["path"]


def test_a_second_pass_changes_nothing_and_stores_nothing(db):
    once, _ = record_carriers.externalize_service(_legacy_record(), Recorder(db))
    put = Recorder(db)

    twice, paths = record_carriers.externalize_service(once, put)

    assert twice == once and paths == [] and put.calls == []


def test_a_header_secret_is_bound_to_the_origin_it_was_stored_for(db):
    moved, _ = record_carriers.externalize_service(_legacy_record(), Recorder(db))
    rows = moved["config"]["headers"]
    assert runtime_secrets.header_pairs(rows, origin="http://engine.invalid:8080") == {"X-Vendor": S["header"]}
    assert runtime_secrets.header_pairs(rows, origin="http://elsewhere.invalid:8080") == {}  # moved address: not sent


def test_a_registry_shaped_like_the_live_one_comes_back_equal_and_writes_nothing(db):
    registry = json.loads(FIXTURE.read_text())
    put = Recorder(db)
    for service_id, record in registry["services"].items():
        moved, paths = record_carriers.externalize_service(record, put)
        assert moved == record and paths == [], service_id
    assert put.calls == []
    assert record_carriers.inline_carriers(registry["services"]["kobold"]) == []


@pytest.mark.parametrize("value", [
    "http://user:pw@host.invalid/v1", "https://host.invalid/v1?a=1&a=2&b=%20x", "/ready?token=t#frag", "user:pw@host.invalid",
    "http://host.invalid:8080/v1", "/health", "host.invalid", "http://[::1]:8080/p?x=y",
])
def test_a_url_is_taken_apart_and_put_back_exactly(value):
    public, parts = record_carriers.split_url(value)
    assert "@" not in public.split("/", 3)[2 if "://" in public else 0] and "?" not in public and "#" not in public
    query = parts.get("query")
    assert record_carriers.join_url(public, userinfo=parts.get("userinfo"), query=query, fragment=parts.get("fragment")) == value.replace("%20", "%20")


@pytest.mark.parametrize("value", ["http://host:notaport/x?token=abc", "http://a\x00b/x", "ht!tp://h/x?token=1"])
def test_a_url_that_cannot_be_read_is_stored_whole_and_never_echoed(db, value):
    holder = {"address": value}
    assert record_carriers.externalize_url(holder, "address", Recorder(db))
    assert holder["address"] == "" and "whole" in holder["address_entries"]
    assert runtime_secrets.url(holder, "address") == value


def test_a_secret_entry_may_be_short_and_keeps_no_suffix(db):
    ref = secret_refs.store("x", db_path=db)
    assert runtime_secrets.resolve(ref) == "x"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT suffix FROM provider_secrets WHERE id=?", (ref,)).fetchone()[0] == ""
    with pytest.raises(ValueError):
        secret_store.put_provider_secret("provider:k:api_key", "abcd", db_path=db)  # a provider key stays longer than 4


def test_the_metadata_of_a_reference_is_a_flag_and_never_reads_the_value(db, monkeypatch):
    ref = secret_refs.store("SEED-flag", db_path=db)
    monkeypatch.setattr(secret_store, "get_provider_secret", lambda *a, **k: pytest.fail("management metadata decrypted"))
    assert secret_refs.configured(ref, db_path=db) is True
    assert secret_refs.configured("sref_" + "0" * 24, db_path=db) is False


def _v10_database(path: Path, registry: dict, index: dict | None = None) -> None:
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=10")
        conn.execute("DELETE FROM operator_settings WHERE key IN ('service_registry','extension_package_index')")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
        conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES ('service_registry', 1)")
        if index is not None:
            conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('extension_package_index',?,0)", (json.dumps(index),))
            conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES ('extension_package_index', 1)")


def test_the_schema_11_migration_moves_the_inline_secrets_once_and_audits_counts_only(tmp_path):
    path = tmp_path / "admin.db"
    index = {"schema_version": 1, "sources": [{"type": "git", "url": f"https://{S['package-user']}:pw@git.invalid/r.git?access_token={S['package-query']}"}],
             "packages": {}, "updated_at": 1}
    _v10_database(path, {"schema_version": 2, "services": {"engine-a": _legacy_record()}}, index)

    admin_store.init_db(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        stored = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]
        stored_index = conn.execute("SELECT value_json FROM operator_settings WHERE key='extension_package_index'").fetchone()[0]
        generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0]
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action='operator.secrets.externalize'").fetchall()
    assert not [seed for seed in S.values() if seed in stored + stored_index]
    assert generation == 2 and len(audit) == 1 and not [seed for seed in S.values() if seed in audit[0][0]]
    runtime_secrets.configure(path)
    record = json.loads(stored)["services"]["engine-a"]
    assert runtime_secrets.argv(record["process"]["start"])[:3] == ["runner", "--api-key", S["argv-flag"]]
    source = json.loads(stored_index)["sources"][0]
    assert source["url"] == "https://git.invalid/r.git" and S["package-query"] in runtime_secrets.url(source, "url")
    admin_store.init_db(path)  # a second start moves nothing
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0] == stored
        assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0] == 2
    runtime_secrets.configure(None)


def test_the_schema_11_migration_on_a_live_shaped_registry_is_a_no_op(tmp_path):
    path = tmp_path / "admin.db"
    registry = json.loads(FIXTURE.read_text())
    _v10_database(path, registry)
    before = json.dumps(registry)

    admin_store.init_db(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0] == before
        assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_events WHERE action='operator.secrets.externalize'").fetchone()[0] == 0
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        count = conn.execute("SELECT count(*) FROM provider_secrets").fetchone()[0] if "provider_secrets" in tables else 0
    assert count == 0
    assert not (tmp_path / secret_store.DEFAULT_KEY_FILE).exists(), "a migration with nothing to move must not create a key"


def test_the_admin_key_of_a_load_unload_driver_is_one_entry(db):
    record = {"id": "e", "lifecycle": {"driver": "http-load-unload", "driver_config": {"load_url": "http://h/load"}, "admin_key": S["env"]}}
    moved, paths = record_carriers.externalize_service(record, Recorder(db))
    assert paths == ["lifecycle.admin_key"] and S["env"] not in json.dumps(moved)
    assert runtime_secrets.scalar(moved["lifecycle"], "admin_key") == S["env"]
