"""Hide secrets in the commands and environments of engine records.

A record may hold a start command with a credential argument and an environment
with secret values. The gateway needs the stored values to run the command; no
caller needs to read them back. Every view of a record shows the shape and the
marker instead, and a save that sends the marker back keeps the stored value:

- ``redact_*`` turn a stored value into its public form.
- ``restore_*`` turn a public form that came back from a caller into a value to
  store, taking each marker's value from the stored record. A marker with no
  stored value raises ``ValueError``: the caller cannot invent a secret by
  sending the marker.

The rules read the command, never an engine's name: an argument is secret when
it follows a flag that names a credential, sits after ``=`` in such a flag or in
``NAME=value``, or has the shape of a key.
"""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any, Iterable, Mapping

from gateway.provenance import looks_like_credential

REDACTED = "[redacted]"

# A flag or assignment name that names a credential, either case.
_CREDENTIAL_NAME = re.compile(
    r"token|secret|passw|(?<![a-z])pass(?:phrase)?(?![a-z])|(?<![a-z])pwd|cookie|bearer|credential|apikey"
    r"|auth(?![a-z])|(?<![a-z])key|key(?![a-z])",
    re.IGNORECASE,
)
# Short flags that conventionally carry a key.
_CREDENTIAL_SHORT_FLAGS = frozenset({"-k", "-K"})
# Flags whose value is a credential when it is a ``user:password`` pair.
_USER_FLAGS = frozenset({"-u", "--user"})

_COMMAND_KEYS = ("start", "stop", "status")
# The process fields the public view may show; any other field stays stored only.
PUBLIC_PROCESS_KEYS = (
    "driver", "legacy_driver", "needs_owner_conversion", "start", "stop", "status", "profile_apply",
    "port", "cwd", "env", "ready_timeout_s", "stop_timeout_s", "restart", "log_max_bytes",
)

# Where a command or an environment can sit in a stored service record.
_PROCESS_PATHS = (("process",), ("config", "process"))
_LIFECYCLE_PATHS = (
    ("lifecycle", "driver_config"), ("config", "lifecycle", "driver_config"),
    ("process", "legacy_lifecycle", "driver_config"), ("config", "process", "legacy_lifecycle", "driver_config"),
)


def _secret_slots(argv: list[Any]) -> dict[int, str]:
    """Index -> ``"whole"`` (the argument) or ``"value"`` (the part after ``=``)."""
    slots: dict[int, str] = {}
    for index, arg in enumerate(argv):
        if not isinstance(arg, str) or arg == REDACTED:
            continue
        if "=" in arg:
            name, _, value = arg.partition("=")
            if value and (_CREDENTIAL_NAME.search(name) or (name in _USER_FLAGS and ":" in value)):
                slots[index] = "value"
                continue
        elif arg.startswith("-"):
            following = argv[index + 1] if index + 1 < len(argv) else None
            if isinstance(following, str) and not following.startswith("-"):
                if arg in _CREDENTIAL_SHORT_FLAGS or _CREDENTIAL_NAME.search(arg) or (arg in _USER_FLAGS and ":" in following):
                    slots[index + 1] = "whole"
        if index not in slots and looks_like_credential(arg):
            slots[index] = "whole"
    return slots


def _is_marker(arg: Any) -> bool:
    return isinstance(arg, str) and (arg == REDACTED or arg.endswith("=" + REDACTED))


def _slot_key(argv: list[Any], index: int, mode: str) -> str:
    """The credential a slot belongs to: the name before ``=``, else the flag just before it, else ``""``."""
    if mode == "value":
        return argv[index].partition("=")[0]
    previous = argv[index - 1] if index else None
    return previous if isinstance(previous, str) and previous.startswith("-") and "=" not in previous else ""


def redact_argv(argv: Iterable[Any]) -> list[Any]:
    out = list(argv)
    for index, mode in _secret_slots(out).items():
        out[index] = REDACTED if mode == "whole" else out[index].partition("=")[0] + "=" + REDACTED
    return out


def restore_argv(new: Iterable[Any], stored: Any) -> list[Any]:
    """Put each marker's stored value back; a marker takes the value of the same flag.

    A flag's occurrences pair in order with the stored occurrences of that flag, so removing or
    reordering other arguments cannot give a marker another flag's secret.
    """
    new_argv = list(new)
    stored_argv = list(stored) if isinstance(stored, list) else []
    pool: dict[str, list[str]] = {}
    for index, mode in sorted(_secret_slots(stored_argv).items()):
        value = stored_argv[index] if mode == "whole" else stored_argv[index].partition("=")[2]
        pool.setdefault(_slot_key(stored_argv, index, mode), []).append(value)
    slots = _secret_slots(new_argv)
    for index, arg in enumerate(new_argv):
        if _is_marker(arg):
            slots.setdefault(index, "value" if arg != REDACTED else "whole")
    seen: dict[str, int] = {}
    out = list(new_argv)
    for index, mode in sorted(slots.items()):
        key = _slot_key(new_argv, index, mode)
        position = seen[key] = seen.get(key, -1) + 1
        if not _is_marker(new_argv[index]):
            continue
        values = pool.get(key, [])
        if position >= len(values):
            raise ValueError(
                f"a redacted value after {key or 'this position'!r} has no stored value to keep; type the value again")
        out[index] = values[position] if mode == "whole" else new_argv[index][: -len(REDACTED)] + values[position]
    return out


