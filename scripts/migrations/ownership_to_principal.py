#!/usr/bin/env python3
"""Migrate persisted credential-hash ownership to stable principal ownership.

Dry-run is the default.  Always pass explicit fixture paths while developing;
the defaults exist only for an operator running this deliberately.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


from gateway.ownership import principal_key_for_credential
from gateway.custom_voices import owner_dir_name as owner_dir
from gateway import private_io


def readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def json_owner_rewrite(value: str, principals: dict[str, str]) -> tuple[str, bool]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ownership JSON is invalid") from exc
    if not isinstance(data, dict):
        return value, False
    changed = False
    for field in ("owner_key_hash",):
        current = data.get(field)
        if isinstance(current, str) and current in principals:
            data[field] = principals[current]
            changed = True
    current = data.get("owner_key_hashes")
    if isinstance(current, list):
        replaced = [principals.get(item, item) if isinstance(item, str) else item for item in current]
        if replaced != current:
            unique: list[Any] = []
            for item in replaced:
                if item not in unique:
                    unique.append(item)
            replaced = unique
        if replaced != current:
            data["owner_key_hashes"] = replaced
            changed = True
    return (json.dumps(data, sort_keys=True, separators=(",", ":")), changed)


def unknown_owners(value: str, principals: dict[str, str]) -> set[str]:
    """Return legacy hashes present in an ownership JSON object, without logging JSON."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    values = [data.get("owner_key_hash"), *(data.get("owner_key_hashes") or [])]
    return {
        item for item in values
        if isinstance(item, str) and item not in principals
        and not item.startswith(("app:", "user:", "service_account:"))
    }


@dataclass(frozen=True)
class DbChange:
    db: Path
    table: str
    key_column: str
    key: str
    json_column: str
    value: str
    owner_column: str | None = None
    owner_value: str | None = None


@dataclass(frozen=True)
class MetaChange:
    path: Path
    value: str


@dataclass
class Plan:
    db_changes: list[DbChange] = field(default_factory=list)
    meta_changes: list[MetaChange] = field(default_factory=list)
    voice_sources: dict[Path, Path] = field(default_factory=dict)
    voice_destinations: set[Path] = field(default_factory=set)
    unknown_voice_dirs: list[Path] = field(default_factory=list)
    unknown_hashes: set[str] = field(default_factory=set)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.db_changes or self.meta_changes or self.voice_sources)


def credential_principals(admin_db: Path) -> dict[str, str]:
    with closing(readonly_connection(admin_db)) as conn:
        rows = conn.execute("SELECT subject_kind, subject_id, app_id, hash FROM credentials").fetchall()
    mapping: dict[str, str] = {}
    for kind, subject_id, app_id, digest in rows:
        principal = principal_key_for_credential({"subject_kind": kind, "subject_id": subject_id, "app_id": app_id})
        if isinstance(digest, str) and principal:
            mapping[digest] = principal
    return mapping


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def plan_json_db(path: Path, table: str, key_column: str, json_column: str,
                 principals: dict[str, str], *, owner_column: str | None = None,
                 unknown_hashes: set[str] | None = None) -> list[DbChange]:
    if not path.exists():
        return []
    with closing(readonly_connection(path)) as conn:
        if not table_exists(conn, table):
            return []
        fields = f"{key_column}, {json_column}" + (f", {owner_column}" if owner_column else "")
        rows = conn.execute(f"SELECT {fields} FROM {table}").fetchall()
    changes: list[DbChange] = []
    for row in rows:
        key, original = row[:2]
        if unknown_hashes is not None:
            unknown_hashes.update(unknown_owners(original, principals))
        value, changed = json_owner_rewrite(original, principals)
        owner_value = None
        if owner_column and unknown_hashes is not None and isinstance(row[2], str):
            unknown_hashes.update(unknown_owners(json.dumps({"owner_key_hash": row[2]}), principals))
        if owner_column and isinstance(row[2], str) and row[2] in principals:
            owner_value = principals[row[2]]
            changed = True
        if changed:
            changes.append(DbChange(path, table, key_column, str(key), json_column, value, owner_column, owner_value))
    return changes


