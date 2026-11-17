"""Private, versioned last-known-good choice snapshots; no refresh policy."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path

from gateway import private_io


def choices_from_node_info(info, input_name):
    """Extract a complete choice list without changing the supplied definition."""
    def invalid():
        return ValueError(f"invalid choices for input {input_name}")

    if not isinstance(info, Mapping) or not isinstance(info.get("input"), Mapping):
        raise invalid()
    inputs = info["input"]
    required = inputs.get("required", {})
    optional = inputs.get("optional", {})
    if not isinstance(required, Mapping) or not isinstance(optional, Mapping):
        raise invalid()
    if input_name in required:
        entry = required[input_name]
    elif input_name in optional:
        entry = optional[input_name]
    else:
        raise invalid()
    if not isinstance(entry, list) or not entry:
        raise invalid()
    if isinstance(entry[0], list):
        options = entry[0]
    elif entry[0] == "COMBO" and len(entry) >= 2 and isinstance(entry[1], Mapping):
        options = entry[1].get("options")
    else:
        raise invalid()
    if (not isinstance(options, list) or not options or
            not all(isinstance(value, str) and value for value in options) or
            len(set(options)) != len(options)):
        raise invalid()
    return list(options)


@dataclass(frozen=True)
class Snapshot:
    choices: list[str]
    fetched_at: float

    def stale(self, *, now: float, max_age: float) -> bool:
        if not math.isfinite(now) or not math.isfinite(max_age) or max_age < 0:
            raise ValueError('invalid age')
        return now < self.fetched_at or now - self.fetched_at >= max_age


class ChoiceStore:
    """One file per installation. Writes serialize on a sibling lock file; reads are lock-free."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise ValueError('unreadable choice store') from exc
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
            raise ValueError('invalid choice store schema')
        seen = set()
        for entry in data['entries']:
            if not isinstance(entry, dict) or set(entry) != {'service', 'node_type', 'input', 'choices', 'fetched_at'}:
                raise ValueError('invalid choice entry')
            self._validate(entry['service'], entry['node_type'], entry['input'], entry['choices'], entry['fetched_at'])
            key = (entry['service'], entry['node_type'], entry['input'])
            if key in seen:
                raise ValueError('duplicate choice entry')
            seen.add(key)
        return data['entries']

    @staticmethod
    def _validate(service, node_type, input, choices, fetched_at):
        if not all(isinstance(x, str) and x for x in (service, node_type, input)):
            raise ValueError('invalid choice key')
        if (not isinstance(choices, list) or not choices or
                not all(isinstance(x, str) for x in choices) or len(set(choices)) != len(choices)):
            raise ValueError('invalid choices')
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)) or not math.isfinite(fetched_at) or fetched_at < 0:
            raise ValueError('invalid timestamp')

    def read(self, service, node_type, input):
        try:
            entries = self._load()
        except ValueError:
            return None
        for entry in entries:
            if (entry['service'], entry['node_type'], entry['input']) == (service, node_type, input):
                return Snapshot(entry['choices'], entry['fetched_at'])
        return None

    @contextmanager
    def _locked(self):
        private_io.ensure_private_dir(self.path.parent)
        # Lock a stable sibling: atomic replace changes the store file's inode.
        lock_fd = os.open(self.path.with_name(self.path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_fd)

    def write(self, service, node_type, input, choices, *, fetched_at):
        self._validate(service, node_type, input, choices, fetched_at)
        with self._locked():
            entries = self._load()
            entries = [e for e in entries if (e['service'], e['node_type'], e['input']) != (service, node_type, input)]
            entries.append(dict(service=service, node_type=node_type, input=input, choices=choices, fetched_at=fetched_at))
            private_io.atomic_write_text(self.path, json.dumps({'version': 1, 'entries': entries}, allow_nan=False))

    # Refresh failures live beside the snapshots so the snapshot schema stays unchanged.
    @property
    def failures_path(self):
        return self.path.with_name(self.path.name + '.failures')

    def _load_failures(self):
        try:
            data = json.loads(self.failures_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _failure_key(service, node_type, input):
        return json.dumps([service, node_type, input])

    def read_failure(self, service, node_type, input):
        """The last refresh failure as ``{'at', 'reason'}``, or None."""
        entry = self._load_failures().get(self._failure_key(service, node_type, input))
        if isinstance(entry, dict) and set(entry) == {'at', 'reason'}:
            return entry
        return None

    def _update_failures(self, key, entry):
        with self._locked():
            failures = self._load_failures()
            if entry is None and key not in failures:
                return
            if entry is None:
                del failures[key]
            else:
                failures[key] = entry
            private_io.atomic_write_text(self.failures_path, json.dumps(failures, allow_nan=False))

    def record_failure(self, service, node_type, input, reason, *, at):
        if not all(isinstance(x, str) and x for x in (service, node_type, input, reason)):
            raise ValueError('invalid failure record')
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise ValueError('invalid timestamp')
        self._update_failures(self._failure_key(service, node_type, input), {'at': at, 'reason': reason})

    def clear_failure(self, service, node_type, input):
        self._update_failures(self._failure_key(service, node_type, input), None)
