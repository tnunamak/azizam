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
