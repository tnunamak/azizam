"""One-time backfill of the maker stamp on media jobs written before schema 6 (OV3 slice 1).

Schema 6 gave each media job three stamped fields: ``maker_person_id``, ``maker_credential_id`` and
``maker_connection_id``. Reads authorize from those alone. A job written earlier has none of the three keys in its
JSON, so without this backfill every existing person would lose their Library after the upgrade.

The backfill runs at start-up, after the stores are initialized and before the gateway serves. It reads three
stores together: the job rows, the receipts that describe them, and the admin store that names each App's owner.

Evidence, in order, for each legacy job. Nothing else is read: no parent link, no client-supplied field, and no
owner of a service App.

- Person, from the job's own receipt. The receipt must share the job's request id and name this job's id, so a
  reused request id cannot lend another request's maker. The receipt's request-time maker is the person.
- Person, from the owner the job was stored under (``owner_key_hash``, as written at creation):
  a user principal (a session job with no App) names that user, whom the server recorded at authentication; a session
  App (a signed-in person's own work) names its person; a personal App names its owner, the fallback D51 keeps for
  personal Apps. Any other owner, including a service App, a service-account principal, or an unknown key, names no
  person.
- Credential: the stored owner key, unless it names a user or service account. A legacy job keeps no key id, so for
  an App-keyed job this is the App principal, and for an App-less legacy key it is the key hash.

A job with neither a person nor a credential stays unstamped, and no one reads it. Each legacy job gets the three
keys written (null when nothing resolved), so a later pass skips it. A finished pass is recorded in
``media_jobs_migrations``. The log counts the evidence used; no per-row column records it.

Version 2 exists because version 1 named no person for a user principal and was already recorded as finished on some
instances. Its rows keep the three keys, null, so the legacy selection skips them. Version 2 therefore also selects a
user-principal job with no maker stamp at all, and a second run finds none left.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from gateway import media_jobs, sqlite_startup

# v2: v1 named no person for a user principal and still recorded itself as finished, so v2 runs again and re-checks those.
MIGRATION_NAME = "maker_stamp_backfill_v2"
BATCH_SIZE = 500
_NO_CREDENTIAL_PREFIXES = ("user:", "service_account:")
# A job the pass still has to stamp: a legacy row with no maker keys, or a user-principal row with no maker stamp at all.
# A row that carries a person, credential or connection is a stamp the current code wrote, so it is left alone.
_UNSTAMPED = ("(json_type(job_json, '$.maker_person_id') IS NULL OR (json_extract(job_json, '$.maker_person_id') IS NULL "
              "AND json_extract(job_json, '$.maker_credential_id') IS NULL AND json_extract(job_json, '$.maker_connection_id') IS NULL "
              "AND json_extract(job_json, '$.owner_key_hash') GLOB 'user:*'))")
_MIGRATIONS_DDL = ("CREATE TABLE IF NOT EXISTS media_jobs_migrations ("
                   "name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _load_owners(admin_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The session Apps (App id -> person) and the personal Apps (App id -> owner) of the admin store."""
    if not Path(admin_path).exists():
        return {}, {}
    with closing(sqlite_startup.connect_readonly(admin_path)) as conn:
        session_apps = {str(app_id): str(user_id) for app_id, user_id in conn.execute(
            "SELECT app_id, user_id FROM session_apps")}
        personal_apps = {str(app_id): str(owner) for app_id, owner in conn.execute(
            "SELECT id, owner_user_id FROM apps WHERE kind = 'personal' AND owner_user_id IS NOT NULL "
            "AND owner_user_id != ''")}
    return session_apps, personal_apps


