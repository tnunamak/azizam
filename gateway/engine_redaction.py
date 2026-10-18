"""One public projection of stored service records, and the restore that is its inverse.

A record holds secrets: a credential argument in a start command, an environment value, a header
value, an address with a user name or a query token. The gateway needs the stored values to run
an engine; no caller needs to read them back. Every admin-readable response that carries record
content goes through this module:

- ``public_service_record``, ``public_registry`` and ``public_value`` project stored data into
  what a caller may read. The projection denies by default: no string, list or mapping leaves it
  unexamined, so a block or field added to a record tomorrow is covered without a new rule.
  Secret values are never returned. A set secret shows as a marker, which tells a caller the
  slot is set (the same idea as Grafana's ``secureJsonFields``).
- ``restore_*`` turn a projection that came back from a caller into a value to store, taking
  each marker's value from the stored record. A marker with no stored value, a marker inside a
  longer string and a marker that stands for more than the caller kept all raise ``ValueError``:
  a caller cannot invent a secret by sending the marker, and no marker is ever stored.

Two markers exist. ``[redacted]`` marks a slot a caller edits around (a command argument, an
environment value, a header value). ``<redacted>`` marks any other secret scalar. A save treats
both alike.

The rules read the data, never an engine's name: an argument is secret when it follows a flag
whose name has a credential word in it, sits after ``=`` in such a flag or in ``NAME=value``,
follows an attached short credential flag (``-pSECRET``), or has the shape of a key.
"""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from gateway.provenance import looks_like_credential

REDACTED = "[redacted]"
HIDDEN = "<redacted>"
MARKERS = (REDACTED, HIDDEN)

# A name part that makes the name a credential. A flag, an environment name or a key is split into
# parts at ``-``, ``_`` and a lower-to-upper change, so ``--api-key`` and ``--auth-token`` match and
# ``--tokenizer``, ``--max-tokens`` and ``--keyfile`` (a path) do not.
_CREDENTIAL_WORDS = frozenset({
    "token", "secret", "key", "apikey", "privatekey", "password", "passwd", "pass", "passphrase", "pwd",
    "auth", "authorization", "bearer", "credential", "credentials", "cookie", "jwt", "pin", "otp",
    "session", "sessionid", "sig", "signature", "psk",
})
# A key ending in one of these names a reference or a setting about a credential, not the credential.
_REFERENCE_TAILS = frozenset({
    "env", "ref", "refs", "id", "ids", "origin", "name", "names", "path", "paths", "file", "files",
    "header", "scheme", "source", "type", "kind", "mode", "limit", "ttl", "timeout", "count",
})
# A field that names a downstream secret by reference, not the secret.
_REFERENCE_FIELDS = frozenset({("auth_inject", "secret")})
# A header whose name looks like it carries a credential. Header values are plain text in the
# registry and the router's config file, so a key never goes there.
CREDENTIAL_HEADER_RE = re.compile(r"auth|key|token|secret|passw|cookie|session|credential|signature", re.IGNORECASE)
# Text with the shape of a key, a bearer value or an inline data blob.
_CREDENTIAL_TEXT = re.compile(r"\b(?:opk_|oak_|sk-)[A-Za-z0-9_-]+|^Bearer\s|^data:", re.IGNORECASE)
# Short flags that conventionally carry a password or key, apart (``-p SECRET``) or attached (``-pSECRET``).
_CREDENTIAL_SHORT_FLAGS = frozenset("pPkK")
# Flags whose value is a credential when it is a ``user:password`` pair.
_USER_FLAGS = frozenset({"-u", "--user"})

