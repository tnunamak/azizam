"""Reviewed transition profiles (design D3), read from ``registry/transition_profiles/*.yml``.

A profile is reviewed data. It names the operations an engine's protocol description declares for ``idle``,
``resident``, ``release``, ``load`` and ``query``, and, when the profile drives snapshots, for ``checkpoint``,
``restore``, ``reset`` and ``compatibility``; it also names the observations the adapter answers, as observation
name -> protocol operation name. It says whether a successful release is attested. An owner picks a
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
OPERATION_KEYS: frozenset[str] = frozenset({"idle", "resident", "release", "load", "query", "checkpoint", "restore", "reset", "compatibility"})
REQUIRED_OPERATIONS: frozenset[str] = frozenset({"resident", "release", "load", "query"})
REVIEWED_POLICY_KEYS: frozenset[str] = frozenset({"release", "load", "checkpoint", "restore", "servable_check", "placement_keys", "fresh_within_s"})
"""Reviewed profile blocks the executor and the snapshot steps read through ``StepContext.policy["profile"]``. They pass through
unchanged: this module does not interpret them."""
FAILURE_SEMANTICS: frozenset[str] = frozenset({"no_change", "unknown"})
"""What a declared ``failed`` outcome of an effect operation means. ``no_change``: the engine is as it was before the
call. ``unknown`` (the default when absent): the call may have changed it."""


def attests_no_change(profile: Mapping[str, Any], block: str) -> bool:
    """True when ``profile`` (a ``Profile.declared`` or ``as_policy()`` mapping) declares ``failure_semantics:
    no_change`` for ``block``. ``restore.reset`` is the reset block inside ``restore``."""
    node: Any = profile
    for part in block.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return isinstance(node, Mapping) and node.get("failure_semantics") == "no_change"


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
    observations: Mapping[str, str] = field(default_factory=dict)
    """Observation name -> the operation name in the engine's protocol description."""

    def attests_no_change(self, block: str) -> bool:
        """Does the profile declare ``failure_semantics: no_change`` for ``block`` (release, load, checkpoint, restore
        or restore.reset)? Absent means unknown."""
        return attests_no_change(self.declared, block)

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
    observations = raw.get("observations", {})
    if not isinstance(observations, Mapping):
        raise ProfileError(f"{source}: observations is a mapping of observation name to protocol operation name")
    observed: dict[str, str] = {}
    for key, name in observations.items():
        if not isinstance(key, str) or not key.strip():
            raise ProfileError(f"{source}: observations needs a non-empty observation name")
        if not isinstance(name, str) or not name.strip():
            raise ProfileError(f"{source}: observations {key!r} needs a protocol operation name")
        observed[key.strip()] = name.strip()
    declared = {key: raw[key] for key in REVIEWED_POLICY_KEYS if key in raw}
    if ("checkpoint" in names) != ("restore" in names):
        raise ProfileError(f"{source}: operations 'checkpoint' and 'restore' are declared together or not at all")
    if "reset" in names and "restore" not in names:
        raise ProfileError(f"{source}: operation 'reset' needs 'restore'")
    if "checkpoint" in names and "compatibility" not in names:
        raise ProfileError(f"{source}: operation 'compatibility' is required when 'checkpoint' is declared")
    for key in ("release", "load", "checkpoint", "restore"):
        if key in declared and not isinstance(declared[key], Mapping):
            raise ProfileError(f"{source}: {key} is a mapping")
    if "checkpoint" in declared and "checkpoint" not in names:
        raise ProfileError(f"{source}: checkpoint needs the 'checkpoint' operation")
    restore_block = declared.get("restore", {})
    if "restore" in declared and "restore" not in names:
        raise ProfileError(f"{source}: restore needs the 'restore' operation")
    reset_block = restore_block.get("reset")
    if "reset" in restore_block:
        if not isinstance(reset_block, Mapping):
            raise ProfileError(f"{source}: restore.reset is a mapping")
        if "reset" not in names:
            raise ProfileError(f"{source}: restore.reset needs the 'reset' operation")
    for where, block in (("release", declared.get("release")), ("load", declared.get("load")),
                         ("checkpoint", declared.get("checkpoint")), ("restore", restore_block),
                         ("restore.reset", reset_block)):
        value = block.get("failure_semantics", "unknown") if isinstance(block, Mapping) else "unknown"
        if not isinstance(value, str) or value not in FAILURE_SEMANTICS:  # a string first: a list or dict is unhashable
            raise ProfileError(f"{source}: {where}.failure_semantics is one of {', '.join(sorted(FAILURE_SEMANTICS))}")
    named = {}
    if "servable_check" in raw:
        named["servable_check"] = raw["servable_check"]
    if isinstance(reset_block, Mapping) and "verify" in reset_block:
        named["restore.reset.verify"] = reset_block["verify"]
    for field, value in named.items():
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{source}: {field} names an observation")
        if value != value.strip():
            raise ProfileError(f"{source}: {field} must not contain surrounding whitespace")
        if value not in observed:
            raise ProfileError(f"{source}: {field} names an unknown observation {value!r}")
    return Profile(id=profile_id.strip(), title=str(raw.get("title") or profile_id), operations=names, attested=attested,
                   declared=declared, observations=observed)


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
