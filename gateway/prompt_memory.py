"""Maker-scoped prompt retention for generated assets.

This is the retained personal store for media prompts. Private execution spools
may keep input until normal jobs finish so they can resume after restart.
Receipts, job metadata, asset sidecars and diagnostics never carry that text or
a workflow graph.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

import os

from gateway import admin_store, directory_index, media_jobs, media_metadata, private_io, request_connections, retention
from gateway.sqlite_startup import connect, connect_readonly
from gateway.public_failure import PublicFailure

DB_PATH = media_jobs.DB_PATH
# The policy, the decision and the one writer are gateway/retention.py; the defaults are registry/retention.yml.
NAMESPACE = retention.NAMESPACE
RETENTION_LOCK_TIMEOUT_SECONDS = retention.RETENTION_LOCK_TIMEOUT_SECONDS
ERASURE_CUTOFF_TABLE = retention.ERASURE_CUTOFF_TABLE
Decision = retention.Decision
PromptModeError = retention.PromptModeError
requested_mode = retention.requested_mode
retention_guard = retention.guard

_DDL = """
CREATE TABLE IF NOT EXISTS asset_prompts (
  asset_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  metadata_json TEXT,
  created_at INTEGER NOT NULL,
  kept_until INTEGER,
  PRIMARY KEY (asset_id, person_id)
);
CREATE INDEX IF NOT EXISTS asset_prompts_expiry ON asset_prompts(kept_until);
CREATE INDEX IF NOT EXISTS asset_prompts_person ON asset_prompts(person_id);
CREATE TABLE IF NOT EXISTS media_volume_identity (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  volume_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""


def prompt_text(body: Mapping[str, Any] | None) -> str | None:
    """Extract the maker's original words, not an engine-rewritten prompt."""
    if not isinstance(body, Mapping):
        return None
    for field in ("prompt", "input", "tags"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    shots = body.get("shots")
    if isinstance(shots, list):
        texts = [shot["prompt"].strip() for shot in shots
                 if isinstance(shot, Mapping) and isinstance(shot.get("prompt"), str) and shot["prompt"].strip()]
        return "\n\n".join(texts) if texts else None
    return None


def capture_metadata(content: bytes, suffix: str) -> dict[str, str]:
    """Capture workflow data in the sole prompt record before store-time strip."""
    kind = media_metadata.media_kind(suffix)
    if kind == "png":
        return media_metadata.png_text_chunks(content)
    if kind in {"jpeg", "webp"}:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            exif = image.getexif()
            values: dict[str, str] = {}
            for key, label in ((0x010F, "workflow"), (0x0110, "prompt"), (0x010E, "azizam_provenance")):
                value = exif.get(key)
                if isinstance(value, str) and value:
                    values[label] = value.removeprefix(f"{label}:")
            return values
    if kind in {"video", "audio"}:
        return media_metadata.read_media_tags(content, suffix)
    return {}


def restore_metadata(content: bytes, suffix: str, values: Mapping[str, str]) -> bytes:
    """Build a maker-only full download from clean media plus one prompt row."""
    kind = media_metadata.media_kind(suffix)
    if not values:
        return content
    if kind == "png":
        return media_metadata.embed_png_text(content, values)
    if kind in {"video", "audio"}:
        return media_metadata.embed_media_tags(content, suffix, values)
    if kind in {"jpeg", "webp"}:
        return media_metadata.embed_image_metadata(content, suffix, values)
    return content


def decide(
    *, person_id: str | None, member: Mapping[str, Any] | None,
    household: Mapping[str, Any] | None, request_body: Mapping[str, Any] | None,
    responses_store: bool | None = None, now: int | None = None,
    kind: str = retention.LIBRARY_PROMPT,
) -> Decision:
    """Resolve policy before any payload, media or Responses persistence (``retention.decide``)."""
    return retention.decide(
        kind, person_id=person_id, member=member, household_policy=household,
        request_body=request_body, responses_store=responses_store, now=now,
    )


def household_settings(*, admin_path: Path | None = None) -> dict[str, Any]:
    row = admin_store.get_operator_setting(retention.SETTING_KEY, path=admin_path or admin_store.DB_PATH)
    return retention.household(row.get("value") if row else None)


def decide_for_person(
    person_id: str | None, body: Mapping[str, Any] | None, *,
    admin_path: Path | None = None,
    responses_store: bool | None = None,
    kind: str = retention.LIBRARY_PROMPT,
) -> Decision:
    """Resolve memory policy for an authenticated person independent of App."""
    path = admin_path or admin_store.DB_PATH
    try:
        member = admin_store.ensure_prompt_memory_preference(person_id, path=path) if person_id else None
        household = household_settings(admin_path=path)
    except sqlite3.OperationalError:
        # If storage is unavailable, validate the request and disable memory.
        requested_mode(body)
        return Decision(person_id, False, "storage_unavailable", kind=kind)
    return decide(
        person_id=person_id, member=member, household=household,
        request_body=body, responses_store=responses_store, kind=kind,
    )


def get_member(person_id: str, *, path: Path | None = None) -> dict[str, Any]:
    record = admin_store.get_prompt_memory_preference(person_id, path=path or admin_store.DB_PATH)
    if not record:
        return retention.member_defaults()
    return {
        "remember": bool(record.get("remember")),
        "retention": str(record.get("retention") or retention.member_defaults()["retention"]),
    }


def put_member(
    person_id: str,
    *,
    remember: bool,
    retention: str,
    path: Path | None = None,
) -> dict[str, Any]:
    return admin_store.put_prompt_memory_preference(
        person_id,
        remember=bool(remember),
        retention=retention,
        updated_by=person_id,
        path=path or admin_store.DB_PATH,
    )


def household_default(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {**retention.household_defaults(), **(value or {})}


def effective(member: Mapping[str, Any] | None, household: Mapping[str, Any] | None) -> dict[str, Any]:
    return retention.effective(member, household)


def _connect(path: Path | None) -> sqlite3.Connection:
    target = Path(path or media_jobs.DB_PATH)
    private_io.ensure_private_dir(target.parent)
    conn = connect(target, timeout=5)
    conn.row_factory = sqlite3.Row
    media_jobs._apply_pragmas(conn)
    conn.execute("PRAGMA secure_delete=ON")
    conn.executescript(_DDL)
    return conn


_PREPARED: set[tuple[str, int]] = set()


@contextmanager
def _read_connection(path: Path | None) -> Iterator[sqlite3.Connection]:
    """A connection for prompt reads; inside a request it is the request's media-job connection."""
    if not request_connections.active():
        conn = _connect(path)
        try:
            yield conn
        finally:
            conn.close()
        return
    target = Path(path or media_jobs.DB_PATH)
    with media_jobs._conn(target) as conn:
        try:
            stamp = (str(target), os.stat(target).st_ino)
        except OSError:
            stamp = None
        if stamp is None or stamp not in _PREPARED:
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(_DDL)
            if stamp is not None:
                _PREPARED.add(stamp)
        yield conn


def init_db(*, path: Path | None = None) -> None:
    conn = _connect(path)
    conn.close()


def put(
    asset_id: str, person_id: str, prompt_text: str, *, kept_until: int | None,
    metadata: Mapping[str, str] | None = None, path: Path | None = None,
    admin_path: Path | None = None, created_at: int | None = None,
    on_result: Callable[[retention.WriteOutcome], None] | None = None,
) -> bool:
    if not asset_id or not person_id or not isinstance(prompt_text, str) or not prompt_text:
        raise ValueError("asset, maker and nonempty prompt text are required")
    started = int(time.time()) if created_at is None else created_at

    def insert(deadline: int | None) -> None:
        conn = _connect(path)
        try:
            conn.execute(
                "INSERT INTO asset_prompts(asset_id,person_id,prompt_text,metadata_json,created_at,kept_until) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id,person_id) DO UPDATE SET "
                "prompt_text=excluded.prompt_text,metadata_json=excluded.metadata_json,"
                "created_at=excluded.created_at,kept_until=excluded.kept_until",
                (asset_id, person_id, prompt_text,
                 json.dumps(dict(metadata), ensure_ascii=False) if metadata else None,
                 started, deadline),
            )
            conn.commit()
        finally:
            conn.close()

    return retention.write(
        retention.LIBRARY_PROMPT, admin_path=admin_path, person_id=person_id, created_at=started,
        kept_until=kept_until, commit=insert, on_result=on_result,
    )


def safe_put(
    asset_id: str, person_id: str, prompt_text: str, *, kept_until: int | None,
    metadata: Mapping[str, str] | None = None, path: Path | None = None,
    admin_path: Path | None = None, created_at: int | None = None,
    on_result: Callable[[retention.WriteOutcome], None] | None = None,
) -> bool:
    """Best-effort prompt write; storage contention cannot fail media delivery."""
    try:
        return put(
            asset_id, person_id, prompt_text, kept_until=kept_until, metadata=metadata,
            path=path, admin_path=admin_path, created_at=created_at, on_result=on_result,
        )
    except Exception:
        if on_result is not None:
            on_result(retention.WriteOutcome(False, "write_failed"))
        return False


def tighten_retention(period: str, *, person_id: str | None = None,
                      path: Path | None = None, now: int | None = None) -> int:
    """Shorten existing deadlines; never restore or lengthen a prompt."""
    seconds = retention.seconds_map()[period]
    if seconds is None:
        return 0
    conn = _connect(path)
    try:
        where = "WHERE person_id=?" if person_id else ""
        args: tuple[Any, ...] = (person_id,) if person_id else ()
        count = conn.execute(
            "UPDATE asset_prompts SET kept_until=MIN(COALESCE(kept_until, created_at+?), created_at+?) " + where,
            (seconds, seconds, *args),
        ).rowcount
        conn.execute("DELETE FROM asset_prompts WHERE kept_until<=?", (int(time.time()) if now is None else now,))
        conn.commit()
        return count
    finally:
        conn.close()


def require_resolvable_periods(*, admin_path: Path) -> None:
    """Startup check: every stored period ID is in the contract, or a finite offered choice can replace it.

    A file that offers only no-expiry choices is valid until a stored ID is deleted from it. Then the gateway
    has no period to apply, so it stops with the ID named (``retention.normalize_period`` raises).
    """
    try:
        conn = connect_readonly(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
        try:
            stored = {row[0] for row in conn.execute("SELECT retention FROM prompt_memory_preferences")}
            row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (retention.SETTING_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logging.getLogger(__name__).warning("stored retention period check deferred")
        return
    try:
        groups = json.loads(row[0]).get("groups") if row else None
        cap = (groups or {}).get(retention.GROUP_PROMPTS, {}).get("retention_cap")
    except (AttributeError, TypeError, ValueError):
        cap = None  # A corrupt setting is tolerated and logged by the deadline repair.
    for period in sorted(item for item in (*stored, cap) if isinstance(item, str)):
        retention.normalize_period(period, kind=retention.LIBRARY_PROMPT, owner_cap=period == cap)


def tighten_contract_periods(*, admin_path: Path, path: Path | None = None) -> int:
    """On startup, apply the current contract's shorter deadlines to saved prompts.

    Stored member and household choices stay unchanged. The same normalized
    period used for display and new writes determines the older rows' ceiling.
    Read one policy snapshot. Setting writes can proceed and independently
    tighten deadlines; neither path extends an existing deadline.
    """
    contract = retention.bundled()
    admin_conn = connect_readonly(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    try:
        admin_conn.execute("BEGIN")
        row = admin_conn.execute("SELECT value_json FROM operator_settings WHERE key=?",
                                 (retention.SETTING_KEY,)).fetchone()
        cap = contract.household_defaults(retention.LIBRARY_PROMPT)["retention_cap"]
        if row:
            try:
                document = json.loads(row[0])
                if not isinstance(document, Mapping):
                    raise ValueError("setting must be an object")
                groups = document.get("groups", {})
                if not isinstance(groups, Mapping):
                    raise ValueError("groups must be an object")
                group = groups.get(retention.GROUP_PROMPTS, {})
                if not isinstance(group, Mapping):
                    raise ValueError("prompts must be an object")
                stored_cap = group.get("retention_cap", cap)
                if not isinstance(stored_cap, str):
                    raise ValueError("retention cap must be a string")
                cap = stored_cap
            except (TypeError, ValueError):
                logging.getLogger(__name__).warning("invalid stored retention setting; using bundled defaults")
        members = dict(admin_conn.execute("SELECT user_id, retention FROM prompt_memory_preferences"))

        media_conn = _connect(path)
        try:
            changed = 0

            def shorten(seconds: int, person_id: str | None = None) -> None:
                nonlocal changed
                where = " AND person_id=?" if person_id else ""
                args: tuple[Any, ...] = (person_id,) if person_id else ()
                changed += media_conn.execute(
                    "UPDATE asset_prompts SET kept_until=created_at+? "
                    "WHERE (kept_until IS NULL OR kept_until>created_at+?)" + where,
                    (seconds, seconds, *args),
                ).rowcount

            cap_period = retention.normalize_period(cap, kind=retention.LIBRARY_PROMPT, owner_cap=True)
            cap_seconds = contract.seconds_map()[cap_period]
            if cap_seconds is not None:
                shorten(cap_seconds)
            person_ids = media_conn.execute("SELECT DISTINCT person_id FROM asset_prompts").fetchall()
            default_period = contract.member_defaults(retention.LIBRARY_PROMPT)["retention"]
            for (person_id,) in person_ids:
                member_period = members.get(person_id, default_period)
                period = retention.effective_period(member_period, cap)
                seconds = contract.seconds_map()[period]
                if seconds is not None:
                    shorten(seconds, person_id)
            media_conn.execute("DELETE FROM asset_prompts WHERE kept_until<=?", (int(time.time()),))
            media_conn.commit()
        except Exception:
            media_conn.rollback()
            raise
        finally:
            media_conn.close()
        admin_conn.commit()
        return changed
    except Exception:
        admin_conn.rollback()
        raise
    finally:
        admin_conn.close()


def get(asset_id: str, person_id: str | None, *, path: Path | None = None) -> dict[str, Any] | None:
    if not person_id:
        return None
    with _read_connection(path) as conn:
        row = conn.execute(
            "SELECT asset_id,person_id,prompt_text,metadata_json,created_at,kept_until "
            "FROM asset_prompts WHERE asset_id=? AND person_id=? AND "
            "(kept_until IS NULL OR kept_until>?)",
            (asset_id, person_id, int(time.time())),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data


def get_person_prompts(person_id: str, *, path: Path | None = None,
                       now: int | None = None) -> dict[str, dict[str, Any]]:
    """Load one maker's unexpired prompt rows with one query."""
    if not person_id:
        return {}
    with _read_connection(path) as conn:
        rows = conn.execute(
            "SELECT asset_id,person_id,prompt_text,metadata_json,created_at,kept_until "
            "FROM asset_prompts WHERE person_id=? AND (kept_until IS NULL OR kept_until>?)",
            (person_id, int(time.time()) if now is None else now),
        ).fetchall()
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        records[data["asset_id"]] = data
    return records


def delete_person(person_id: str, *, path: Path | None = None) -> int:
    """Delete a person's prompts and their Library hides in one transaction; returns the prompts deleted."""
    conn = _connect(path)
    try:
        count = conn.execute("DELETE FROM asset_prompts WHERE person_id=?", (person_id,)).rowcount
        media_jobs.delete_person_hides_in(conn, person_id)
        conn.commit()
        return count
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_person_erasure(
    person_id: str, *, admin_path: Path | None = None, now: int | None = None,
) -> int:
    """Record a durable cutoff so older in-flight writes cannot restore prompts.

    Timestamps use whole seconds. A new request in the same second as erasure
    may be refused, which keeps the cutoff conservative under clock rounding.
    """
    if not person_id:
        raise ValueError("person_id is required")
    target = Path(admin_path or admin_store.DB_PATH)
    current = int(time.time()) if now is None else int(now)
    conn = connect(target, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout={int(RETENTION_LOCK_TIMEOUT_SECONDS * 1000)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        admin_store._record_prompt_erasure(conn, person_id, current)
        cutoff = conn.execute(
            f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id=?", (person_id,),
        ).fetchone()[0]
        conn.commit()
        return int(cutoff)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recorded_erasure_cutoff(conn: sqlite3.Connection, person_id: str) -> int:
    changes = [0]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (ERASURE_CUTOFF_TABLE,)).fetchone():
        row = conn.execute(f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id=?",
                           (person_id,)).fetchone()
        if row:
            changes.append(int(row[0]))
    preference = conn.execute(
        "SELECT remember, updated_at FROM prompt_memory_preferences WHERE user_id=?", (person_id,),
    ).fetchone()
    if preference is not None and not preference[0]:
        changes.append(int(preference[1] or 0))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'").fetchone():
        access = conn.execute("SELECT disabled_at, removed_at FROM account_access WHERE user_id=?",
                              (person_id,)).fetchone()
        if access and access[1] is not None:
            changes.append(int(access[1]))
    return max(changes)


def recorded_erasure_cutoff(person_id: str, *, admin_path: Path | None = None) -> int:
    """Read durable erasure intent, including settings whose cleanup has not started."""
    if not person_id:
        raise ValueError("person_id is required")
    conn = connect(Path(admin_path or admin_store.DB_PATH),
                           timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    try:
        conn.execute("BEGIN")
        return _recorded_erasure_cutoff(conn, person_id)
    finally:
        conn.close()


def settle_erasure_cutoff(person_id: str, *, admin_path: Path | None = None) -> int:
    """Return the person's cutoff after raising it to when memory went off.

    The setting is read and the cutoff written in one transaction, so a member
    who turned memory back on is never erased by an earlier "off" reading. The
    cutoff is the time of the recorded change, never "now": prompts saved
    after a later switch back on stay outside it.
    """
    if not person_id:
        raise ValueError("person_id is required")
    conn = connect(Path(admin_path or admin_store.DB_PATH),
                           timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout={int(RETENTION_LOCK_TIMEOUT_SECONDS * 1000)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        retention.ensure_erasure_cutoff_table(conn)
        row = conn.execute(
            f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id=?", (person_id,),
        ).fetchone()
        current = int(row[0]) if row else 0
        cutoff = _recorded_erasure_cutoff(conn, person_id)
        if cutoff > current:
            conn.execute(
                f"INSERT INTO {ERASURE_CUTOFF_TABLE}(person_id,cutoff_at) VALUES(?,?) "
                "ON CONFLICT(person_id) DO UPDATE SET cutoff_at=MAX(cutoff_at,excluded.cutoff_at)",
                (person_id, cutoff),
            )
        conn.commit()
        return cutoff
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def household_erasure_cutoff(*, admin_path: Path | None = None) -> int | None:
    """The household fence survives turning memory back on."""
    conn = connect(Path(admin_path or admin_store.DB_PATH),
                           timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ERASURE_CUTOFF_TABLE,),
        ).fetchone()
        row = conn.execute(
            f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id='*'"
        ).fetchone() if table_exists else None
        setting = conn.execute(
            "SELECT value_json, updated_at FROM operator_settings WHERE key=?", (retention.SETTING_KEY,)
        ).fetchone()
        legacy = int(setting[1]) if setting and json.loads(setting[0]).get("never_keep") else 0
        return max(int(row[0]) if row else 0, legacy) or None
    finally:
        conn.close()


def erasure_pending(person_id: str, *, admin_path: Path) -> bool:
    conn = connect(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='prompt_memory_erasure_intents'").fetchone():
            return False
        return conn.execute(
            "SELECT 1 FROM prompt_memory_erasure_intents WHERE person_id IN (?, '*')", (person_id,),
        ).fetchone() is not None
    finally:
        conn.close()


def complete_erasure(person_id: str, cutoff: int, *, admin_path: Path) -> None:
    """An old worker must not acknowledge a newer deletion."""
    conn = connect(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='prompt_memory_erasure_intents'").fetchone():
            conn.execute("DELETE FROM prompt_memory_erasure_intents WHERE person_id=? AND cutoff_at<=?",
                         (person_id, int(cutoff)))
            conn.commit()
    finally:
        conn.close()


def prune_all_before(cutoff: int, *, path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        deleted = conn.execute("DELETE FROM asset_prompts WHERE created_at<=?", (int(cutoff),)).rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def prune_person_before(
    person_id: str, cutoff: int, *, path: Path | None = None,
) -> int:
    """Retry reconciliation by deleting rows at or before an erasure cutoff."""
    conn = _connect(path)
    try:
        deleted = conn.execute(
            "DELETE FROM asset_prompts WHERE person_id=? AND created_at<=?",
            (person_id, int(cutoff)),
        ).rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def delete_all(*, path: Path | None = None) -> int:
    """Purge retained prompts when the server-wide kill switch is enabled."""
    conn = _connect(path)
    try:
        count = conn.execute("DELETE FROM asset_prompts").rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def delete_asset(asset_id: str, *, path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        count = conn.execute("DELETE FROM asset_prompts WHERE asset_id=?", (asset_id,)).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def move(source_id: str, asset_id: str, person_id: str, *, metadata: Mapping[str, str] | None = None,
         path: Path | None = None, admin_path: Path | None = None) -> bool:
    """Move an in-flight video prompt to its finished asset in one transaction.

    An existing destination prompt is never replaced. With the same text the
    rows merge, keeping the earlier creation time and the stricter deadline.
    With different text both rows stay and the move returns False.
    """
    if source_id == asset_id:
        return False
    with retention_guard(admin_path, person_id) as policy:
        if policy["never_keep"] or not policy["remember"]:
            delete_asset_for_person(source_id, person_id, path=path)
            return False
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT prompt_text,metadata_json,created_at,kept_until FROM asset_prompts "
                "WHERE asset_id=? AND person_id=?", (source_id, person_id),
            ).fetchone()
            if row is None:
                return False
            if (row["created_at"] <= policy.get("forgot_before", 0)
                    or (row["kept_until"] is not None and int(row["kept_until"]) <= int(time.time()))):
                conn.execute("DELETE FROM asset_prompts WHERE asset_id=? AND person_id=?", (source_id, person_id))
                conn.commit()
                return False
            destination = conn.execute(
                "SELECT prompt_text,metadata_json,created_at,kept_until FROM asset_prompts "
                "WHERE asset_id=? AND person_id=? AND (kept_until IS NULL OR kept_until>?)",
                (asset_id, person_id, int(time.time())),
            ).fetchone()
            if destination and destination["prompt_text"] != row["prompt_text"]:
                conn.rollback()
                return False
            merged = json.loads(row["metadata_json"] or "{}")
            created_at, kept_until = row["created_at"], row["kept_until"]
            if destination:
                merged.update(json.loads(destination["metadata_json"] or "{}"))
                created_at = min(created_at, destination["created_at"])
                deadlines = [value for value in (kept_until, destination["kept_until"]) if value is not None]
                kept_until = min(deadlines) if deadlines else None
            if metadata:
                merged.update(dict(metadata))
            conn.execute(
                "INSERT OR REPLACE INTO asset_prompts(asset_id,person_id,prompt_text,metadata_json,created_at,kept_until) "
                "VALUES(?,?,?,?,?,?)",
                (asset_id, person_id, row["prompt_text"], json.dumps(merged, ensure_ascii=False), created_at, kept_until),
            )
            conn.execute("DELETE FROM asset_prompts WHERE asset_id=? AND person_id=?", (source_id, person_id))
            conn.commit()
            return True
        finally:
            conn.close()


def prune(*, path: Path | None = None, now: int | None = None) -> int:
    """Apply explicit prompt privacy deadlines; missing media is not consent."""
    conn = _connect(path)
    try:
        current_time = int(time.time()) if now is None else now
        count = conn.execute(
            "DELETE FROM asset_prompts WHERE kept_until IS NOT NULL AND kept_until<=?",
            (current_time,),
        ).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


MEDIA_FOLDER_MARKER = ".azizam-media"


def _folder_identity(folder: Path, expected: tuple[int, int] | None = None) -> None:
    try:
        info = folder.stat()
        if not stat.S_ISDIR(info.st_mode) or (expected is not None and (info.st_dev, info.st_ino) != expected):
            raise OSError("media folder changed")
    except OSError as exc:
        raise PublicFailure("media_folder_unavailable", 503) from exc


def _read_media_marker(folder: Path) -> dict[str, Any] | None:
    """The marker's value, None when there is no marker. Raises when a marker is there but not usable.

    O_NOFOLLOW refuses a symlink rather than reading its target, and O_NONBLOCK stops a
    FIFO from waiting for a writer before the regular-file check refuses it.
    """
    try:
        fd = os.open(folder / MEDIA_FOLDER_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("media marker is not a regular file")
        value = json.loads(os.read(fd, 4097))
        if (not isinstance(value, dict) or not isinstance(value.get("volume_id"), str)
                or len(value["volume_id"]) != 32
                or any(c not in "0123456789abcdef" for c in value["volume_id"])
                or type(value.get("created_at")) is not int or value["created_at"] <= 0):
            raise ValueError("invalid media marker")
        return value
    finally:
        os.close(fd)


def _write_media_marker(folder: Path, *, replace: bool) -> dict[str, Any]:
    """Write a new marker. ``replace`` is the owner's confirmation; otherwise nothing may already be there.

    The owner's replacement goes through a scratch file and os.replace. The adoption
    path creates the marker with O_EXCL instead of a hard link, so it works on
    filesystems without hard links (FAT, exFAT, some SMB mounts). Either way, a
    failed write leaves no marker behind.
    """
    marker = {"volume_id": uuid.uuid4().hex, "created_at": int(time.time())}
    private_io.ensure_private_dir(folder)
    path = folder / MEDIA_FOLDER_MARKER
    payload = json.dumps(marker).encode("utf-8")
    if replace:
        scratch = folder / (MEDIA_FOLDER_MARKER + "." + uuid.uuid4().hex + ".tmp")
        try:
            private_io.create_private_file(scratch)
            with scratch.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(scratch, path)
        finally:
            scratch.unlink(missing_ok=True)
        return marker
    # Raises FileExistsError when a marker appeared after the absence check; nothing is touched then.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return marker


def _recorded_volume(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT volume_id FROM media_volume_identity WHERE singleton=1").fetchone()
    return row[0] if row is not None else None


def _remember_media_marker(conn: sqlite3.Connection, marker: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO media_volume_identity(singleton, volume_id, created_at) VALUES(1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET volume_id=excluded.volume_id, created_at=excluded.created_at",
        (marker["volume_id"], marker["created_at"]),
    )


def _require_media_folder(
    conn: sqlite3.Connection, folder: Path, expected: tuple[int, int] | None = None,
) -> None:
    _folder_identity(folder, expected)
    try:
        marker = _read_media_marker(folder)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PublicFailure("media_folder_unconfirmed", 503) from exc
    recorded = _recorded_volume(conn)
    # Nothing recorded means nothing to match: the owner confirms, never the removal.
    if marker is None or recorded is None or marker["volume_id"] != recorded:
        raise PublicFailure("media_folder_unconfirmed", 503)


def _has_library_entries(conn: sqlite3.Connection) -> bool:
    # This is deliberately global, never the current person's filtered Library.
    # Even an unfinished job prevents automatic adoption of an unknown folder.
    if conn.execute("SELECT 1 FROM asset_prompts LIMIT 1").fetchone():
        return True
    has_jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_jobs'").fetchone()
    return bool(has_jobs and conn.execute("SELECT 1 FROM media_jobs LIMIT 1").fetchone())


def _holds_media(folder: Path) -> bool:
    """Whether the folder holds a stored media file. Sidecars, scratch files and other names do not count."""
    return any(directory_index.stored_digest(name) is not None for name in os.listdir(folder))


def _adopt_fresh_folder(folder: Path, *, prompt_path: Path | None, path: Path | None) -> None:
    """Record this folder for a fresh Library: no entries in either store, no media file, no marker.

    Anything else needs the owner's confirmation. The refusals are checked before any
    connection is opened, so an unconfirmed install that is not fresh pays nothing per read.
    Failing to write quietly leaves the folder unconfirmed, which the Library shows as such.
    """
    if os.path.lexists(folder / MEDIA_FOLDER_MARKER):
        return
    try:
        if _holds_media(folder):
            return
    except OSError:
        return
    with _read_connection(path) as conn:
        if _has_library_entries(conn):
            return
    if prompt_path is not None and Path(prompt_path) != Path(path or media_jobs.DB_PATH):
        with _read_connection(prompt_path) as other:
            if _has_library_entries(other):
                return
    _record_fresh_folder(folder, path=path)


def _record_fresh_folder(folder: Path, *, path: Path | None) -> None:
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The checks repeat inside the write lock, so two first reads cannot both adopt.
            if (_recorded_volume(conn) is None and not os.path.lexists(folder / MEDIA_FOLDER_MARKER)
                    and not _has_library_entries(conn)):
                marker = _write_media_marker(folder, replace=False)
                _remember_media_marker(conn, marker)
                conn.commit()
            else:
                conn.rollback()
        except (OSError, ValueError):
            conn.rollback()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def media_folder_state(
    folder: Path, *, path: Path | None = None, prompt_path: Path | None = None,
    expected: tuple[int, int] | None = None,
) -> str:
    """The folder's state: "confirmed", "unconfirmed" (nothing recorded yet) or "mismatch" (recorded, but not on this folder).

    A fresh Library adopts its folder here, on its first read. Ordinary media writes never
    reach this, and a recorded identity is never regenerated: an existing install or a moved
    volume needs the owner's confirmation. Raises PublicFailure when the folder cannot be seen.
    """
    _folder_identity(folder, expected)
    with _read_connection(path) as conn:
        recorded = _recorded_volume(conn)
    if recorded is None:
        _adopt_fresh_folder(folder, prompt_path=prompt_path, path=path)
        with _read_connection(path) as conn:
            recorded = _recorded_volume(conn)
    try:
        marker = _read_media_marker(folder)
    except (OSError, ValueError, UnicodeError):
        marker = None  # an unusable marker matches nothing
    if recorded is None:
        return "unconfirmed"
    if marker is None or marker["volume_id"] != recorded:
        return "mismatch"
    return "confirmed"


def confirm_media_folder(
    folder: Path, *, owner: bool, path: Path | None = None,
    expected: tuple[int, int] | None = None,
) -> str:
    """The owner explicitly adopts this folder and records its marker. Removes no entries. Returns the volume id."""
    if not owner:
        raise PublicFailure("media_folder_owner_required", 403)
    _folder_identity(folder, expected)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            marker = _write_media_marker(folder, replace=True)
        except OSError as exc:
            raise PublicFailure("media_folder_unavailable", 503) from exc
        _remember_media_marker(conn, marker)
        _require_media_folder(conn, folder, expected)
        conn.commit()
        return str(marker["volume_id"])
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_missing_media(
    person_id: str,
    gone_asset_ids: Iterable[str],
    hides: Iterable[tuple[str, str]],
    job_ids: Iterable[str],
    *,
    path: Path | None = None,
    media_folder: Path | None = None,
    media_identity: tuple[int, int] | None = None,
    prompt_path: Path | None = None,
) -> int:
    """Remove missing entries only while the recorded volume is present.

    The volume is checked before any deletion and again before commit, so a
    refusal or a failed statement rolls back every store this touches. A split
    prompt store is attached to the same connection, but both stores run in WAL
    mode, and SQLite does not commit WAL databases atomically together. A crash
    between the two commits can therefore keep the job rows and lose the
    prompts' deletion, or the reverse.
    """
    if media_folder is None:
        raise PublicFailure("media_folder_unavailable", 503)
    conn = _connect(path)
    other = None
    try:
        prompts = "asset_prompts"
        if prompt_path is not None and Path(prompt_path) != Path(path or media_jobs.DB_PATH):
            other = _connect(prompt_path)
            # Keep the store connection open to hold its startup/recovery lock.
            conn.execute("ATTACH DATABASE ? AS retained_prompts", (str(prompt_path),))
            prompts = "retained_prompts.asset_prompts"
        conn.execute("BEGIN IMMEDIATE")
        try:
            _require_media_folder(conn, media_folder, media_identity)
            conn.executemany(
                f"DELETE FROM {prompts} WHERE asset_id=? AND person_id=?",
                [(asset_id, person_id) for asset_id in gone_asset_ids],
            )
            media_jobs.hide_assets_in(conn, person_id, hides)
            deleted = media_jobs.delete_succeeded_in(conn, job_ids)
            _require_media_folder(conn, media_folder, media_identity)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return deleted
    finally:
        conn.close()
        if other is not None:
            other.close()


def delete_asset_for_person(asset_id: str, person_id: str, *, path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        count = conn.execute(
            "DELETE FROM asset_prompts WHERE asset_id=? AND person_id=?", (asset_id, person_id),
        ).rowcount
        conn.commit()
        return count
    finally:
        conn.close()