# Keys whose string or list value is a command.
_COMMAND_KEYS = frozenset({"start", "stop", "status", "reload", "command", "cmd", "argv", "args", "exec", "entrypoint"})
# Keys whose mapping value is an environment.
_ENV_KEYS = frozenset({"env", "environment", "env_vars"})
# The process fields the v2 record view may show; any other field stays stored only.
PUBLIC_PROCESS_KEYS = (
    "driver", "legacy_driver", "needs_owner_conversion", "start", "stop", "status", "profile_apply",
    "port", "cwd", "env", "ready_timeout_s", "stop_timeout_s", "restart", "log_max_bytes",
)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def _words(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [part for part in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if part]


def names_credential(name: str) -> bool:
    """True when a flag, an environment name or a key has a credential word in it."""
    return any(part in _CREDENTIAL_WORDS for part in _words(name))


def _key_holds_credential(key: str, parent: str) -> bool:
    words = _words(key)
    return (
        any(part in _CREDENTIAL_WORDS for part in words)
        and words[-1] not in _REFERENCE_TAILS
        and (parent.lower(), key.lower()) not in _REFERENCE_FIELDS
    )


def _is_header_key(lowered: str) -> bool:
    return lowered.endswith("headers")


# ---------------------------------------------------------------------------
# Commands: a list of arguments, or one line
# ---------------------------------------------------------------------------

_ATTACHED_SHORT = re.compile(r"^-([pPkK])([^-].*)$")
_ATTACHED_USER = re.compile(r"^-(u)([^-].*:.*)$")


def _single_dash_names_credential(flag: str) -> bool:
    """``-token`` or ``-pass``: a one-dash flag that is a credential word as a whole (``-pPW`` is not)."""
    return flag[1:].lower() in _CREDENTIAL_WORDS


def _flag_takes_credential(flag: str, following: str) -> bool:
    if flag.startswith("--"):
        return names_credential(flag[2:]) or (flag in _USER_FLAGS and ":" in following)
    return (len(flag) == 2 and (flag[1] in _CREDENTIAL_SHORT_FLAGS or (flag in _USER_FLAGS and ":" in following))) or (
        len(flag) > 2 and _single_dash_names_credential(flag))


def _secret_slots(argv: list[Any]) -> dict[int, int]:
    """Index of each secret argument -> how many leading characters stay visible (0: all of it is secret)."""
    slots: dict[int, int] = {}
    for index, arg in enumerate(argv):
        if not isinstance(arg, str) or arg == REDACTED:
            continue
        if "=" in arg:
            name, _, value = arg.partition("=")
            if value and (names_credential(name) or (name in _USER_FLAGS and ":" in value)):
                slots[index] = len(name) + 1
                continue
        elif arg.startswith("-"):
            following = argv[index + 1] if index + 1 < len(argv) else None
            if isinstance(following, str) and not following.startswith("-") and _flag_takes_credential(arg, following):
                slots[index + 1] = 0
            attached = _ATTACHED_SHORT.match(arg) or _ATTACHED_USER.match(arg)
            if attached and not _single_dash_names_credential(arg):
                slots[index] = 2
                continue
        if index not in slots and (looks_like_credential(arg) or ("://" in arg and redact_text(arg) != arg)):
            slots[index] = 0
    return slots


def _slot_key(argv: list[Any], index: int, visible: int) -> str:
    """What a slot belongs to: the flag it follows or sits in (``--token``, whether spelled ``--token X``
    or ``--token=X``), else ``""`` for a bare value."""
    if visible:
        return argv[index][:visible].rstrip("=")
    previous = argv[index - 1] if index else None
    return previous if isinstance(previous, str) and previous.startswith("-") and "=" not in previous else ""


def redact_argv(argv: Iterable[Any]) -> list[Any]:
    out = list(argv)
    for index, visible in _secret_slots(out).items():
        out[index] = out[index][:visible] + REDACTED
    return out


def _marker_slots(argv: list[Any]) -> dict[int, int]:
    """Index of each argument that holds a marker -> its visible prefix length. Raises when a marker sits inside a value."""
    found: dict[int, int] = {}
    for index, arg in enumerate(argv):
        if not isinstance(arg, str) or REDACTED not in arg and HIDDEN not in arg:
            continue
        for marker in MARKERS:
            prefix = arg[: -len(marker)] if arg.endswith(marker) else None
            if prefix is not None and (prefix == "" or prefix.endswith("=") or re.fullmatch(r"-[A-Za-z]", prefix)):
                found[index] = len(prefix)
                break
        else:
            raise ValueError("a redacted marker sits inside a longer value; send the argument whole or type it again")
    return found


def restore_argv(new: Iterable[Any], stored: Any) -> list[Any]:
    """Put each marker's stored value back. A marker takes the value of the same flag, in order.

    A flag's secrets pair in order with the stored secrets of that flag, so removing or reordering other
    arguments cannot give a marker another flag's secret, and typing a new value beside a marker leaves
    the marker its own place. When a flag has a marker and its number of secrets differs from the
    stored number, the save is refused: a removed or inserted occurrence would shift every later marker
    onto another occurrence's secret.
    """
    new_argv = list(new)
    markers = _marker_slots(new_argv)
    if not markers:
        return new_argv
    stored_argv = list(stored) if isinstance(stored, list) else []
    pool: dict[str, list[str]] = {}
    for index, visible in sorted(_secret_slots(stored_argv).items()):
        pool.setdefault(_slot_key(stored_argv, index, visible), []).append(stored_argv[index][visible:])
    slots = {**_secret_slots(new_argv), **markers}
    by_key: dict[str, list[int]] = {}
    for index, visible in sorted(slots.items()):
        by_key.setdefault(_slot_key(new_argv, index, visible), []).append(index)
    out = list(new_argv)
    for key, indexes in by_key.items():
        asked = [index for index in indexes if index in markers]
        if not asked:
            continue
        values = pool.get(key, [])
        label = key or "this position"
        if not values:
            raise ValueError(f"a redacted value after {label!r} has no stored value to keep; type the value again")
        if len(indexes) != len(values):
            raise ValueError(
                f"{label!r} has {len(values)} stored value(s) and this save has {len(indexes)}; "
                "keep the same number of values for a flag that holds a redacted one, or type the values again")
        for position, index in enumerate(indexes):
            if index in markers:
                out[index] = new_argv[index][: markers[index]] + values[position]
    return out


def _tokens(text: str) -> list[str] | None:
    try:
        return shlex.split(text)
    except ValueError:
        return None


def _join(tokens: Iterable[str]) -> str:
    return " ".join(token if token == REDACTED else shlex.quote(token) for token in tokens)


def redact_command(command: Any) -> Any:
    """A start, stop, status or reload command as a list of arguments or as one line."""
    if isinstance(command, list):
        return redact_argv(command)
    if not isinstance(command, str) or not command:
        return command
    tokens = _tokens(command)
    if tokens is None:
        # A line that does not parse cannot be told apart; hide it whole when it looks credential-bearing.
        return REDACTED if names_credential(command) or looks_like_credential(command) else command
    redacted = redact_argv(tokens)
    if redacted == tokens:
        return command
    return " ".join(
        token if token == REDACTED
        else shlex.quote(token[: -len(REDACTED)]) + REDACTED if token.endswith(REDACTED)
        else shlex.quote(token)
        for token in redacted
    )


def restore_command(new: Any, stored: Any) -> Any:
    """A command from a caller with each marker replaced by the stored value it stands for."""
    if isinstance(new, list):
        return restore_argv(new, stored)
    if not isinstance(new, str):
        return new
    if isinstance(stored, list) and new == ",".join(str(arg) for arg in redact_argv(stored)):
        # A list posted through String(): one word with commas in it, not the command the caller saw.
        raise ValueError("the command arrived as one comma-joined line; send it as a list of arguments")
    if not new or (REDACTED not in new and HIDDEN not in new):
        return new
    if isinstance(stored, str) and new == redact_command(stored):
        return stored
    tokens = _tokens(new)
    if tokens is None:
        raise ValueError("the command has a redacted value and cannot be read; type the value again")
    stored_tokens = list(stored) if isinstance(stored, list) else _tokens(stored) if isinstance(stored, str) else None
    return _join(restore_argv(tokens, stored_tokens))


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


def redact_env(env: Any) -> Any:
    """Names stay, values go: a set name shows the marker."""
    if isinstance(env, Mapping):
        return {name: REDACTED for name in env}
    return REDACTED if env else env


def restore_env(new: Any, stored: Any) -> Any:
    if isinstance(new, Mapping):
        out = dict(new)
        for name, value in new.items():
            if value in MARKERS:
                if not isinstance(stored, Mapping) or name not in stored:
                    raise ValueError(f"environment value {name!r} has no stored value to keep; type the value again")
                out[name] = stored[name]
            elif isinstance(value, str) and (REDACTED in value or HIDDEN in value):
                raise ValueError(f"environment value {name!r} holds a redacted marker inside a longer value; type the value again")
        return out
    if new in MARKERS:
        return copy.deepcopy(stored) if stored else new
    return new


# ---------------------------------------------------------------------------
# Scalars: a header value, an address, a probe path
# ---------------------------------------------------------------------------


def redact_text(value: str, marker: str = HIDDEN) -> str:
    """A string without the credential it holds: key-shaped text becomes ``marker``; a URL loses its
    user name, password, query and fragment; a path loses its query and fragment."""
    if "://" in value:
        try:
            url = urlsplit(value)
            if url.username or url.password or url.query or url.fragment:
                host = url.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                value = f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "") + url.path
        except ValueError:
            return marker
    elif value.startswith("/") and ("?" in value or "#" in value):
        value = value.split("?", 1)[0].split("#", 1)[0]
    if _CREDENTIAL_TEXT.search(value) or looks_like_credential(value):
        return marker
    return value


