"""Reviewed transition profiles (design D3), read from ``registry/transition_profiles/*.yml``.

A profile is reviewed data. It names the operations an engine's protocol description declares for ``idle``,
``resident``, ``release``, ``load`` and ``query``, and whether a successful release is attested. An owner picks a
profile by id (``residency.transition.profile``); the owner cannot set ``attested`` or the operations
(``transition_gate.forbidden_profile_keys``). No engine name appears in a profile or in this module.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

PROFILE_DIR = Path(__file__).resolve().parent.parent / "registry" / "transition_profiles"
OPERATION_KEYS: frozenset[str] = frozenset({"idle", "resident", "release", "load", "query"})
REQUIRED_OPERATIONS: frozenset[str] = frozenset({"resident", "release", "load", "query"})
REVIEWED_POLICY_KEYS: frozenset[str] = frozenset({"checkpoint", "restore", "servable_check", "placement_keys", "fresh_within_s"})
"""Reviewed profile blocks the snapshot steps (T1.3) read through ``StepContext.policy["profile"]``. They pass through
unchanged: this module does not interpret them."""


class ProfileError(ValueError):
    """A profile file that cannot be used. The message names the file and the field."""


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    operations: Mapping[str, str]
    """Profile operation -> the operation name in the engine's protocol description."""
    attested: bool
    declared: Mapping[str, Any] = field(default_factory=dict)
    """The ``REVIEWED_POLICY_KEYS`` blocks the file declares, as written."""

    def as_policy(self) -> Mapping[str, Any]:
        """The reviewed profile as ``StepContext.policy["profile"]``: the only place attested fields are read from."""
        return {"id": self.id, "attested": self.attested, "operations": dict(self.operations), **self.declared}


def parse_profile(raw: Any, *, source: str) -> Profile:
    if not isinstance(raw, Mapping):
        raise ProfileError(f"{source}: a profile is a mapping")
    if raw.get("profile_version") != 1:
        raise ProfileError(f"{source}: profile_version must be 1")
    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProfileError(f"{source}: id is required")
    operations = raw.get("operations")
    if not isinstance(operations, Mapping):
        raise ProfileError(f"{source}: operations is required")
    unknown = sorted(set(map(str, operations)) - OPERATION_KEYS)
    if unknown:
        raise ProfileError(f"{source}: unknown operation {unknown[0]!r}")
    missing = sorted(REQUIRED_OPERATIONS - set(map(str, operations)))
    if missing:
        raise ProfileError(f"{source}: operation {missing[0]!r} is required")
    names: dict[str, str] = {}
    for key, name in operations.items():
        if not isinstance(name, str) or not name.strip():
            raise ProfileError(f"{source}: operation {key!r} needs a protocol operation name")
        names[str(key)] = name.strip()
    attested = raw.get("attested", False)
    if not isinstance(attested, bool):
        raise ProfileError(f"{source}: attested is true or false")
    declared = {key: raw[key] for key in REVIEWED_POLICY_KEYS if key in raw}
    return Profile(id=profile_id.strip(), title=str(raw.get("title") or profile_id), operations=names, attested=attested,
                   declared=declared)


_CACHE: dict[Path, tuple[tuple[tuple[str, int, int], ...], dict[str, Profile]]] = {}
_LOCK = threading.Lock()


def load_profiles(directory: Optional[Path] = None) -> dict[str, Profile]:
    """Every valid profile in ``directory``, by id. A file that does not parse is skipped (it names no profile).
    Read again when a file changes."""
    root = Path(directory) if directory is not None else PROFILE_DIR
    files = sorted(root.glob("*.yml")) if root.is_dir() else []
    signature = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files)
    with _LOCK:
        cached = _CACHE.get(root)
        if cached is not None and cached[0] == signature:
            return cached[1]
    found: dict[str, Profile] = {}
    for path in files:
        try:
            profile = parse_profile(yaml.safe_load(path.read_text(encoding="utf-8")), source=path.name)
        except (OSError, yaml.YAMLError, ProfileError):
            continue
        found.setdefault(profile.id, profile)
    with _LOCK:
        _CACHE[root] = (signature, found)
    return found


def known(profile_id: str, directory: Optional[Path] = None) -> bool:
    return profile_id in load_profiles(directory)


def get(profile_id: str, directory: Optional[Path] = None) -> Optional[Profile]:
    return load_profiles(directory).get(profile_id)