def _receipt_makers(receipts_path: Path, request_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Request id -> (job id, request-time maker) for each receipt that exists."""
    if not request_ids or not Path(receipts_path).exists():
        return {}
    found: dict[str, tuple[str | None, str | None]] = {}
    with closing(sqlite_startup.connect_readonly(receipts_path)) as conn:
        for start in range(0, len(request_ids), BATCH_SIZE):
            chunk = request_ids[start:start + BATCH_SIZE]
            marks = ",".join("?" for _ in chunk)
            for request_id, job_id, maker in conn.execute(
                f"SELECT request_id, job_id, json_extract(receipt_json, '$.maker_person_id') "
                f"FROM receipts WHERE request_id IN ({marks})", chunk,
            ):
                found[str(request_id)] = (_text(job_id), _text(maker))
    return found


def _resolve(
    job_id: str, request_id: str | None, data: dict[str, Any], receipts: dict[str, tuple[str | None, str | None]],
    owners: tuple[dict[str, str], dict[str, str]],
) -> tuple[str | None, str | None, str]:
    """The (person, credential, evidence) for one legacy job. Evidence names the source that gave the person."""
    session_apps, personal_apps = owners
    owner = _text(data.get("owner_key_hash"))
    credential = None if owner is None or owner.startswith(_NO_CREDENTIAL_PREFIXES) else owner
    receipt_job, receipt_maker = receipts.get(request_id or "", (None, None))
    if receipt_maker and receipt_job == job_id:
        return receipt_maker, credential, "receipt"
    user_id = owner[len("user:"):] if owner and owner.startswith("user:") else ""
    if user_id:
        return user_id, credential, "user_owner"
    if owner and owner.startswith("app:"):
        app_id = owner[len("app:"):]
        if app_id in session_apps:
            return session_apps[app_id], credential, "session_app"
        if app_id in personal_apps:
            return personal_apps[app_id], credential, "personal_app"
    return None, credential, "credential_only" if credential else "unresolved"


def _already_applied(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM media_jobs_migrations WHERE name = ?", (MIGRATION_NAME,)).fetchone() is not None


def backfill(
    path: Path = media_jobs.DB_PATH, *, receipts_path: Path, admin_path: Path,
) -> dict[str, int] | None:
    """Stamp each job in ``_UNSTAMPED`` once. Returns the count per evidence source, or None when this pass already ran."""
    with media_jobs._conn(path) as conn:
        conn.execute(_MIGRATIONS_DDL)
        conn.commit()
        if _already_applied(conn):
            return None
    owners = _load_owners(admin_path)
    counts: Counter[str] = Counter()
    last_job_id = ""
    while True:
        with media_jobs._conn(path) as conn:
            rows = conn.execute(
                f"SELECT job_id, request_id, job_json FROM media_jobs WHERE job_id > ? AND {_UNSTAMPED} "
                "ORDER BY job_id LIMIT ?",
                (last_job_id, BATCH_SIZE),
            ).fetchall()
        if not rows:
            break
        last_job_id = str(rows[-1][0])
        receipts = _receipt_makers(receipts_path, [str(row[1]) for row in rows if row[1]])
        with media_jobs._conn(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for job_id, request_id, job_json in rows:
                try:
                    data = json.loads(job_json)
                except (TypeError, ValueError):
                    data = None
                if not isinstance(data, dict):
                    counts["unreadable"] += 1
                    continue
                person, credential, evidence = _resolve(str(job_id), _text(request_id), data, receipts, owners)
                data.update(maker_person_id=person, maker_credential_id=credential, maker_connection_id=None)
                conn.execute(
                    "UPDATE media_jobs SET maker_person_id = ?, maker_credential_id = ?, maker_connection_id = NULL, "
                    f"job_json = ? WHERE job_id = ? AND {_UNSTAMPED}",
                    (person, credential, json.dumps(data, ensure_ascii=False, sort_keys=True), job_id),
                )
                counts[evidence] += 1
            conn.commit()
    with media_jobs._conn(path) as conn:
        conn.execute("INSERT INTO media_jobs_migrations(name, applied_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
                     (MIGRATION_NAME, int(time.time())))
        conn.commit()
    return dict(counts)
