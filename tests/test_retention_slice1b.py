"""Retention slice 1b: the four follow-ups to slice 1 (docs/design/REQUEST_RETENTION.md, "Slice 1 as built").

1. A stored period ID keeps its exact meaning while it is in ``retention_choices``, offered or not; a deleted ID
   uses the shortest finite offered choice; startup, maintenance and writes share one normalization.
2. (web) The account note tells an unoffered ID from a deleted ID and names an owner cap as the cause.
3. A file that offers only ``never`` is valid unless a stored ID was deleted and no finite choice can replace it.
4. A failed startup deadline repair shows as pending in the owner's prompt status until a retry succeeds.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest
import yaml

from gateway import admin_store, media_jobs, prompt_memory, retention

SCHEMA = json.loads(retention.SCHEMA_PATH.read_text(encoding="utf-8"))
DAY = 86400


def _data() -> dict:
    return yaml.safe_load(retention.BUNDLED_PATH.read_text(encoding="utf-8"))


def _use(monkeypatch, data) -> retention.Contract:
    contract = retention.parse_contract(data, schema=SCHEMA)
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    return contract


def _kept_days(media_path, person) -> list:
    with sqlite3.connect(media_path) as conn:
        return [None if kept is None else round((kept - created) / DAY) for created, kept in conn.execute(
            "SELECT created_at, kept_until FROM asset_prompts WHERE person_id=? ORDER BY asset_id", (person,))]


def test_unoffered_never_keeps_its_meaning_in_startup_maintenance_and_writes(tmp_path, monkeypatch):
    import proxy
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "never-unoffered", path=admin_path)["id"]
    prompt_memory._connect(media_path).close()
    created = int(time.time()) - DAY
    with sqlite3.connect(media_path) as conn:
        for asset, days in (("a-open", None), ("b-ten", 10)):
            conn.execute("INSERT INTO asset_prompts VALUES(?,?,?,?,?,?)", (asset, person, "p", None, created,
                         None if days is None else created + days * DAY))
    with sqlite3.connect(admin_path) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES('retention',?,1)",
                     (json.dumps({"groups": {"prompts": {"retention_cap": "never"}}}),))
    data = _data()
    data["groups"]["prompts"].update(choices=["1_year", "3_months"], default_retention="1_year", default_cap="1_year")
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention='never' WHERE user_id=? AND grp='prompts'", (person,))
    _use(monkeypatch, data)
    assert retention.effective_period("never", "never") == "never"
    prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path)
    assert _kept_days(media_path, person) == [None, 10]
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_path)
    monkeypatch.setattr(media_jobs, "DB_PATH", media_path)
    proxy._maintain_household_prompt_memory()
    proxy._maintain_person_prompt_memory(person)
    assert _kept_days(media_path, person) == [None, 10]
    decision = prompt_memory.decide_for_person(person, None, admin_path=admin_path)
    assert decision.kept_until is None
    assert prompt_memory.put("c-new", person, "p", kept_until=decision.kept_until, created_at=decision.created_at,
                             path=media_path, admin_path=admin_path)
    assert _kept_days(media_path, person)[-1] is None


def test_a_file_that_offers_only_never_is_valid_until_a_stored_id_is_deleted(tmp_path, monkeypatch):
    data = _data()
    data["groups"]["prompts"].update(choices=["never"], default_retention="never", default_cap="never")
    contract = _use(monkeypatch, data)
    assert contract.choices(retention.LIBRARY_PROMPT) == ["never"]
    admin_path = tmp_path / "admin.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "only-never", path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention='1_year' WHERE user_id=? AND grp='prompts'", (person,))
    prompt_memory.require_resolvable_periods(admin_path=admin_path)  # 1_year is still in the vocabulary
    del data["retention_choices"]["1_year"]
    _use(monkeypatch, data)
    with pytest.raises(retention.RetentionDataError, match="1_year.*no finite"):
        prompt_memory.require_resolvable_periods(admin_path=admin_path)
    with pytest.raises(retention.RetentionDataError, match="1_year.*no finite"):
        retention.normalize_period("1_year", kind=retention.LIBRARY_PROMPT, owner_cap=False)


def test_transient_startup_database_contention_does_not_invalidate_the_contract(tmp_path, monkeypatch):
    def busy(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(prompt_memory, "connect_readonly", busy)
    prompt_memory.require_resolvable_periods(admin_path=tmp_path / "busy.db")


def test_startup_repair_failure_is_pending_in_owner_status_until_a_retry_succeeds(tmp_path, monkeypatch):
    import proxy
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    prompt_memory._connect(media_path).close()
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_path)
    monkeypatch.setattr(media_jobs, "DB_PATH", media_path)
    monkeypatch.setattr(proxy, "_PROMPT_DEADLINE_REPAIR_PENDING", False, raising=False)
    assert proxy._prompt_purge_status("*")["pending"] is False
    real_to_thread = proxy.asyncio.to_thread

    async def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic busy database")

    monkeypatch.setattr(proxy.asyncio, "to_thread", unavailable)
    asyncio.run(proxy._repair_prompt_deadlines_at_startup())
    monkeypatch.setattr(proxy.asyncio, "to_thread", real_to_thread)
    status = proxy._prompt_purge_status("*")
    assert status["pending"] is True and status["deadline_repair_pending"] is True
    assert "deadline_repair_pending" not in proxy._prompt_purge_status("someone")
    monkeypatch.setattr(proxy.admin_store, "list_users", lambda **_kw: [])
    for name in ("_maintain_household_prompt_memory", "_drain_prompt_purges", "_prune_orphaned_media_job_payloads"):
        monkeypatch.setattr(proxy, name, lambda: None)
    monkeypatch.setattr(proxy.prompt_memory, "prune", lambda: None)
    proxy._maintain_prompt_memory()
    assert proxy._prompt_purge_status("*")["pending"] is False
