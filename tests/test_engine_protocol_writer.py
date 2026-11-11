"""The owner writes protocol descriptions through one item route; the dispatch loader sees each write at once.

Descriptions the owner installs are rows in the admin database. Shipped files stay read-only: a shipped id refuses every
write. A write validates the raw document with the loader's own parser. Concurrency is settled by the store transaction,
so two workers on one database agree without a restart.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest
import yaml

from gateway import admin_store, egress, engine_protocol_admin, protocol_description_store
from gateway.engine_protocol import description

ROOT = Path(__file__).resolve().parent.parent
PATH = "/admin/api/engine-protocols"


def _document(name="tiny", title="Tiny protocol", **extra):
    return {"protocol_version": 1, "id": name, "title": title, "executor": "http-v1",
            "operations": {"models": {"class": "inventory", "request": {"method": "GET", "path": "/models"},
                                       "response": {"framing": "json", "select": {"ids": "/data/*/id"}}}}, **extra}


def _text(name="tiny", title="Tiny protocol", **extra):
    return yaml.safe_dump(_document(name, title, **extra))


@pytest.fixture
def env(tmp_path, monkeypatch):
    seeds, overlay, db = tmp_path / "seed", tmp_path / "overlay", tmp_path / "admin.db"
    seeds.mkdir()
    overlay.mkdir()
    (seeds / "seeded.yml").write_text(_text("seeded", "Seeded"))
    monkeypatch.setattr(description, "REGISTRY_DIR", seeds)
    monkeypatch.setenv("AZIZAM_PROTOCOLS_DIR", str(overlay))
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    monkeypatch.setattr(protocol_description_store, "_provider", None)  # a proxy import in this worker registered its own
    admin_store.init_db(db)
    return SimpleNamespace(seeds=seeds, overlay=overlay, db=db)


def _client(roles=("owner", "admin")):
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)
    engine_protocol_admin.register_engine_protocol_routes(router, guard=lambda request: SimpleNamespace(roles=roles))
    app.include_router(router)
    return TestClient(app)


def _put(client, name, text, *, kind="application/yaml", **headers):
    return client.put(f"{PATH}/{name}", content=text, headers={"Content-Type": kind, **headers})


def _create(client, name="tiny", text=None):
    response = _put(client, name, _text(name) if text is None else text, **{"If-None-Match": "*"})
    assert response.status_code == 201, response.text
    return response.headers["ETag"]


def _rows(env):
    with sqlite3.connect(env.db) as conn:
        return conn.execute("SELECT id, revision FROM protocol_descriptions ORDER BY id").fetchall()


def _listing(client):
    return {row["id"]: row for row in client.get(PATH).json()["protocols"]}


@pytest.mark.parametrize("kind", ["application/yaml", "application/json"])
def test_put_creates_an_owner_row_from_raw_yaml_or_json(env, kind):
    client = _client()
    raw = _text() if kind == "application/yaml" else json.dumps(_document())
    created = _put(client, "tiny", raw, kind=kind, **{"If-None-Match": "*"})
    assert created.status_code == 201, created.text
    etag = created.headers["ETag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    fetched = client.get(f"{PATH}/tiny")
    assert fetched.status_code == 200 and fetched.headers["ETag"] == etag
    assert yaml.safe_load(fetched.json()["text"]) == _document()
    assert [row[0] for row in _rows(env)] == ["tiny"]
    row = _listing(client)["tiny"]
    assert row["source"] == "user" and row["operations"] == ["models"] and row["dispatch_supported"] is True
    assert set(_listing(client)) == {"seeded", "tiny"}
    assert list(env.overlay.iterdir()) == [] and sorted(p.name for p in env.seeds.iterdir()) == ["seeded.yml"]


def test_get_one_item_unknown_and_shipped(env):
    client = _client()
    missing = client.get(f"{PATH}/nothing")
    assert missing.status_code == 404 and str(env.db) not in missing.text
    shipped = client.get(f"{PATH}/seeded")
    assert shipped.status_code == 200 and shipped.json()["source"] == "seed"
    assert yaml.safe_load(shipped.json()["text"]) == _document("seeded", "Seeded")
    assert client.get(f"{PATH}/seeded").headers["ETag"] == shipped.headers["ETag"]


def test_replace_uses_the_stored_revision_and_the_next_read_sees_it(env):
    client = _client()
    first = _create(client)
    replaced = _put(client, "tiny", _text(title="Tiny two"), **{"If-Match": first})
    assert replaced.status_code == 200
    second = replaced.headers["ETag"]
    assert second != first
    assert client.get(f"{PATH}/tiny").headers["ETag"] == second
    assert _listing(client)["tiny"]["title"] == "Tiny two"
    assert len(_rows(env)) == 1


def _semantic_invalid():
    doc = _document()
    doc["operations"]["models"]["response"]["select"]["ids"] = "not/a/pointer"
    return yaml.safe_dump(doc)


@pytest.mark.parametrize("bad", ["syntax", "schema", "semantic", "mismatch"])
@pytest.mark.parametrize("replace", [False, True])
def test_an_invalid_document_changes_nothing(env, bad, replace):
    raw = {"syntax": "operations: [", "schema": _text(not_a_field="unreadable-secret-text"),
           "semantic": _semantic_invalid(), "mismatch": _text("other")}[bad]
    if bad == "semantic":
        with pytest.raises(description.DescriptionError):
            description.parse_description_text(raw, source="probe")
    client = _client()
    etag = _create(client) if replace else None
    before = _rows(env)
    refused = _put(client, "tiny", raw, **({"If-Match": etag} if replace else {"If-None-Match": "*"}))
    assert refused.status_code == 422, refused.text
    for leaked in ("unreadable-secret-text", str(env.db), str(env.seeds)):
        assert leaked not in refused.text
    assert _rows(env) == before
    if replace:
        assert client.get(f"{PATH}/tiny").headers["ETag"] == etag
    else:
        assert client.get(f"{PATH}/tiny").status_code == 404


def test_a_shipped_id_refuses_every_write(env):
    client = _client()
    shipped_etag = client.get(f"{PATH}/seeded").headers["ETag"]
    before = (env.seeds / "seeded.yml").read_bytes()
    assert _put(client, "seeded", _text("seeded"), **{"If-None-Match": "*"}).status_code == 409
    assert _put(client, "seeded", _text("seeded"), **{"If-Match": shipped_etag}).status_code == 409
    assert client.delete(f"{PATH}/seeded", headers={"If-Match": shipped_etag}).status_code == 409
    assert (env.seeds / "seeded.yml").read_bytes() == before and _rows(env) == []


def test_create_needs_if_none_match_star(env):
    client = _client()
    assert _put(client, "tiny", _text()).status_code == 428
    assert _put(client, "tiny", _text(), **{"If-Match": '"pd-1"'}).status_code == 412
    assert _put(client, "tiny", _text(), **{"If-Match": '"x"', "If-None-Match": "*"}).status_code == 400
    assert _rows(env) == []
    etag = _create(client)
    again = _put(client, "tiny", _text(title="Other"), **{"If-None-Match": "*"})
    assert again.status_code == 412
    assert client.get(f"{PATH}/tiny").headers["ETag"] == etag


def test_replace_and_delete_need_the_current_strong_revision(env):
    client = _client()
    first = _create(client)
    second = _put(client, "tiny", _text(title="Two"), **{"If-Match": first}).headers["ETag"]
    for send in (lambda **h: _put(client, "tiny", _text(title="Three"), **h), lambda **h: client.delete(f"{PATH}/tiny", headers=h)):
        assert send().status_code == 428
        for stale in (first, '"unknown"', "W/" + second):
            assert send(**{"If-Match": stale}).status_code == 412
    assert client.get(f"{PATH}/tiny").headers["ETag"] == second
    assert _put(client, "tiny", _text(title="Two"), **{"If-Match": second}).status_code == 200


def test_delete_then_recreate_gets_a_new_revision(env):
    client = _client()
    first = _create(client)
    assert client.delete(f"{PATH}/tiny", headers={"If-Match": first}).status_code == 200
    assert client.get(f"{PATH}/tiny").status_code == 404 and "tiny" not in _listing(client) and _rows(env) == []
    assert client.delete(f"{PATH}/tiny", headers={"If-Match": first}).status_code == 412
    again = _create(client)
    assert again != first
    assert client.delete(f"{PATH}/tiny", headers={"If-Match": first}).status_code == 412
    assert "tiny" in _listing(client)


def _name_description_in_an_engine(env, protocol, *, enabled=False):
    registry = {"services": {"box": {"enabled": enabled, "config": {"connection": {"dispatch": "protocol", "protocol": protocol}}}}}
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at) VALUES ('service_registry', ?, 0)",
                     (json.dumps(registry),))


@pytest.mark.parametrize("enabled", [False, True])
def test_delete_refuses_a_description_an_engine_names(env, enabled):
    client = _client()
    etag = _create(client)
    _name_description_in_an_engine(env, "tiny", enabled=enabled)
    refused = client.delete(f"{PATH}/tiny", headers={"If-Match": etag})
    assert refused.status_code == 409 and "box" in refused.text
    assert client.get(f"{PATH}/tiny").headers["ETag"] == etag
    _name_description_in_an_engine(env, "something-else")
    assert client.delete(f"{PATH}/tiny", headers={"If-Match": etag}).status_code == 200


def test_competing_creates_leave_exactly_one_winner(env):
    outcomes = []

    def attempt(title):
        outcomes.append(_put(_client(), "tiny", _text(title=title), **{"If-None-Match": "*"}).status_code)

    threads = [threading.Thread(target=attempt, args=(f"T{n}",)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == [201] + [412] * 5 and len(_rows(env)) == 1


def test_competing_replaces_on_one_revision_leave_one_winner(env):
    client = _client()
    etag = _create(client)
    outcomes = []

    def attempt(title):
        outcomes.append(_put(_client(), "tiny", _text(title=title), **{"If-Match": etag}).status_code)

    threads = [threading.Thread(target=attempt, args=(f"T{n}",)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == [200] + [412] * 5


def test_only_the_owner_writes_and_the_guard_reply_passes_through(env):
    admin = _client(roles=("admin",))
    assert admin.get(PATH).status_code == 200 and admin.get(f"{PATH}/seeded").status_code == 200
    assert _put(admin, "tiny", _text(), **{"If-None-Match": "*"}).status_code == 403
    assert admin.delete(f"{PATH}/tiny", headers={"If-Match": '"x"'}).status_code == 403
    assert _rows(env) == []
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)
    engine_protocol_admin.register_engine_protocol_routes(
        router, guard=lambda request: egress.safe_error_response(403, "admin_required", "Sign in as an administrator."))
    app.include_router(router)
    denied = TestClient(app)
    assert denied.get(f"{PATH}/tiny").status_code == 403
    assert _put(denied, "tiny", _text(), **{"If-None-Match": "*"}).status_code == 403
    assert denied.delete(f"{PATH}/tiny", headers={"If-Match": '"x"'}).status_code == 403
    assert _rows(env) == []


def test_an_id_that_is_not_a_description_id_is_refused(env):
    assert _put(_client(), "Bad_Id", _text(), **{"If-None-Match": "*"}).status_code in (404, 422)


def test_the_loader_serves_owner_rows_over_files_and_keeps_shipped_files_first(env):
    client = _client()
    _create(client)
    assert description.get_description("tiny").title == "Tiny protocol"
    assert {"seeded", "tiny"} <= set(description.load_registry())
    assert set(description.load_registry(env.seeds)) == {"seeded"}


def test_a_second_worker_sees_create_replace_and_delete_without_a_restart(env, monkeypatch):
    worker = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, json\nfrom gateway.engine_protocol import description\n"
         "for line in sys.stdin:\n    docs = description.load_registry()\n"
         "    print(json.dumps({k: v.title for k, v in sorted(docs.items())}), flush=True)\n"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={"PATH": "/usr/bin:/bin", "AZIZAM_ADMIN_DB": str(env.db), "AZIZAM_PROTOCOLS_DIR": str(env.overlay),
             "PYTHONPATH": str(ROOT)})
    try:
        def seen():
            worker.stdin.write("go\n")
            worker.stdin.flush()
            return json.loads(worker.stdout.readline())

        assert "tiny" not in seen()
        client = _client()
        etag = _create(client)
        assert seen()["tiny"] == "Tiny protocol"
        etag = _put(client, "tiny", _text(title="Tiny two"), **{"If-Match": etag}).headers["ETag"]
        assert seen()["tiny"] == "Tiny two"
        assert client.delete(f"{PATH}/tiny", headers={"If-Match": etag}).status_code == 200
        assert "tiny" not in seen()
    finally:
        worker.stdin.close()
        worker.wait(timeout=20)


# --- Fixes from the independent review (c2d/t52-owner-fixes) -----------------------------------------------------------

def _document_with(name, *operations):
    doc = _document(name)
    model = doc["operations"]["models"]
    doc["operations"] = {op: json.loads(json.dumps(model)) for op in operations}
    return yaml.safe_dump(doc)


def _engine_advertising(env, protocol, *capabilities):
    registry = {"services": {"box": {"enabled": False, "capabilities": list(capabilities),
                                      "config": {"connection": {"dispatch": "protocol", "protocol": protocol}}}}}
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at) VALUES ('service_registry', ?, 0)",
                     (json.dumps(registry),))


def test_a_replace_that_drops_an_operation_an_engine_advertises_is_refused(env):
    client = _client()
    etag = _create(client, "tiny", _document_with("tiny", "models", "custom.infer"))
    _engine_advertising(env, "tiny", "custom.infer")
    refused = _put(client, "tiny", _document_with("tiny", "models"), **{"If-Match": etag})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "protocol_in_use"
    assert "box" in refused.text and "custom.infer" in refused.text
    assert "operations:" not in refused.text and "models" not in refused.json()["error"]["message"]
    assert client.get(f"{PATH}/tiny").headers["ETag"] == etag
    # A replace that keeps the advertised operation, or one nobody advertises, goes through.
    assert _put(client, "tiny", _document_with("tiny", "models", "custom.infer"), **{"If-Match": etag}).status_code == 200
    _engine_advertising(env, "tiny", "models")
    etag = client.get(f"{PATH}/tiny").headers["ETag"]
    assert _put(client, "tiny", _document_with("tiny", "models"), **{"If-Match": etag}).status_code == 200


def _future_release_ships(env, protocol_id, title="Shipped later"):
    (env.seeds / f"{protocol_id}.yml").write_text(_text(protocol_id, title))


def test_an_owner_row_wins_over_a_later_shipped_id_until_the_owner_deletes_it(env):
    client = _client()
    etag = _create(client, "future", _text("future", "Owner copy"))
    _future_release_ships(env, "future")
    assert client.get(f"{PATH}/future").json()["source"] == "user"
    assert description.get_description("future").title == "Owner copy"
    assert _listing(client)["future"]["title"] == "Owner copy" and _listing(client)["future"]["source"] == "user"
    # PUT stays refused for a shipped id, replace included.
    assert _put(client, "future", _text("future", "Edited"), **{"If-Match": etag}).status_code == 409
    assert description.get_description("future").title == "Owner copy"
    # DELETE needs the current revision, then removes the owner row and the shipped file shows again.
    assert client.delete(f"{PATH}/future", headers={"If-Match": '"pd-999"'}).status_code == 412
    assert client.delete(f"{PATH}/future").status_code == 428
    assert client.delete(f"{PATH}/future", headers={"If-Match": etag}).status_code == 200
    assert _rows(env) == []
    assert description.get_description("future").title == "Shipped later"
    assert client.get(f"{PATH}/future").json()["source"] == "seed"
    # With no owner row left, the shipped id is not removable again.
    assert client.delete(f"{PATH}/future", headers={"If-Match": etag}).status_code == 409


def test_a_shipped_owner_row_is_still_refused_while_an_engine_names_it(env):
    client = _client()
    etag = _create(client, "future", _text("future", "Owner copy"))
    _future_release_ships(env, "future")
    _name_description_in_an_engine(env, "future")
    assert client.delete(f"{PATH}/future", headers={"If-Match": etag}).status_code == 409


def test_the_routes_take_no_database_path_and_share_the_loaders_store(env, tmp_path, monkeypatch):
    import inspect
    assert "db_path" not in inspect.signature(engine_protocol_admin.register_engine_protocol_routes).parameters
    other = tmp_path / "other.db"
    admin_store.init_db(other)
    monkeypatch.setattr(protocol_description_store, "_provider", lambda: other)
    client = _client()
    _create(client)
    assert _rows(env) == []  # nothing in the default database
    with sqlite3.connect(other) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM protocol_descriptions")] == ["tiny"]
    assert client.get(f"{PATH}/tiny").status_code == 200 and "tiny" in _listing(client)
    assert description.get_description("tiny") is not None


def _lock_the_reads(monkeypatch):
    real = admin_store.shared_connection

    def failing(path):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(admin_store, "shared_connection", failing)
    return real


def test_a_failed_read_is_a_503_and_the_loader_keeps_its_last_good_snapshot(env, monkeypatch):
    client = _client()
    _create(client)
    assert "tiny" in description.load_registry()
    _lock_the_reads(monkeypatch)
    unavailable = client.get(f"{PATH}/tiny")
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "protocol_store_unavailable"
    assert "locked" not in unavailable.text and str(env.db) not in unavailable.text
    assert "tiny" in description.load_registry()
    assert any("stored descriptions" in row["file"] for row in description.registry_problems())
    with pytest.raises(sqlite3.OperationalError):
        protocol_description_store.read_all()
    with pytest.raises(sqlite3.OperationalError):
        protocol_description_store.generation()


def test_only_an_absent_database_or_table_reads_as_nothing_installed(env, tmp_path):
    assert protocol_description_store.read_all(tmp_path / "missing.db") == (0, [])
    assert protocol_description_store.generation(tmp_path / "missing.db") == 0
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    assert protocol_description_store.read_all(bare) == (0, [])
    assert protocol_description_store.generation(bare) == 0
    assert protocol_description_store.get("tiny", bare) is None


def _canary_documents():
    canary, fake_path = "CANARY-7f3a", "/etc/canary-7f3a/secret.yml"
    doc = _document()
    bad_pointer = _document()
    bad_pointer["operations"]["models"]["response"]["select"]["ids"] = f"{canary}/{fake_path}"
    bad_key = {**_document(), canary: fake_path}
    bad_impl = _document(implementation=canary)
    bad_header = _document()
    bad_header["operations"]["models"]["request"]["headers"] = {f"Authorization-{canary}": fake_path}
    bad_enum = _document(executor=f"{canary}{fake_path}")
    return {
        "yaml-syntax": f"operations: [{canary} {fake_path}",
        "yaml-tag": f"!!python/object:{canary} {{}}",
        "yaml-alias": f"a: &{canary} 1\nb: *{canary}\n",
        "not-a-mapping": f"- {canary}\n- {fake_path}\n",
        "pointer": yaml.safe_dump(bad_pointer),
        "extra-key": yaml.safe_dump(bad_key),
        "implementation": yaml.safe_dump(bad_impl),
        "header": yaml.safe_dump(bad_header),
        "enum": yaml.safe_dump(bad_enum),
        "long": yaml.safe_dump({**doc, "title": canary * 5000}),
        "int-key": yaml.safe_dump({**doc, 7: canary}),
    }


@pytest.mark.parametrize("kind", sorted(_canary_documents()))
def test_a_refusal_never_carries_posted_text_or_a_path(env, kind):
    client = _client()
    refused = _put(client, "tiny", _canary_documents()[kind], **{"If-None-Match": "*"})
    assert refused.status_code in (413, 422), refused.text
    for leaked in ("CANARY", "canary", "7f3a", "/etc", "secret.yml", str(env.db), str(env.seeds), str(env.overlay), "Traceback"):
        assert leaked not in refused.text
    error = refused.json()["error"]
    if refused.status_code == 422 and error["code"] == "invalid_description":
        assert error["message"] == "The description is not valid. Check it with the validator and send it again."
        assert error["problem_codes"] and error["problem_count"] >= 1
        assert set(error["problem_codes"].split(",")) <= {
            "yaml_alias", "yaml_syntax", "not_a_mapping", "code_field", "non_string_key", "unknown_implementation",
            "bad_pointer", "bad_header", "bad_path", "invalid_value"}
    assert _rows(env) == []


def test_a_registry_write_that_names_a_missing_description_is_refused(env):
    client = _client()
    _create(client)

    def write(protocol_id, generation):
        registry = {"services": {"box": {"enabled": False, "config": {"connection": {"dispatch": "protocol", "protocol": protocol_id}}}}}
        return admin_store.write_service_registry_with_audit(
            registry, expected_generation=generation, updated_by="t", audit_action="t", audit_actor_kind="t", audit_actor_id="t",
            audit_actor_label="t", audit_target_kind="t", audit_target_id="t", path=env.db)

    with pytest.raises(protocol_description_store.DescriptionRefused):
        write("ghost", 0)
    assert write("tiny", 0)["operator_settings_generation"] == 1
    # A reference the registry already holds does not stop an unrelated write.
    assert write("tiny", 1)["operator_settings_generation"] == 2