def _credential_header(row: Mapping[str, Any]) -> bool:
    name = str(row.get("name") or "")
    value = row.get("value")
    return bool(CREDENTIAL_HEADER_RE.search(name)) or (
        isinstance(value, str) and (looks_like_credential(value) or redact_text(value) != value))


def redact_headers(rows: Any) -> Any:
    """Header rows with the value of each credential row hidden; names and other values stay."""
    if not isinstance(rows, list):
        return rows
    return [{**row, "value": REDACTED} if isinstance(row, Mapping) and "value" in row and _credential_header(row) else row
            for row in rows]


def _redact_header_map(headers: Mapping[str, Any], *, everything: bool) -> dict[str, Any]:
    return {name: REDACTED if everything or _credential_header({"name": name, "value": value}) else value
            for name, value in headers.items()}


def _name_key(name: Any) -> str:
    return str(name).strip().lower()


def restore_headers(new: Any, stored: Any) -> Any:
    """Rows from a caller with each marker replaced by the stored value of the row of the same name.

    Rows of one name pair in order, as the secrets of one flag do. A marker binds only to a row the
    projection hid. A marker row whose name holds no hidden stored value (a row renamed with its marker,
    a literal marker typed into a new row or into a plain header) is refused, not dropped or kept:
    either would hide from the caller that the value did not follow the name. A name with a marker row
    must keep as many rows as are stored, or a removed row would shift a later marker onto another
    row's value.
    """
    if not isinstance(new, list):
        return new
    stored_rows: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(stored, list):
        for row in stored:
            if isinstance(row, Mapping) and "value" in row:
                stored_rows.setdefault(_name_key(row.get("name")), []).append(row)
    rows_by_name: dict[str, list[int]] = {}
    marked: set[str] = set()
    for index, row in enumerate(new):
        if not isinstance(row, Mapping):
            continue
        name = _name_key(row.get("name"))
        rows_by_name.setdefault(name, []).append(index)
        value = row.get("value")
        if value in MARKERS:
            marked.add(name)
        elif isinstance(value, str) and (REDACTED in value or HIDDEN in value):
            raise ValueError(f"header {row.get('name')!r} holds a redacted marker inside a longer value; type the value again")
    out = [dict(row) if isinstance(row, Mapping) else row for row in new]
    for name in marked:
        indexes = rows_by_name[name]
        rows = stored_rows.get(name, [])
        shown_name = new[indexes[0]].get("name")
        if not any(_credential_header(row) for row in rows):
            raise ValueError(
                f"header {shown_name!r} has no hidden stored value to keep; type the value again "
                f"(the text {REDACTED!r} is reserved for hidden values)")
        if len(indexes) != len(rows):
            raise ValueError(
                f"header {shown_name!r} has {len(rows)} stored row(s) and this save has {len(indexes)}; "
                "keep the same number of rows for a name that holds a redacted value, or type the values again")
        for index, row in zip(indexes, rows):
            if new[index].get("value") in MARKERS:
                if not _credential_header(row):
                    raise ValueError(
                        f"header {shown_name!r} holds no hidden value at this row; type the value again "
                        f"(the text {REDACTED!r} is reserved for hidden values)")
                out[index]["value"] = row["value"]
    return out


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def _scalar_is_set(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)) or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _argv_secrets(argv: list[Any]) -> dict[str, str]:
    """Each secret of an argument list, labelled by the flag it follows and its order among that flag's secrets."""
    found: dict[str, str] = {}
    seen: dict[str, int] = {}
    for index, visible in sorted(_secret_slots(argv).items()):
        key = _slot_key(argv, index, visible)
        position = seen[key] = seen.get(key, -1) + 1
        found[f"{key}#{position}"] = argv[index][visible:]
    return found


