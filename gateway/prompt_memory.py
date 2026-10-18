"""Maker-scoped prompt retention for generated assets.

This is the retained personal store for media prompts. Private execution spools
may keep input until normal jobs finish so they can resume after restart.
Receipts, job metadata, asset sidecars and diagnostics never carry that text or
a workflow graph.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import os

from gateway import admin_store, media_jobs, media_metadata, request_connections
from gateway.sqlite_startup import connect
from gateway.public_failure import PublicFailure

DB_PATH = media_jobs.DB_PATH
NAMESPACE = "com.azizam.prompts"
RETENTION_SECONDS = {"never": None, "3_months": 90 * 86400, "1_year": 365 * 86400}
RETENTION_LOCK_TIMEOUT_SECONDS = 0.25
ERASURE_CUTOFF_TABLE = "prompt_memory_erasure_cutoffs"
MEMBER_DEFAULTS = {"remember": True, "retention": "never"}
HOUSEHOLD_DEFAULTS = {"default_remember": True, "never_keep": False, "retention_cap": "never"}

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


@dataclass(frozen=True)
class Decision:
    person_id: str | None
    keep: bool
    reason: str
    kept_until: int | None = None
    created_at: int | None = None


class PromptModeError(ValueError):
    pass


def requested_mode(body: Mapping[str, Any] | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    extensions = body.get("extensions")
    if extensions is None:
        return None
    if not isinstance(extensions, Mapping):
        raise PromptModeError("extensions must be an object")
    block = extensions.get(NAMESPACE)
    if block is None:
        return None
    if not isinstance(block, Mapping) or set(block) != {"mode"} or block.get("mode") != "forget":
        raise PromptModeError(f"extensions.{NAMESPACE} must be {{'mode':'forget'}}")
    return "forget"


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
) -> Decision:
    """Resolve policy before any payload, media or Responses persistence."""
    mode = requested_mode(request_body)
    policy = {**HOUSEHOLD_DEFAULTS, **(household or {})}
    if policy["never_keep"]:
        return Decision(person_id, False, "server_disabled")
    if mode == "forget" or responses_store is False:
        return Decision(person_id, False, "request_forget")
    if not person_id:
        return Decision(None, False, "no_person")
    preference = {**MEMBER_DEFAULTS, "remember": policy["default_remember"], **(member or {})}
    if not preference.get("remember", policy["default_remember"]):
        return Decision(person_id, False, "member_disabled")
    started = int(time.time()) if now is None else now
    member_period = RETENTION_SECONDS.get(preference.get("retention", "never"))
    cap_period = RETENTION_SECONDS.get(policy.get("retention_cap", "never"))
    finite = [period for period in (member_period, cap_period) if period is not None]
    expiry = started + min(finite) if finite else None
    return Decision(person_id, True, "remembered", expiry, started)


@contextmanager
def retention_guard(admin_path: Path | None, person_id: str | None = None) -> Iterator[dict[str, Any]]:
    """Hold the admin setting's write lock through a prompt-bearing write.

    A kill-switch update waits for this write, then purges it. A write that
    starts after the update sees the new policy and is refused. The lock works
    across gateway processes because it is a SQLite transaction.
    """
    if admin_path is None:
        yield {**HOUSEHOLD_DEFAULTS, **MEMBER_DEFAULTS}
        return
    conn = connect(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout={int(RETENTION_LOCK_TIMEOUT_SECONDS * 1000)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value_json FROM operator_settings WHERE key='prompt_memory'").fetchone()
        household = {**HOUSEHOLD_DEFAULTS, **(json.loads(row[0]) if row else {})}
        _ensure_erasure_cutoff_table(conn)
        erased = conn.execute(
            f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id='*'",
        ).fetchone()
        household_cutoff = int(erased[0]) if erased else 0
        member = None
        person_enabled = True
        forgot_before = household_cutoff
        if person_id:
            erased = conn.execute(
                f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id=?", (person_id,),
            ).fetchone()
            forgot_before = max(household_cutoff, int(erased[0]) if erased else 0)
            member = conn.execute(
                "SELECT remember, retention FROM prompt_memory_preferences WHERE user_id=?", (person_id,),
            ).fetchone()
            user_exists = conn.execute("SELECT 1 FROM users WHERE id=?", (person_id,)).fetchone() is not None
            if not user_exists:
                person_enabled = False
            else:
                if member is None:
                    conn.execute(
                        "INSERT OR IGNORE INTO prompt_memory_preferences"
                        "(user_id, remember, retention, updated_at, updated_by) "
                        "VALUES (?, ?, 'never', ?, NULL)",
                        (person_id, 1 if household["default_remember"] else 0, int(time.time())),
                    )
                    member = conn.execute(
                        "SELECT remember, retention FROM prompt_memory_preferences WHERE user_id=?", (person_id,),
                    ).fetchone()
                account_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'"
                ).fetchone()
                if account_table is not None:
                    access = conn.execute(
                        "SELECT disabled_at, removed_at FROM account_access WHERE user_id=?", (person_id,),
                    ).fetchone()
                    person_enabled = access is None or (access[0] is None and access[1] is None)
        policy = {**household, "remember": bool(member[0]) if member else household["default_remember"],
                  "retention": member[1] if member else MEMBER_DEFAULTS["retention"],
                  "forgot_before": forgot_before}
        policy["remember"] = bool(policy["remember"]) and person_enabled
        yield policy
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_erasure_cutoff_table(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ERASURE_CUTOFF_TABLE,),
    ).fetchone() is None:
        conn.execute(
            f"CREATE TABLE {ERASURE_CUTOFF_TABLE} ("
            "person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)"
        )


def household_settings(*, admin_path: Path | None = None) -> dict[str, Any]:
    row = admin_store.get_operator_setting(
        "prompt_memory", path=admin_path or admin_store.DB_PATH,
    )
    return {**HOUSEHOLD_DEFAULTS, **(row.get("value") or {} if row else {})}


def decide_for_person(
    person_id: str | None, body: Mapping[str, Any] | None, *,
    admin_path: Path | None = None,
    responses_store: bool | None = None,
) -> Decision:
    """Resolve memory policy for an authenticated person independent of App."""
    path = admin_path or admin_store.DB_PATH
    try:
        member = admin_store.ensure_prompt_memory_preference(person_id, path=path) if person_id else None
        household = household_settings(admin_path=path)
    except sqlite3.OperationalError:
        # If storage is unavailable, validate the request and disable memory.
        requested_mode(body)
        return Decision(person_id, False, "storage_unavailable")
    return decide(
        person_id=person_id, member=member, household=household,
        request_body=body, responses_store=responses_store,
    )


def get_member(person_id: str, *, path: Path | None = None) -> dict[str, Any]:
    record = admin_store.get_prompt_memory_preference(person_id, path=path or admin_store.DB_PATH)
    if not record:
        return dict(MEMBER_DEFAULTS)
    return {
        "remember": bool(record.get("remember")),
        "retention": str(record.get("retention") or MEMBER_DEFAULTS["retention"]),
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
    return {**HOUSEHOLD_DEFAULTS, **(value or {})}


def effective(member: Mapping[str, Any] | None, household: Mapping[str, Any] | None) -> dict[str, Any]:
    prefs = {**MEMBER_DEFAULTS, **(member or {})}
    policy = household_default(household)
    remember = bool(prefs.get("remember", True)) and not bool(policy.get("never_keep"))
    return {
        "remember": remember,
        "retention": _effective_retention(str(prefs.get("retention") or "never"), str(policy.get("retention_cap") or "never")),
    }


def _effective_retention(member_retention: str, cap_retention: str) -> str:
    finite = [value for value in (member_retention, cap_retention) if RETENTION_SECONDS.get(value) is not None]
    if not finite:
        return "never"
    return min(finite, key=lambda value: RETENTION_SECONDS[value] or 0)


def _connect(path: Path | None) -> sqlite3.Connection:
    target = Path(path or media_jobs.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
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
) -> bool:
    if not asset_id or not person_id or not isinstance(prompt_text, str) or not prompt_text:
        raise ValueError("asset, maker and nonempty prompt text are required")
    started = int(time.time()) if created_at is None else created_at
    with retention_guard(admin_path, person_id) as policy:
        if (policy["never_keep"] or not policy["remember"]
                or started <= policy.get("forgot_before", 0)):
            return False
        periods = [RETENTION_SECONDS[value] for value in
                   (policy["retention"], policy["retention_cap"])
                   if RETENTION_SECONDS[value] is not None]
        limits = [kept_until] if kept_until is not None else []
        limits.extend(started + period for period in periods)
        deadline = min(limits) if limits else None
        if deadline is not None and deadline <= int(time.time()):
            return False
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
    return True


def safe_put(
    asset_id: str, person_id: str, prompt_text: str, *, kept_until: int | None,
    metadata: Mapping[str, str] | None = None, path: Path | None = None,
    admin_path: Path | None = None, created_at: int | None = None,
) -> bool:
    """Best-effort prompt write; storage contention cannot fail media delivery."""
    try:
        return put(
            asset_id, person_id, prompt_text, kept_until=kept_until, metadata=metadata,
            path=path, admin_path=admin_path, created_at=created_at,
        )
    except Exception:
        return False


def tighten_retention(period: str, *, person_id: str | None = None,
                      path: Path | None = None, now: int | None = None) -> int:
    """Shorten existing deadlines; never restore or lengthen a prompt."""
    seconds = RETENTION_SECONDS[period]
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
        _ensure_erasure_cutoff_table(conn)
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
            "SELECT value_json, updated_at FROM operator_settings WHERE key='prompt_memory'"
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
    marker = {"volume_id": uuid.uuid4().hex, "created_at": int(time.time())}
    scratch = folder / (MEDIA_FOLDER_MARKER + "." + uuid.uuid4().hex + ".tmp")
    try:
        with scratch.open("x", encoding="utf-8") as stream:
            json.dump(marker, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(scratch, folder / MEDIA_FOLDER_MARKER)
        else:
            # Never overwrite a marker that appeared after the absence check.
            os.link(scratch, folder / MEDIA_FOLDER_MARKER)
    finally:
        scratch.unlink(missing_ok=True)
    return marker


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
    recorded = conn.execute("SELECT volume_id FROM media_volume_identity WHERE singleton=1").fetchone()
    if marker is None or recorded is None or marker["volume_id"] != recorded[0]:
        raise PublicFailure("media_folder_unconfirmed", 503)


def _has_library_entries(conn: sqlite3.Connection) -> bool:
    # This is deliberately global, never the current person's filtered Library.
    # Even an unfinished job prevents automatic adoption of an unknown folder.
    if conn.execute("SELECT 1 FROM asset_prompts LIMIT 1").fetchone():
        return True
    has_jobs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_jobs'").fetchone()
    return bool(has_jobs and conn.execute("SELECT 1 FROM media_jobs LIMIT 1").fetchone())


def ensure_media_folder(
    folder: Path, *, path: Path | None = None, prompt_path: Path | None = None,
    expected: tuple[int, int] | None = None,
) -> None:
    """Initialize only a fresh Library; ordinary media writes never call this.

    A recorded identity is never regenerated automatically, even after all
    entries have been removed. Existing installs and moves need the owner.
    """
    _folder_identity(folder, expected)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute("SELECT 1 FROM media_volume_identity WHERE singleton=1").fetchone()
        if recorded is None:
            populated = _has_library_entries(conn)
            if prompt_path is not None and Path(prompt_path) != Path(path or media_jobs.DB_PATH):
                other = _connect(prompt_path)
                try:
                    populated = populated or _has_library_entries(other)
                finally:
                    other.close()
            # Orphan files/sidecars may still be Library entries without a job
            # or retained prompt. Only a truly fresh folder is auto-adopted.
            try:
                populated = populated or any(name != MEDIA_FOLDER_MARKER for name in os.listdir(folder))
            except OSError as exc:
                raise PublicFailure("media_folder_unavailable", 503) from exc
            if populated:
                raise PublicFailure("media_folder_unconfirmed", 503)
            try:
                marker = _read_media_marker(folder)
                if marker is None:
                    marker = _write_media_marker(folder, replace=False)
            except (OSError, ValueError, UnicodeError) as exc:
                raise PublicFailure("media_folder_unconfirmed", 503) from exc
            _remember_media_marker(conn, marker)
        _require_media_folder(conn, folder, expected)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_media_folder(
    folder: Path, *, owner: bool, path: Path | None = None,
    expected: tuple[int, int] | None = None,
) -> None:
    """The owner explicitly adopts this folder. This action removes no entries."""
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

    The volume is checked inside the write transaction before any deletion
    and again before commit. A split prompt store joins the same transaction,
    so a refusal or statement failure cannot leave just its prompts deleted.
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