def trees_identical(left: Path, right: Path) -> bool:
    left_files = sorted(p.relative_to(left) for p in left.rglob("*") if p.is_file())
    right_files = sorted(p.relative_to(right) for p in right.rglob("*") if p.is_file())
    return left_files == right_files and all((left / item).read_bytes() == (right / item).read_bytes() for item in left_files)


def plan_voice_moves(root: Path, principals: dict[str, str], plan: Plan) -> None:
    if not root.is_dir():
        return
    source_dirs = {owner_dir(digest): (digest, principal) for digest, principal in principals.items()}
    known_dirs = set(source_dirs) | {owner_dir(principal) for principal in principals.values()}
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name == "local":
            continue
        if entry.name not in known_dirs:
            plan.unknown_voice_dirs.append(entry)
    by_principal: dict[str, list[Path]] = {}
    for digest, principal in principals.items():
        source = root / owner_dir(digest)
        if source.is_dir():
            by_principal.setdefault(principal, []).append(source)
    for principal, sources in by_principal.items():
        destination = root / owner_dir(principal)
        plan.voice_destinations.add(destination)
        # Compare individual records before any move.  A collision is a hard stop:
        # preserving all originals is safer than a partial migration.
        seen: dict[tuple[str, str], Path] = {}
        for base in ([destination] if destination.is_dir() else []) + sources:
            for kind in ("voices", "consents"):
                kind_dir = base / kind
                if not kind_dir.is_dir():
                    continue
                for record in kind_dir.iterdir():
                    if not record.is_dir():
                        continue
                    label = (kind, record.name)
                    earlier = seen.get(label)
                    if earlier is not None and not trees_identical(earlier, record):
                        plan.conflicts.append(f"conflicting {kind[:-1]} id {record.name} in {earlier} and {record}")
                    else:
                        seen[label] = record
        for source in sources:
            if source != destination:
                plan.voice_sources[source] = destination


def plan_metadata(cache_dir: Path, principals: dict[str, str]) -> tuple[list[MetaChange], set[str]]:
    changes: list[MetaChange] = []
    unknown: set[str] = set()
    if not cache_dir.is_dir():
        return changes, unknown
    for path in sorted(cache_dir.glob("*.meta.json")):
        original = path.read_text(encoding="utf-8")
        value, changed = json_owner_rewrite(original, principals)
        try:
            data = json.loads(original)
        except ValueError:
            continue
        if isinstance(data, dict):
            unknown.update(unknown_owners(original, principals))
        if changed:
            changes.append(MetaChange(path, value))
    return changes, unknown


def build_plan(data_dir: Path, cache_dir: Path, admin_db: Path) -> Plan:
    principals = credential_principals(admin_db)
    plan = Plan()
    plan.db_changes.extend(plan_json_db(data_dir / "media_jobs.db", "media_jobs", "job_id", "job_json", principals, unknown_hashes=plan.unknown_hashes))
    plan.db_changes.extend(plan_json_db(data_dir / "receipts.db", "receipts", "request_id", "receipt_json", principals, unknown_hashes=plan.unknown_hashes))
    plan.db_changes.extend(plan_json_db(data_dir / "media_jobs.db", "video_job_records", "prompt_id", "record_json", principals, owner_column="owner_key_hash", unknown_hashes=plan.unknown_hashes))
    plan.meta_changes, metadata_unknown = plan_metadata(cache_dir, principals)
    plan.unknown_hashes.update(metadata_unknown)
    plan_voice_moves(data_dir / "voices", principals, plan)
    return plan


def backup_sqlite(source: Path, target: Path) -> None:
    private_io.ensure_private_dir(target.parent)
    if not target.exists():
        private_io.create_private_file(target)
    with closing(readonly_connection(source)) as read, closing(sqlite3.connect(target)) as write:
        read.backup(write)