def _project(value: Any, key: str = "", parent: str = "", path: str = "", found: dict[str, str] | None = None) -> Any:
    """The public form of ``value``. With ``found``, also collect every secret the projection hides, by path."""
    lowered = key.lower()

    def note(label: str, original: Any) -> None:
        if found is not None and original not in (None, ""):
            found[f"{path}{label}"] = str(original)

    if isinstance(value, Mapping):
        if lowered in _ENV_KEYS:
            for name, item in value.items():
                note(f".{name}", item)
            return redact_env(value)
        if _is_header_key(lowered):
            shown = _redact_header_map(value, everything=lowered == "extra_headers")
            for name, item in value.items():
                if shown[name] == REDACTED:
                    note(f".{name}", item)
            return shown
        out = {}
        for name, item in value.items():
            if (_key_holds_credential(str(name), key) and _scalar_is_set(item)
                    and not (isinstance(item, str) and item.startswith("os.environ/"))):
                note(f".{name}", item)
                out[name] = HIDDEN
            else:
                out[name] = _project(item, str(name), key, f"{path}.{name}", found)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if _is_header_key(lowered):
            shown = redact_headers(items)
            ordinals: dict[str, int] = {}
            for row, hidden in zip(items, shown):
                if isinstance(row, Mapping) and isinstance(hidden, Mapping) and hidden.get("value") == REDACTED and "value" in row:
                    name = _name_key(row.get("name"))
                    position = ordinals[name] = ordinals.get(name, -1) + 1
                    note(f"[{name}#{position}]", row["value"])
            return shown
        if all(isinstance(item, str) for item in items):
            for label, original in _argv_secrets(items).items():
                note(f"[{label}]", original)
            return redact_argv(items)
        return [_project(item, key, parent, f"{path}[{index}]", found) for index, item in enumerate(items)]
    if isinstance(value, str):
        if lowered in _COMMAND_KEYS:
            tokens = _tokens(value) if value else None
            if tokens is not None:
                for label, original in _argv_secrets(tokens).items():
                    note(f"[{label}]", original)
            shown = redact_command(value)
            if shown != value and tokens is None:
                note("", value)
            return shown
        shown = redact_text(value)
        if shown != value:
            note("", value)
        return shown
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<not serializable>"