def _tokens(text: str) -> list[str] | None:
    try:
        return shlex.split(text)
    except ValueError:
        return None


def redact_command(command: Any) -> Any:
    """A start, stop or status command as a list of arguments or as one line."""
    if isinstance(command, list):
        return redact_argv(command)
    if not isinstance(command, str) or not command:
        return command
    tokens = _tokens(command)
    if tokens is None:
        # A line that does not parse cannot be told apart; hide it whole when it looks credential-bearing.
        return REDACTED if _CREDENTIAL_NAME.search(command) or looks_like_credential(command) else command
    redacted = redact_argv(tokens)
    if redacted == tokens:
        return command
    return " ".join(
        token if token == REDACTED
        else shlex.quote(token[: -len(REDACTED)]) + REDACTED if token.endswith("=" + REDACTED)
        else shlex.quote(token)
        for token in redacted
    )


def restore_command(new: Any, stored: Any) -> Any:
    if isinstance(new, list):
        return restore_argv(new, stored)
    if not isinstance(new, str) or REDACTED not in new:
        return new
    if isinstance(stored, str) and new == redact_command(stored):
        return stored
    tokens = _tokens(new)
    if tokens is None:
        raise ValueError("the command has a redacted value and cannot be read; type the value again")
    stored_tokens = _tokens(stored) if isinstance(stored, str) else None
    restored = restore_argv(tokens, stored_tokens)
    return " ".join(shlex.quote(token) for token in restored)


def redact_env(env: Any) -> Any:
    """Names stay, values go: a set name shows the marker."""
    if isinstance(env, Mapping):
        return {name: REDACTED for name in env}
    return REDACTED if env else env


def restore_env(new: Any, stored: Any) -> Any:
    if isinstance(new, Mapping):
        out = dict(new)
        for name, value in new.items():
            if value == REDACTED:
                if not isinstance(stored, Mapping) or name not in stored:
                    raise ValueError(f"environment value {name!r} has no stored value to keep; type the value again")
                out[name] = stored[name]
        return out
    if new == REDACTED:
        return copy.deepcopy(stored) if stored else new
    return new


def _redact_block(block: dict[str, Any]) -> None:
    for key in _COMMAND_KEYS:
        if key in block:
            block[key] = redact_command(block[key])
    if "env" in block:
        block["env"] = redact_env(block["env"])
    apply = block.get("profile_apply")
    if isinstance(apply, dict) and "reload" in apply:
        apply["reload"] = redact_command(apply["reload"])


def _restore_block(new: dict[str, Any], stored: Mapping[str, Any]) -> None:
    for key in _COMMAND_KEYS:
        if key in new:
            new[key] = restore_command(new[key], stored.get(key))
    if "env" in new:
        new["env"] = restore_env(new["env"], stored.get("env"))
    apply = new.get("profile_apply")
    if isinstance(apply, dict) and "reload" in apply:
        stored_apply = stored.get("profile_apply")
        apply["reload"] = restore_command(apply["reload"], stored_apply.get("reload") if isinstance(stored_apply, Mapping) else None)


def _walk(record: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(record, Mapping):
            return None
        record = record.get(part)
    return record if isinstance(record, dict) else None


def redact_process(process: Mapping[str, Any]) -> dict[str, Any]:
    """A process block with its commands and environment hidden; nothing else changes."""
    out = copy.deepcopy(dict(process))
    _redact_block(out)
    return out


def public_process(process: Mapping[str, Any]) -> dict[str, Any]:
    """The process fields a caller may see: an allow-list, redacted."""
    redacted = redact_process(process)
    return {key: redacted[key] for key in PUBLIC_PROCESS_KEYS if key in redacted}


def redact_service(record: Mapping[str, Any]) -> dict[str, Any]:
    """A service record as a caller sees it: same shape, no secret in a command or environment."""
    out = copy.deepcopy(dict(record))
    for path in _PROCESS_PATHS + _LIFECYCLE_PATHS:
        block = _walk(out, path)
        if block is not None:
            _redact_block(block)
    return out


def redact_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(registry)
    services = registry.get("services")
    if isinstance(services, Mapping):
        out["services"] = {key: redact_service(row) if isinstance(row, Mapping) else row for key, row in services.items()}
    return out


def _stored_block(stored: Mapping[str, Any] | None, path: tuple[str, ...]) -> Mapping[str, Any]:
    """The stored block for ``path``, or the same kind of block where the stored record keeps it."""
    if stored is None:
        return {}
    candidates = _PROCESS_PATHS if path in _PROCESS_PATHS else _LIFECYCLE_PATHS
    for candidate in (path, *candidates):
        block = _walk(stored, candidate)
        if block is not None:
            return block
    return {}


def restore_registry(new: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """A registry from a caller with each marker replaced by the stored value it stands for."""
    out = copy.deepcopy(dict(new))
    new_services = out.get("services")
    stored_services = stored.get("services") if isinstance(stored, Mapping) else None
    if not isinstance(new_services, dict):
        return out
    for service_id, record in new_services.items():
        if not isinstance(record, dict):
            continue
        before = stored_services.get(service_id) if isinstance(stored_services, Mapping) else None
        try:
            for path in _PROCESS_PATHS + _LIFECYCLE_PATHS:
                block = _walk(record, path)
                if block is not None:
                    _restore_block(block, _stored_block(before, path))
        except ValueError as exc:
            raise ValueError(f"service {service_id!r}: {exc}") from None
    return out
