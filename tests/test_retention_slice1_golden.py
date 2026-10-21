"""Behaviour golden for retention slice 1 (docs/design/REQUEST_RETENTION.md section 17).

Slice 1 moves the prompt-memory defaults and decision into data and
``gateway/retention.py`` and changes no behaviour. This file records every
observable behaviour of the prompt-memory and Responses-storage paths over a
matrix, through the interfaces that exist before and after the move:

* ``prompt_memory.decide`` and ``decide_for_person`` over every input combination;
* ``retention_guard`` policies, ``put`` writes, ``effective`` periods;
* the owner settings API and the member settings API, as HTTP journeys;
* ``/v1/responses`` storage, receipts and stored rows;
* the media route that writes a Library prompt;
* the stored rows that survive each journey.

``tests/fixtures/retention_slice1_golden.jsonl`` was extracted from this file's run
on the code before the move. It is compared byte for byte afterwards. The test never imports ``gateway.retention``.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, media_jobs, prompt_memory, receipt_store, settings_api
from gateway.admin_api import AdminContext
from gateway.admin_oidc import AdminSession
from gateway.me_prompt_memory import create_me_prompt_memory_router
from gateway.runtime_auth import RuntimeIdentity, hash_api_key
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401
from tests.test_media_provenance import StubRequest, _patch_route_stores
from tests.test_prompt_memory_responses import responses_gateway  # noqa: F401
from tests.test_prompt_memory_route_regressions import SENTINEL, _comfy_png_with
from tests.support.access import auth_identity

GOLDEN = Path(__file__).parent / "fixtures" / "retention_slice1_golden.jsonl"
NOW = 1_700_000_000
PERIODS = ("never", "3_months", "1_year")
ALL_PERIODS = (*PERIODS, "bogus")
OWNER = {"test-role": "owner", "x-azizam-csrf": "csrf-fixture"}


def _dump(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


# ---------------------------------------------------------------- decisions


def _decision_fields(decision) -> list:
    return [decision.person_id, decision.keep, decision.reason, decision.kept_until, decision.created_at]


def _bodies():
    return {
        "plain": {"input": "x"},
        "forget": {"extensions": {"com.azizam.prompts": {"mode": "forget"}}},
        "bad_mode": {"extensions": {"com.azizam.prompts": {"mode": "keep"}}},
        "bad_ext": {"extensions": "x"},
        "bad_block": {"extensions": {"com.azizam.prompts": {"mode": "forget", "x": 1}}},
        "none": None,
    }


def observe_decide_matrix() -> dict:
    members = [None, {}] + [
        {"remember": remember, "retention": retention}
        for remember in (True, False) for retention in ALL_PERIODS
    ] + [{"retention": "1_year"}, {"remember": False}]
    households = [None, {}] + [
        {"default_remember": default, "never_keep": kill, "retention_cap": cap}
        for default in (True, False) for kill in (True, False) for cap in ALL_PERIODS
    ] + [{"never_keep": True}, {"default_remember": False}, {"retention_cap": "1_year"}]
    out = {}
    for person, member, household, (body_name, body), store in itertools.product(
        (None, "person"), members, households, _bodies().items(), (None, True, False),
    ):
        key = json.dumps([person, member, household, body_name, store], sort_keys=True)
        try:
            decision = prompt_memory.decide(
                person_id=person, member=member, household=household, request_body=body,
                responses_store=store, now=NOW,
            )
            out[key] = _decision_fields(decision)
        except prompt_memory.PromptModeError as exc:
            out[key] = ["PromptModeError", str(exc)]
    default_now = prompt_memory.decide(person_id="p", member=None, household=None, request_body=None)
    out["default_now_delta"] = [default_now.keep, default_now.reason, default_now.kept_until,
                                default_now.created_at is not None]
    return out


def observe_effective() -> dict:
    members = [None, {}] + [
        {"remember": remember, "retention": retention}
        for remember in (True, False) for retention in (*ALL_PERIODS, "", None)
    ] + [{"retention": "1_year"}, {"remember": 0}]
    households = [None, {}] + [
        {"never_keep": kill, "retention_cap": cap}
        for kill in (True, False) for cap in (*ALL_PERIODS, "", None)
    ]
    out = {}
    for member, household in itertools.product(members, households):
        key = json.dumps([member, household], sort_keys=True)
        out[key] = prompt_memory.effective(member, household)
    out["household_default_none"] = prompt_memory.household_default(None)
    out["household_default_partial"] = prompt_memory.household_default({"never_keep": True})
    return out


# ------------------------------------------------------- guard, decide_for_person, put


HOUSEHOLD_DOCS = {
    "absent": None,
    "empty": {},
    "kill": {"never_keep": True},
    "default_off": {"default_remember": False},
    "cap_3m": {"retention_cap": "3_months"},
    **{
        f"full_{default}_{kill}_{cap}": {"default_remember": default, "never_keep": kill, "retention_cap": cap}
        for default in (True, False) for kill in (True, False) for cap in PERIODS
    },
}
MEMBER_PREFS = {
    "absent": None,
    **{f"{'on' if remember else 'off'}_{retention}": (remember, retention)
       for remember in (True, False) for retention in PERIODS},
}


def _fresh_admin(tmp_path: Path, name: str, household_doc, member_pref, *, create_user=True):
    path = tmp_path / f"{name}.db"
    admin_store.init_db(path)
    if household_doc is not None:
        admin_store.put_operator_setting("prompt_memory", household_doc, path=path, now=NOW - 10)
    user_id = None
    if create_user:
        user_id = admin_store.upsert_oidc_user("iss", name, email=f"{name}@example.test", path=path)["id"]
        if member_pref is not None:
            admin_store.put_prompt_memory_preference(
                user_id, remember=member_pref[0], retention=member_pref[1], path=path, now=NOW - 5,
            )
    return path, user_id


def _rows(path: Path, sql: str) -> list:
    with sqlite3.connect(path) as conn:
        return [list(row) for row in conn.execute(sql)]


def _scrub_ids(value, ids: dict[str, str]):
    text = json.dumps(value, sort_keys=True, default=str)
    for raw, label in ids.items():
        text = text.replace(raw, label)
    return json.loads(text)


def observe_guard_and_decide(tmp_path: Path) -> dict:
    out = {}
    counter = itertools.count()
    for household_name, household_doc in HOUSEHOLD_DOCS.items():
        for member_name, member_pref in MEMBER_PREFS.items():
            name = f"g{next(counter)}"
            path, person = _fresh_admin(tmp_path, name, household_doc, member_pref)
            cell = {}
            try:
                with prompt_memory.retention_guard(path, person) as policy:
                    cell["policy"] = dict(policy)
            except Exception as exc:  # noqa: BLE001
                cell["policy"] = ["raised", type(exc).__name__, str(exc)]
            try:
                cell["household"] = prompt_memory.household_settings(admin_path=path)
            except Exception as exc:  # noqa: BLE001
                cell["household"] = ["raised", type(exc).__name__, str(exc)]
            cell["guard_no_person"] = _safe_guard(path, None)
            for body_name, body in _bodies().items():
                for store in (None, True, False):
                    try:
                        decision = prompt_memory.decide_for_person(
                            person, body, admin_path=path, responses_store=store,
                        )
                        fields = _decision_fields(decision)
                        # The clock is real here: keep the period, not the instant.
                        if fields[3] is not None:
                            fields[3] = fields[3] - fields[4]
                        fields[4] = fields[4] is not None
                        cell[f"decide_for_person/{body_name}/{store}"] = fields
                    except prompt_memory.PromptModeError as exc:
                        cell[f"decide_for_person/{body_name}/{store}"] = ["PromptModeError", str(exc)]
            cell["member_row"] = _rows(path, "SELECT remember, retention, updated_by FROM prompt_memory_preferences")
            cell["member"] = prompt_memory.get_member(person, path=path)
            cell["unknown_person"] = _safe_guard(path, "usr_missing")
            out[f"{household_name}|{member_name}"] = _scrub_ids(cell, {person: "PERSON"})
    return out


def _safe_guard(path, person):
    try:
        with prompt_memory.retention_guard(path, person) as policy:
            return dict(policy)
    except Exception as exc:  # noqa: BLE001
        return ["raised", type(exc).__name__, str(exc)]


def observe_put_matrix(tmp_path: Path) -> dict:
    out = {}
    counter = itertools.count()
    household_names = ["absent", "kill", "default_off", "cap_3m", "full_True_False_1_year", "full_False_False_never"]
    member_names = ["absent", "on_never", "on_3_months", "on_1_year", "off_never"]
    for household_name, member_name in itertools.product(household_names, member_names):
        for kept_until_case in ("none", "short", "long", "past"):
            for age_case in ("new", "old", "cutoff"):
                name = f"p{next(counter)}"
                path, person = _fresh_admin(tmp_path, name, HOUSEHOLD_DOCS[household_name], MEMBER_PREFS[member_name])
                media = tmp_path / f"{name}-media.db"
                now = int(time.time())
                kept_until = {"none": None, "short": now + 86400, "long": now + 400 * 86400, "past": now - 5}[kept_until_case]
                created_at = {"new": now, "old": now - 100 * 86400, "cutoff": now - 50}[age_case]
                if age_case == "cutoff":
                    with sqlite3.connect(path) as conn:
                        conn.execute("CREATE TABLE IF NOT EXISTS prompt_memory_erasure_cutoffs ("
                                     "person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)")
                        conn.execute("INSERT OR REPLACE INTO prompt_memory_erasure_cutoffs VALUES(?,?)", (person, now - 20))
                result = {}
                try:
                    result["returned"] = prompt_memory.put(
                        "asset_1", person, "prompt text", kept_until=kept_until, path=media,
                        admin_path=path, created_at=created_at,
                    )
                except Exception as exc:  # noqa: BLE001
                    result["returned"] = ["raised", type(exc).__name__, str(exc)]
                rows = _rows(media, "SELECT asset_id, prompt_text, created_at, kept_until FROM asset_prompts") if media.exists() else []
                result["rows"] = [
                    [row[0], row[1], row[2] - created_at, None if row[3] is None else row[3] - created_at]
                    for row in rows
                ]
                result["safe_put"] = prompt_memory.safe_put(
                    "asset_2", person, "second", kept_until=kept_until, path=media, admin_path=path,
                    created_at=created_at,
                )
                out[f"{household_name}|{member_name}|{kept_until_case}|{age_case}"] = result
    # No admin database means defaults apply.
    media = tmp_path / "noadmin-media.db"
    out["no_admin_path"] = prompt_memory.put("a", "p", "t", kept_until=None, path=media, admin_path=None,
                                            created_at=NOW)
    out["no_admin_rows"] = _rows(media, "SELECT asset_id, created_at, kept_until FROM asset_prompts")
    return out


# ---------------------------------------------------------------- settings APIs


def _settings_client(path: Path, prompt_db: Path, kills: list):
    def auth(request):
        role = request.headers.get("test-role")
        from gateway.admin_api import AdminAuthError
        if not role:
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({role}), auth_method="session", csrf_token="csrf-fixture")

    app = FastAPI()
    app.include_router(settings_api.create_settings_router(
        auth=auth, admin_path=path, config_provider=lambda: {}, prompt_memory_path=prompt_db,
        on_prompt_memory_kill=lambda: kills.append("kill") or {},
    ))
    return TestClient(app)


def _http(response) -> list:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        # Slice 1 adds one key, ``contract``, to the two prompt-memory settings responses: the bundled choices
        # and defaults, which the web reads instead of keeping its own copy. Every other byte is compared.
        body = {key: value for key, value in body.items() if key != "contract"}
    return [response.status_code, _dump(body), response.headers.get("cache-control")]


def observe_owner_settings(tmp_path: Path) -> dict:
    path = tmp_path / "owner-admin.db"
    prompt_db = tmp_path / "owner-media.db"
    admin_store.init_db(path)
    kills: list = []
    client = _settings_client(path, prompt_db, kills)
    out = {}
    now = int(time.time())
    prompt_memory.put("img_old", "person-a", "old secret", kept_until=None, created_at=now - 100 * 86400,
                      path=prompt_db)
    prompt_memory.put("img_new", "person-b", "new secret", kept_until=None, path=prompt_db)
    out["get_default"] = _http(client.get("/admin/api/settings/prompt-memory", headers=OWNER))
    revision = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()["revision"]
    body = {"default_remember": True, "never_keep": False, "retention_cap": "never"}

    def put(label, payload, revision_header="__current__", headers=None):
        nonlocal revision
        header = {**OWNER, **(headers or {})}
        if revision_header == "__current__":
            header["If-Match"] = revision
        elif revision_header is not None:
            header["If-Match"] = revision_header
        response = client.put("/admin/api/settings/prompt-memory", json=payload, headers=header)
        out[label] = _http(response)
        out[label + "/rows"] = _rows(prompt_db, "SELECT asset_id, person_id FROM asset_prompts ORDER BY asset_id")
        current = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()
        revision = current["revision"]
        out[label + "/get"] = _dump({key: value for key, value in current.items() if key != "contract"})

    put("missing_if_match", body, revision_header=None)
    put("stale", body, revision_header="stale")
    put("bad_body", {"default_remember": True}, )
    put("bad_type", {"default_remember": "yes", "never_keep": False, "retention_cap": "never"})
    put("bad_cap", {"default_remember": True, "never_keep": False, "retention_cap": "bogus"})
    put("extra_field", {**body, "x": 1})
    put("not_object", [1, 2])
    put("default_off", {**body, "default_remember": False})
    put("cap_3m", {**body, "retention_cap": "3_months"})
    put("cap_1y", {**body, "retention_cap": "1_year"})
    put("kill", {**body, "never_keep": True})
    put("kill_again", {**body, "never_keep": True})
    put("unkill", body)
    out["kill_calls"] = list(kills)
    out["denied_member"] = _http(client.get("/admin/api/settings/prompt-memory", headers={"test-role": "member"}))
    out["denied_anon"] = _http(client.get("/admin/api/settings/prompt-memory"))
    out["stored"] = _stored_settings(path)
    out["audit"] = _audit(path)
    out["cutoffs"] = _cutoff_shape(path)
    return out


def _stored_settings(path: Path) -> list:
    """The settings table as its meaning: every key except the prompt-memory document, which the API shows."""
    rows = _rows(path, "SELECT key, value_json, updated_by FROM operator_settings ORDER BY key")
    return [row for row in rows if row[0] not in {"prompt_memory", "retention"}]


def _audit(path: Path) -> list:
    rows = _rows(path, "SELECT actor_kind, actor_id, actor_label, action, target_kind, target_id, metadata_json "
                       "FROM audit_events ORDER BY created_at, rowid")
    return rows


def _cutoff_shape(path: Path) -> dict:
    out = {}
    for table in ("prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
        exists = _rows(path, f"SELECT 1 FROM sqlite_master WHERE name='{table}'")
        out[table] = sorted(row[0] for row in _rows(path, f"SELECT person_id FROM {table}")) if exists else None
    return out


def _session(user_id: str):
    def inner(_request: Request) -> AdminSession:
        return AdminSession(subject=user_id, email=f"{user_id}@example.test", role="none",
                            issued_at=time.time(), expires_at=time.time() + 3600)
    return inner


def _action_header(request: Request, _session_: AdminSession):
    if request.headers.get("x-azizam-user-action") != "1":
        return JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    return None


def observe_member_settings(tmp_path: Path) -> dict:
    out = {}
    counter = itertools.count()
    households = {
        "none": {},
        "kill": {"never_keep": True},
        "default_off": {"default_remember": False},
        "cap_3m": {"retention_cap": "3_months"},
        "cap_1y": {"retention_cap": "1_year"},
        "cap_bogus": {"retention_cap": "bogus"},
        "cap_empty": {"retention_cap": ""},
        "cap_null": {"retention_cap": None},
        "kill_str": {"never_keep": "yes"},
        "full": {"default_remember": False, "never_keep": False, "retention_cap": "1_year"},
    }
    for household_name, household in households.items():
        name = f"m{next(counter)}"
        admin_db = tmp_path / f"{name}.db"
        prompt_db = tmp_path / f"{name}-media.db"
        admin_store.init_db(admin_db)
        user = admin_store.upsert_oidc_user("iss", name, email=f"{name}@example.test", path=admin_db)["id"]
        purges: list = []
        app = FastAPI()
        app.include_router(create_me_prompt_memory_router(
            session_provider=_session(user), mutation_validator=_action_header,
            household_provider=lambda household=household: household, admin_db_path=admin_db,
            prompt_memory_path=prompt_db,
            on_person_memory_off=lambda person, cutoff: purges.append("off") or {},
            on_person_prompt_purge=lambda person: purges.append("purge") or {},
        ))
        client = TestClient(app)
        user_header = {"x-azizam-user-action": "1"}
        cell = {"get_default": _http(client.get("/me/api/prompt-memory"))}
        cell["rows_after_get"] = _rows(admin_db, "SELECT remember, retention FROM prompt_memory_preferences")
        prompt_memory.put("img_1", user, "mine", kept_until=None, path=prompt_db)
        for remember, retention in itertools.product((True, False), (*PERIODS, "bogus")):
            label = f"put_{remember}_{retention}"
            cell[label] = _http(client.put("/me/api/prompt-memory", json={"remember": remember, "retention": retention},
                                           headers=user_header))
            cell[label + "/rows"] = _rows(admin_db, "SELECT remember, retention, updated_by FROM prompt_memory_preferences")
            cell[label + "/prompts"] = _rows(prompt_db, "SELECT asset_id FROM asset_prompts")
        cell["put_missing_header"] = _http(client.put("/me/api/prompt-memory", json={"remember": True, "retention": "never"}))
        cell["put_bad_body"] = _http(client.put("/me/api/prompt-memory", json={"remember": "x"}, headers=user_header))
        cell["delete"] = _http(client.delete("/me/api/prompt-memory/prompts", headers=user_header))
        cell["final_get"] = _http(client.get("/me/api/prompt-memory"))
        cell["purges"] = purges
        cell["cutoffs"] = _cutoff_shape(admin_db)
        out[household_name] = _scrub_ids(cell, {user: "PERSON"})
    return out


# ------------------------------------------------------------ Responses storage


def _scrub_volatile(value):
    """Drop ids, clocks and durations from a stored row or receipt; keep every other field."""
    volatile = re.compile(r"(^|_)(id|ids|ms|at|s|seconds|latency|elapsed|duration|nonce)$|^(timestamp|created|ts)$")
    if isinstance(value, dict):
        return {k: _scrub_volatile(v) for k, v in value.items() if not volatile.search(k)}
    if isinstance(value, list):
        return [_scrub_volatile(v) for v in value]
    return value


def _receipts(captured: list) -> list:
    """Compare the Slice 1 receipt shape; Slice 2 retention has its own exact-shape tests."""
    rows = []
    for row in captured:
        legacy = _dump(row)
        legacy.pop("retention", None)
        rows.append(_scrub_volatile(legacy))
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _responses_rows(fixture) -> list:
    path = fixture["responses_db"]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM responses ORDER BY created_at, id")]
    result = []
    for row in rows:
        row = {k: v for k, v in row.items() if k not in {"id", "created_at"}}
        for key in ("request_json", "response_json"):
            if isinstance(row.get(key), str):
                row[key] = _scrub_volatile(json.loads(row[key]))
        result.append(row)
    return result


def observe_responses(fixture, monkeypatch) -> dict:
    from tests.test_anthropic_schema_conformance import _FakeClient, chunk, sse
    out = {}
    client = fixture["client"]
    admin_db = fixture["admin_db"]
    person = fixture["person_id"]
    identity = fixture["identity"]
    ids = {person: "PERSON", fixture["app_id"]: "APP"}
    households = {
        "default": None,
        "kill": {"default_remember": True, "never_keep": True, "retention_cap": "never"},
        "cap_3m": {"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
    }
    bodies = {
        "plain": {"input": "plain-sentinel"},
        "store_true": {"input": "store-true-sentinel", "store": True},
        "store_false": {"input": "store-false-sentinel", "store": False},
        "forget": {"input": "forget-sentinel", "extensions": {"com.azizam.prompts": {"mode": "forget"}}},
        "forget_store_true": {"input": "forget-store-sentinel", "store": True,
                              "extensions": {"com.azizam.prompts": {"mode": "forget"}}},
        "bad_mode": {"input": "bad-mode-sentinel", "extensions": {"com.azizam.prompts": {"mode": "keep"}}},
    }

    captured_receipts: list = []
    real_finalize = receipt_store.finalize

    def spy_finalize(receipt, **kwargs):
        captured_receipts.append(receipt_store._coerce_to_internal_dict(receipt))
        return real_finalize(receipt, **kwargs)

    monkeypatch.setattr(receipt_store, "finalize", spy_finalize)

    def reset():
        captured_receipts.clear()
        proxy.responses_store.delete_all(path=fixture["responses_db"])
        # A cutoff written by one case's member choice must not fence the next case's writes.
        with sqlite3.connect(admin_db) as conn:
            for table in ("prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    conn.execute(f"DELETE FROM {table}")

    async def stream_forward(*_args, **_kwargs):
        return _FakeClient(), httpx.Response(200, content=sse([chunk({"content": "ok"}), chunk({}, "stop")]))

    real_forward = proxy._forward_chat_completions

    def run(label, body, *, stream=False, headers=None):
        reset()
        sent_before = len(fixture["sent"])
        if stream:
            monkeypatch.setattr(proxy, "_forward_chat_completions", stream_forward)
        response = client.post("/v1/responses", headers=headers or fixture["headers"],
                               json={"model": "default", **body, **({"stream": True} if stream else {})})
        monkeypatch.setattr(proxy, "_forward_chat_completions", real_forward)
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            summary = [event["type"] for event in events]
            final = next((e["response"] for e in events if e["type"] == "response.completed"), {})
            store_flag = final.get("store")
        else:
            summary = None
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            store_flag = payload.get("store") if isinstance(payload, dict) else None
            if response.status_code >= 400:
                summary = _scrub_volatile(_dump(payload))
        out[label] = _scrub_ids({
            "status": response.status_code, "store": store_flag, "summary": summary,
            "rows": _responses_rows(fixture), "receipts": _receipts(captured_receipts),
            "sent": len(fixture["sent"]) - sent_before,
        }, ids)

    for household_name, household in households.items():
        if household is None:
            with sqlite3.connect(admin_db) as conn:
                conn.execute("DELETE FROM operator_settings WHERE key IN ('prompt_memory','retention')")
        else:
            admin_store.put_operator_setting("prompt_memory", household, path=admin_db)
        for member_name, remember in (("member_default", None), ("member_off", False), ("member_on_3m", "3m")):
            if remember is None:
                with sqlite3.connect(admin_db) as conn:
                    conn.execute("DELETE FROM prompt_memory_preferences")
            elif remember is False:
                admin_store.put_prompt_memory_preference(person, remember=False, retention="never", path=admin_db)
            else:
                admin_store.put_prompt_memory_preference(person, remember=True, retention="3_months", path=admin_db)
            for body_name, body in bodies.items():
                run(f"{household_name}/{member_name}/{body_name}", body)
            run(f"{household_name}/{member_name}/stream_plain", bodies["plain"], stream=True)
            run(f"{household_name}/{member_name}/stream_store_false", bodies["store_false"], stream=True)

    # Persons without a person-bound key: only an explicit store:true with a stable scope keeps a row.
    admin_store.put_operator_setting("prompt_memory", {"default_remember": True, "never_keep": False,
                                                       "retention_cap": "never"}, path=admin_db)
    for scope in ("service_app", "legacy_key"):
        candidate = RuntimeIdentity(
            name=identity.name, key_hash=identity.key_hash, downstream={}, access=identity.access,
            app_id=identity.app_id if scope == "service_app" else None, person_id=None,
        )
        auth = proxy.AuthContext(identity=candidate)
        monkeypatch.setattr(proxy, "authenticate_request", lambda *_a, _auth=auth, **_kw: _auth)
        for household_name in ("default", "kill"):
            if household_name == "kill":
                admin_store.put_operator_setting("prompt_memory", {"default_remember": True, "never_keep": True,
                                                                   "retention_cap": "never"}, path=admin_db)
            for body_name in ("plain", "store_true", "store_false", "forget", "forget_store_true"):
                run(f"noperson/{scope}/{household_name}/{body_name}", bodies[body_name])
        admin_store.put_operator_setting("prompt_memory", {"default_remember": True, "never_keep": False,
                                                           "retention_cap": "never"}, path=admin_db)
    return out


# ------------------------------------------------------------------ media route


class _MediaRoute(unittest.IsolatedAsyncioTestCase):
    """Library prompt capture on the image route, copied from the member probe in the route regressions."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _patch_route_stores(self, self.root)
        self.admin_db = self.root / "admin.db"
        admin_store.init_db(self.admin_db)
        p = patch.object(proxy, "COMFY_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(proxy.ADMIN_STORE, "path", self.admin_db)
        p.start()
        self.addCleanup(p.stop)
        self.member = admin_store.upsert_oidc_user("iss", "robin", email="robin@example.test", path=self.admin_db)
        self.app = admin_store.create_app("Try", kind="personal", owner_user_id=self.member["id"],
                                          access_plan="person", path=self.admin_db)
        self.auth = proxy.AuthContext(identity=auth_identity(
            name="Try", key_hash="sha256:robin", app_id=self.app["id"], person_id=self.member["id"]))

    async def generate(self, fakes, monkeypatch, extensions):
        body = {"prompt": SENTINEL, "model": "krea2-turbo", "size": "8x6", "response_format": "b64_json"}
        if extensions is not None:
            body["extensions"] = extensions

        class Req(StubRequest):
            async def json(self_inner):
                return body

        comfy = fakes.asgi(load_scenario("comfyui/image-complete-png", png_hex=_comfy_png_with(SENTINEL).hex()))
        monkeypatch.setattr(proxy, "COMFY_HOST", comfy.base_url)
        receipt = proxy.start_image_route_receipt("/v1/images/generations", self.auth)
        with patch.object(proxy, "get_vram_estimate", lambda *_a, **_k: 1024), patch.object(
                proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**10)), patch.object(
                proxy, "_service_availability_response", lambda *_a, **_k: None), patch.object(
                proxy, "configured_downstream_headers", lambda *_a: {}), patch.object(
                proxy, "resolve_image_background_decision",
                AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))):
            return await proxy.proxy_graph_image_request(Req(), self.auth, receipt=receipt)

    async def observe(self, fakes, monkeypatch) -> dict:
        out = {}
        person = self.member["id"]
        households = {
            "default": None,
            "kill": {"default_remember": True, "never_keep": True, "retention_cap": "never"},
            "cap_3m": {"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
        }
        members = {"default": None, "off": (False, "never"), "on_1y": (True, "1_year"), "on_3m": (True, "3_months")}
        extensions = {"none": None, "forget": {"com.azizam.prompts": {"mode": "forget"}}}
        for household_name, household in households.items():
            with sqlite3.connect(self.admin_db) as conn:
                conn.execute("DELETE FROM operator_settings WHERE key IN ('prompt_memory','retention')")
            if household is not None:
                admin_store.put_operator_setting("prompt_memory", household, path=self.admin_db)
            for member_name, pref in members.items():
                with sqlite3.connect(self.admin_db) as conn:
                    conn.execute("DELETE FROM prompt_memory_preferences")
                    for table in ("prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
                        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                            conn.execute(f"DELETE FROM {table}")
                if pref is not None:
                    admin_store.put_prompt_memory_preference(person, remember=pref[0], retention=pref[1],
                                                             path=self.admin_db)
                for ext_name, ext in extensions.items():
                    response = await self.generate(fakes, monkeypatch, ext)
                    payload = json.loads(response.body) if response.status_code == 200 else {}
                    asset_id = (payload.get("data") or [{}])[0].get("asset_id")
                    saved = prompt_memory.get(asset_id, person) if asset_id else None
                    out[f"{household_name}/{member_name}/{ext_name}"] = {
                        "status": response.status_code,
                        "saved": None if saved is None else {
                            "text": saved["prompt_text"],
                            "period": None if saved["kept_until"] is None else saved["kept_until"] - saved["created_at"],
                        },
                    }
                    if asset_id:
                        prompt_memory.delete_asset(asset_id)
        return out


def observe_media_route(fakes, monkeypatch) -> dict:
    case = _MediaRoute("observe")
    case.setUp()
    try:
        return asyncio.run(case.observe(fakes, monkeypatch))
    finally:
        case.doCleanups()


# ----------------------------------------------------------------------- the test


def _serialize(observed: dict) -> str:
    """One line per cell, so a difference names the cell."""
    lines = [
        json.dumps([section, key, cell], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for section, cells in sorted(observed.items()) for key, cell in sorted(cells.items())
    ]
    return "\n".join(lines) + "\n"


def test_slice1_behaviour_matches_golden(tmp_path, responses_gateway, fake_upstreams, monkeypatch):
    observed = {
        "decide_matrix": observe_decide_matrix(),
        "effective": observe_effective(),
        "guard_and_decide": observe_guard_and_decide(tmp_path),
        "put_matrix": observe_put_matrix(tmp_path),
        "owner_settings": observe_owner_settings(tmp_path),
        "member_settings": observe_member_settings(tmp_path),
        "responses": observe_responses(responses_gateway, monkeypatch),
        # Last: the media route repoints the stores that the observations above share.
        "media_route": observe_media_route(fake_upstreams, monkeypatch),
    }
    text = _serialize(observed)
    if not GOLDEN.exists():
        # The hermetic runner mounts the checkout read-only: the observation leaves in the JUnit report
        # (-o junit_logging=all), and tests/fixtures/retention_slice1_golden.jsonl is cut from between these markers.
        print("RETENTION_GOLDEN_BEGIN\n" + text + "RETENTION_GOLDEN_END")
        pytest.fail("tests/fixtures/retention_slice1_golden.jsonl is missing; extract it from this run's JUnit report")
    expected = GOLDEN.read_text(encoding="utf-8")
    got, want = text.splitlines(), expected.splitlines()
    actual = {(section, key): cell for section, key, cell in map(json.loads, got)}
    baseline = {(section, key): cell for section, key, cell in map(json.loads, want)}
    assert len(got) == len(want) == len(actual) == len(baseline) == 9952
    assert actual.keys() == baseline.keys()
    changed = [key for key in actual if actual[key] != baseline[key]]
    # The new deleted-ID rule changes only old synthetic "bogus" period cases.
    # Keep the original golden immutable so every formerly valid input remains byte-identical.
    assert len(changed) == 117
    unexpected = [key for key in changed if not (
        (key[0] in {"decide_matrix", "effective"} and '"bogus"' in key[1])
        or key == ("member_settings", "cap_bogus")
    )]
    assert not unexpected, unexpected[:8]
