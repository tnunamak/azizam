#!/usr/bin/env python3
"""Tag existing MCP sign-in receipts with the connection that made them.

New receipts record ``connection_id`` (an ``mcp_oauth_grants.id``) when an MCP
OAuth token authenticates the request. Older ones ran as the person's own
session App under the name "<client name> (MCP)" and carry no connection. This
sets ``connection_id`` on such a receipt when exactly one of that person's
connections could have made it:

* the receipt's App is a session App (``session_apps``) and it names a person;
* its ``api_key_name`` is "<client_name> (MCP)" for a connection of that person;
* the connection existed at the time: created before the request, and not
  revoked before it. Revoked connections count, so history of a connection that
  has since ended is kept. Two connections that fit (the same client approved
  twice in overlapping time) leave the receipt untagged.

Finished receipts only, never ``in_progress`` ones, and only where
``connection_id`` is not yet set, so a second run changes nothing.

Dry run is the default and opens both databases read only. ``--apply`` writes
receipts.db in short batches; the admin database is never written. Take a
backup of receipts.db first.

    python scripts/migrations/receipt_connection_ids.py [--admin-db PATH] [--receipts-db PATH] [--apply]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP_SUFFIX = " (MCP)"
BATCH = 500


@dataclass(frozen=True)
class Grant:
    id: str
    person_id: str
    client_name: str
    created_at: int
    revoked_at: int | None

    def existed_at(self, when: int) -> bool:
        return self.created_at <= when and (self.revoked_at is None or when <= self.revoked_at)


@dataclass
class Plan:
    examined: int = 0
    tagged: dict[str, str] = field(default_factory=dict)  # request_id -> grant id
    ambiguous: int = 0
    no_grant: int = 0
    per_grant: Counter = field(default_factory=Counter)


def read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def load_people_and_grants(admin_db: Path) -> tuple[dict[str, str], list[Grant]]:
    """The person behind each session App, and every connection ever made."""
    with closing(read_connection(admin_db)) as conn:
        people = {app_id: user_id for app_id, user_id in conn.execute("SELECT app_id, user_id FROM session_apps")}
        has_grants = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_oauth_grants'").fetchone()
        grants = [Grant(*row) for row in conn.execute(
            "SELECT id, person_id, client_name, created_at, revoked_at FROM mcp_oauth_grants")] if has_grants else []
    return people, grants


def plan(admin_db: Path, receipts_db: Path) -> Plan:
    people, grants = load_people_and_grants(admin_db)
    by_person_name: dict[tuple[str, str], list[Grant]] = {}
    for grant in grants:
        by_person_name.setdefault((grant.person_id, grant.client_name + MCP_SUFFIX), []).append(grant)
    result = Plan()
    if not people:
        return result
    with closing(read_connection(receipts_db)) as conn:
        rows = conn.execute(
            "SELECT request_id, created_at, app_id, api_key_name FROM receipts "
            "WHERE api_key_name LIKE '% (MCP)' AND status != 'in_progress' "
            "AND json_extract(receipt_json, '$.connection_id') IS NULL "
            "ORDER BY created_at, request_id"
        ).fetchall()
    for request_id, created_at, app_id, api_key_name in rows:
        person = people.get(app_id)
        if person is None:
            continue  # not a session App: a key, not a sign-in
        result.examined += 1
        fits = [g for g in by_person_name.get((person, api_key_name), []) if g.existed_at(int(created_at))]
        if len(fits) == 1:
            result.tagged[request_id] = fits[0].id
            result.per_grant[fits[0].id] += 1
        elif fits:
            result.ambiguous += 1
        else:
            result.no_grant += 1
    return result


def apply(receipts_db: Path, tagged: dict[str, str]) -> int:
    changed = 0
    conn = sqlite3.connect(receipts_db, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        items = sorted(tagged.items())
        for start in range(0, len(items), BATCH):
            conn.execute("BEGIN IMMEDIATE")
            for request_id, grant_id in items[start:start + BATCH]:
                changed += conn.execute(
                    "UPDATE receipts SET receipt_json = json_set(receipt_json, '$.connection_id', ?) "
                    "WHERE request_id = ? AND status != 'in_progress' "
                    "AND json_extract(receipt_json, '$.connection_id') IS NULL",
                    (grant_id, request_id),
                ).rowcount
            conn.execute("COMMIT")
    finally:
        conn.close()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--admin-db", type=Path, default=ROOT / "data" / "admin.db")
    parser.add_argument("--receipts-db", type=Path, default=ROOT / "data" / "receipts.db")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args(argv)
    result = plan(args.admin_db, args.receipts_db)
    print(f"{'apply' if args.apply else 'dry run'}: {args.receipts_db}")
    print(f"sign-in receipts without a connection: {result.examined}")
    print(f"  to tag with one connection: {len(result.tagged)}")
    print(f"  several connections fit, left alone: {result.ambiguous}")
    print(f"  no connection fits, left alone: {result.no_grant}")
    for grant_id, count in sorted(result.per_grant.items()):
        print(f"  {grant_id}: {count}")
    if args.apply:
        print(f"tagged: {apply(args.receipts_db, result.tagged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
