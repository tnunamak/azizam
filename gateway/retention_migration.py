"""One-time data migration (admin schema version 14): operator setting ``prompt_memory`` becomes ``retention``.

Before this version the household's prompt-memory policy was one setting, ``prompt_memory``, with three values:
``default_remember``, ``never_keep`` and ``retention_cap``. The retention contract (docs/design/REQUEST_RETENTION.md
section 12) keeps the household policy as the setting ``retention``, with one switch for the whole gateway and one
entry per group of kinds. Today there is one group, ``prompts``, and it holds what ``default_remember`` and
``retention_cap`` held:

    {"default_remember": B, "never_keep": K, "retention_cap": C}
        becomes  {"never_keep": K, "groups": {"prompts": {"default_keep": B, "retention_cap": C}}}

This is a pure move. A value is copied as it is, with no check, so the policy in force does not change: a value the
old code accepted the new code accepts, and a value it refused it still refuses. A value the legacy setting did not
hold stays unset, so the bundled default applies, as before. The setting's revision, last-update time and author move
with it. Member rows (``prompt_memory_preferences``) are the rows of the group ``prompts`` and do not move: no
member's choice, no deadline and no stored prompt or conversation changes.

Running it again does nothing, because the old setting is gone. If both settings exist the database was edited by hand;
the migration stops with the reason rather than choose a policy for the owner.
"""
from __future__ import annotations

import json
import sqlite3
import time

from gateway import retention

LEGACY_KEY = retention.PROMPTS_VIEW_KEY
_LEGACY_FIELDS = retention.PROMPTS_VIEW_FIELDS


class RetentionMigrationError(RuntimeError):
    """The household policy cannot be moved safely. The message says what to fix."""


def migrate(conn: sqlite3.Connection) -> dict[str, object]:
    """Move the setting inside the caller's transaction. Returns what moved (empty when there was nothing)."""
    row = conn.execute(
        "SELECT value_json, updated_at, updated_by FROM operator_settings WHERE key=?", (LEGACY_KEY,)
    ).fetchone()
    if row is None:
        return {}
    if conn.execute("SELECT 1 FROM operator_settings WHERE key=?", (retention.SETTING_KEY,)).fetchone():
        raise RetentionMigrationError(
            f"operator settings holds both {LEGACY_KEY!r} and {retention.SETTING_KEY!r}; "
            f"remove the one that is out of date and start again"
        )
    try:
        legacy = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        raise RetentionMigrationError(f"operator setting {LEGACY_KEY!r} is not JSON; fix or remove it and start again") from None
    if not isinstance(legacy, dict):
        raise RetentionMigrationError(f"operator setting {LEGACY_KEY!r} is not an object; fix or remove it and start again")
    document = retention.document_from_prompts_view(legacy)
    unclassified = {key: value for key, value in legacy.items() if key not in _LEGACY_FIELDS}
    if unclassified:
        # D34: unknown settings remain stored for a future classification, outside every API projection.
        document["legacy_unclassified"] = unclassified
    conn.execute(
        "INSERT INTO operator_settings(key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
        (retention.SETTING_KEY, json.dumps(document, ensure_ascii=False, sort_keys=True), row[1], row[2]),
    )
    # The revision counter carries over, so a settings form that is open across the upgrade still matches.
    conn.execute("DELETE FROM operator_settings_generations WHERE setting_key=?", (retention.SETTING_KEY,))
    conn.execute(
        "UPDATE operator_settings_generations SET setting_key=? WHERE setting_key=?",
        (retention.SETTING_KEY, LEGACY_KEY),
    )
    conn.execute("DELETE FROM operator_settings WHERE key=?", (LEGACY_KEY,))
    return {"moved": LEGACY_KEY, "to": retention.SETTING_KEY,
            "unclassified": len(unclassified)}


#: Schema 17: the tables whose rows gain a preference group, and the column that must exist after the rebuild.
_GROUPED_TABLES = ("prompt_memory_preferences", "prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_group_keys(conn: sqlite3.Connection) -> dict[str, int]:
    """Key member rows and erasure cutoffs by preference group (REQUEST_RETENTION.md sections 3.2 and 12).

    Every existing row becomes a row of group ``prompts``, which is what it always was. Every existing person also
    gets a ``request_history`` row with the switch off, whatever the household default is: nobody already using
    Azizam starts keeping request bodies without being told (section 12, item 3). The statements run inside the
    caller's transaction and use no ``executescript``, which would commit first. Running it again changes nothing.
    """
    pending_cutoffs = [table for table in _GROUPED_TABLES[1:]
                       if _exists(conn, table) and "grp" not in _columns(conn, table)]
    if "grp" in _columns(conn, "prompt_memory_preferences") and not pending_cutoffs:
        return {}
    moved: dict[str, int] = {}
    history = retention.bundled().member_defaults(retention.REQUEST_HISTORY)
    now = int(time.time())
    if "grp" not in _columns(conn, "prompt_memory_preferences"):
        conn.execute("ALTER TABLE prompt_memory_preferences RENAME TO prompt_memory_preferences_v16")
        conn.execute(
            "CREATE TABLE prompt_memory_preferences ("
            "user_id TEXT NOT NULL, grp TEXT NOT NULL, remember INTEGER NOT NULL, retention TEXT NOT NULL, "
            "parts_json TEXT, updated_at INTEGER NOT NULL, updated_by TEXT, PRIMARY KEY(user_id, grp), "
            "FOREIGN KEY(user_id) REFERENCES users(id))")
        conn.execute(
            "INSERT INTO prompt_memory_preferences(user_id, grp, remember, retention, parts_json, updated_at, updated_by) "
            "SELECT user_id, ?, remember, retention, NULL, updated_at, updated_by FROM prompt_memory_preferences_v16",
            (retention.GROUP_PROMPTS,))
        moved["prompts"] = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(
            "INSERT INTO prompt_memory_preferences(user_id, grp, remember, retention, parts_json, updated_at, updated_by) "
            "SELECT id, ?, 0, ?, NULL, ?, NULL FROM users",
            (retention.GROUP_REQUEST_HISTORY, history["retention"], now))
        moved["request_history"] = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("DROP TABLE prompt_memory_preferences_v16")
    for table in ("prompt_memory_erasure_cutoffs", "prompt_memory_erasure_intents"):
        if not _exists(conn, table) or "grp" in _columns(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v16")
        conn.execute(
            f"CREATE TABLE {table} (person_id TEXT NOT NULL, grp TEXT NOT NULL, cutoff_at INTEGER NOT NULL, "
            "PRIMARY KEY(person_id, grp))")
        conn.execute(
            f"INSERT INTO {table}(person_id, grp, cutoff_at) SELECT person_id, ?, cutoff_at FROM {table}_v16",
            (retention.GROUP_PROMPTS,))
        moved[table] = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(f"DROP TABLE {table}_v16")
    return moved


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