def create_backup(plan: Plan, root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    root = root / "migration-backups"
    private_io.ensure_private_dir(root)
    backup = root / f"ownership-to-principal.backup.{stamp}"
    private_io.ensure_private_dir(backup)
    for db in sorted({change.db for change in plan.db_changes}):
        backup_sqlite(db, backup / "db" / db.name)
    for source in sorted(set(plan.voice_sources) | plan.voice_destinations):
        if source.is_dir():
            destination = backup / "voices" / source.name
            private_io.ensure_private_dir(destination.parent)
            private_io.copy_private_tree(source, destination)
    for change in plan.meta_changes:
        destination = backup / "cache-images" / change.path.name
        private_io.ensure_private_dir(destination.parent)
        private_io.atomic_copy_file(change.path, destination)
    return backup


def apply_voice_move(source: Path, destination: Path) -> None:
    private_io.ensure_private_dir(destination)
    for kind in ("voices", "consents"):
        old_kind, new_kind = source / kind, destination / kind
        if not old_kind.is_dir():
            continue
        private_io.ensure_private_dir(new_kind)
        for record in list(old_kind.iterdir()):
            target = new_kind / record.name
            if not target.exists():
                shutil.move(str(record), str(target))
            elif trees_identical(record, target):
                shutil.rmtree(record)
            else:  # The planner made this impossible; never overwrite anyway.
                raise RuntimeError(f"voice conflict appeared during apply: {record}")
        if not any(old_kind.iterdir()):
            old_kind.rmdir()
    if not any(source.iterdir()):
        source.rmdir()


def apply_plan(plan: Plan, data_dir: Path) -> Path | None:
    if plan.conflicts:
        raise RuntimeError("voice conflicts prevent apply")
    if not plan.changed:
        return None
    backup = create_backup(plan, data_dir)
    grouped: dict[Path, list[DbChange]] = {}
    for change in plan.db_changes:
        grouped.setdefault(change.db, []).append(change)
    for db, changes in grouped.items():
        with closing(sqlite3.connect(db)) as conn:
            with conn:
                for change in changes:
                    if change.owner_column and change.owner_value is not None:
                        conn.execute(f"UPDATE {change.table} SET {change.json_column}=?, {change.owner_column}=? WHERE {change.key_column}=?", (change.value, change.owner_value, change.key))
                    else:
                        conn.execute(f"UPDATE {change.table} SET {change.json_column}=? WHERE {change.key_column}=?", (change.value, change.key))
    for change in plan.meta_changes:
        private_io.atomic_write_text(change.path, change.value)
    for source, destination in plan.voice_sources.items():
        if source.is_dir():
            apply_voice_move(source, destination)
    return backup


def report(plan: Plan, *, apply: bool, backup: Path | None = None) -> None:
    print("APPLY" if apply else "DRY RUN (no writes)")
    print(f"database records: {len(plan.db_changes)}")
    print(f"cache metadata: {len(plan.meta_changes)}")
    print(f"voice owner folders: {len(plan.voice_sources)}")
    for change in plan.db_changes:
        print(f"rewrite owner: {change.db.name}/{change.table}/{change.key}")
    for change in plan.meta_changes:
        print(f"rewrite cache owners: {change.path}")
    for source, destination in sorted(plan.voice_sources.items()):
        print(f"merge voice owner: {source} -> {destination}")
    for digest in sorted(plan.unknown_hashes):
        print(f"unknown owner hash left untouched: {digest}")
    for path in sorted(plan.unknown_voice_dirs):
        print(f"unknown voice folder left untouched: {path}")
    for conflict in plan.conflicts:
        print(f"CONFLICT: {conflict}", file=sys.stderr)
    if backup:
        print(f"Backup: {backup}")
    print(f"{sum((len(plan.db_changes), len(plan.meta_changes), len(plan.voice_sources)))} changes {'applied' if apply else 'to apply'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "cache" / "images")
    parser.add_argument("--admin-db", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        data_dir, cache_dir, admin_db = (item.expanduser().resolve() for item in (args.data_dir, args.cache_dir, args.admin_db or args.data_dir / "admin.db"))
        plan = build_plan(data_dir, cache_dir, admin_db)
        if plan.conflicts:
            report(plan, apply=False)
            return 1
        backup = apply_plan(plan, data_dir) if args.apply else None
        report(plan, apply=args.apply, backup=backup)
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