def public_value(value: Any) -> Any:
    """Any stored data as a caller may read it (a settings snapshot, a package source, a compiled router config)."""
    return _project(value)


def public_json_response(content: Any, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> Any:
    """The one way an admin route answers with registry or record content.

    Every response that carries a service registry, a service record, a router config, a package index or
    anything else read from stored records is built here, so the projection cannot be forgotten on one
    route. ``tests/test_admin_secret_sweep.py`` fails a route that answers with such content any other way.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=public_value(content), headers=dict(headers or {}))


def public_service_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """A service record as a caller reads it: same shape, no secret value anywhere in it."""
    return _project(dict(record))


def public_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    """A registry document as a caller reads it: every record and every other section projected."""
    return _project(dict(registry))


def secret_slots(record: Mapping[str, Any]) -> dict[str, str]:
    """Every secret a record holds, by path: exactly what its public projection hides.

    A caller that must know whether a save moves or changes a secret asks here, so "what is a secret" has
    one answer in the code.
    """
    found: dict[str, str] = {}
    _project(dict(record), found=found)
    return found


def address_secrets(address: str) -> list[str]:
    """The parts of an address that a view hides: user name, password, each query value and the fragment."""
    try:
        parts = urlsplit(address)
    except ValueError:
        return []
    found = {item for item in (parts.username, parts.password, parts.fragment) if item}
    found.update(pair.partition("=")[2] for pair in parts.query.split("&") if pair.partition("=")[2])
    return sorted((item for item in found if len(item) >= 4), key=len, reverse=True)


def scrub_secrets(value: Any, secrets: Iterable[str]) -> Any:
    """``value`` with each known secret text replaced by the marker, in every string at any depth.

    A check or a probe that ran against a stored address reports that address in its own words; the
    caller knows which texts are secret, so this removes them wherever the report put them.
    """
    texts = [secret for secret in secrets if secret]
    if not texts:
        return value
    if isinstance(value, str):
        for secret in texts:
            value = value.replace(secret, HIDDEN)
        return value
    if isinstance(value, Mapping):
        return {key: scrub_secrets(item, texts) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(item, texts) for item in value]
    return value


def public_process(process: Mapping[str, Any]) -> dict[str, Any]:
    """The process fields the v2 record view may show: an allow-list, projected."""
    projected = _project(dict(process))
    return {key: projected[key] for key in PUBLIC_PROCESS_KEYS if key in projected}


# ---------------------------------------------------------------------------
# The restore
# ---------------------------------------------------------------------------


def _restore(new: Any, stored: Any, key: str = "", parent: str = "") -> Any:
    lowered = key.lower()
    if isinstance(new, Mapping):
        stored_map = stored if isinstance(stored, Mapping) else {}
        if lowered in _ENV_KEYS:
            return restore_env(new, stored)
        if _is_header_key(lowered):
            return _restore_header_map(new, stored_map)
        return {name: _restore(item, stored_map.get(name), str(name), key) for name, item in new.items()}
    if isinstance(new, list):
        stored_list = stored if isinstance(stored, list) else []
        if _is_header_key(lowered):
            return restore_headers(new, stored_list)
        if all(isinstance(item, str) for item in new):
            return restore_argv(new, stored_list)
        return [_restore(item, stored_list[index] if index < len(stored_list) else None, key, parent)
                for index, item in enumerate(new)]
    if isinstance(new, str):
        if lowered in _COMMAND_KEYS:
            return restore_command(new, stored)
        return _restore_scalar(new, stored, key, parent)
    return new


def _restore_header_map(new: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(new)
    for name, value in new.items():
        if value in MARKERS:
            if name not in stored:
                raise ValueError(f"header {name!r} has no stored value to keep; type the value again")
            out[name] = stored[name]
        elif isinstance(value, str) and (REDACTED in value or HIDDEN in value):
            raise ValueError(f"header {name!r} holds a redacted marker inside a longer value; type the value again")
    return out


def _restore_scalar(new: str, stored: Any, key: str, parent: str) -> Any:
    if new in MARKERS:
        hidden = isinstance(stored, (str, int, float)) and not isinstance(stored, bool) and (
            _key_holds_credential(key, parent) or (isinstance(stored, str) and redact_text(stored) != stored))
        if not hidden:
            raise ValueError(
                f"{key or 'a value'!r} has no hidden stored value to keep; type the value again "
                f"(the text {new!r} is reserved for hidden values)")
        return stored
    if isinstance(stored, str) and stored != new and new == redact_text(stored):
        return stored  # sent back exactly as it was shown
    if (REDACTED in new or HIDDEN in new) and new != stored:
        raise ValueError("a value holds a redacted marker; type the value again")
    return new


def restore_shown_scalars(new: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """A flat form with each text field that came back exactly as the registry showed it set to the stored text.

    The add and edit form reads an address from the registry view, which hides a user name, a password and
    a query. Sending that text back must not strip the stored address.
    """
    out = dict(new)
    for name, value in new.items():
        before = stored.get(name)
        if isinstance(value, str) and isinstance(before, str) and before != value and value == redact_text(before):
            out[name] = before
    return out


def restore_registry(new: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """A registry from a caller with each marker replaced by the stored value it stands for.

    The two documents are read side by side, by path. A value that came back as shown is the stored
    value; a marker with no stored value at its path, or one that stands for part of a list the caller
    cut, raises ``ValueError``. No marker survives into the result.
    """
    return _restore(copy.deepcopy(dict(new)), dict(stored) if isinstance(stored, Mapping) else {})


def refuse_markers(value: Any, *, where: str = "this record") -> None:
    """Raise ``ValueError`` when a string in a record about to be stored holds a redaction marker.

    A create has nothing stored to restore from, so any marker in it is a literal the caller typed.
    """
    if isinstance(value, str):
        if REDACTED in value or HIDDEN in value:
            raise ValueError(f"{where} holds the text {REDACTED!r} or {HIDDEN!r}; those are reserved for hidden values")
    elif isinstance(value, Mapping):
        for item in value.values():
            refuse_markers(item, where=where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            refuse_markers(item, where=where)
