"""Write-only secret commands (decision D27).

A secret of a record changes only through an explicit command that names a stable entry id: ``add``,
``replace`` (a value is required), ``rename`` (public name only), ``retain`` (no value; it checks that the id
exists), and ``clear``. A command never infers anything: a ``[redacted]`` marker, an empty string, ``null`` or a
position does not mean "keep", and a value that is one of the marker texts is refused as reserved.

The carriers are the ones ``gateway/record_carriers.py`` defines. A list of arguments is changed as a whole
(``set``), where every item says what it is: a public ``literal``, a ``retain`` of an entry by id, or a new
``secret``; an entry the new list does not keep must be named in ``clear``, so nothing is dropped by omission.
Every command is checked before anything is stored, a bad one stores nothing, and a failure after storing
removes what this call stored. The error text is fixed: it never repeats a submitted value.

This module is management-safe: it stores and deletes secrets and never decrypts one.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from gateway import record_carriers, secret_refs
from gateway.secret_refs import Put

MAX_VALUE = 8192
MAX_COMMANDS = 64
RESERVED_TEXTS = frozenset({"[redacted]", "<redacted>", "<configured>"})
OPERATIONS = ("add", "replace", "rename", "retain", "clear", "set", "set_credentials", "clear_credentials")
_ALLOWED_KEYS = {
    "add": {"carrier", "operation", "name", "value", "public"},
    "replace": {"carrier", "operation", "entry_id", "value"},
    "rename": {"carrier", "operation", "entry_id", "name"},
    "retain": {"carrier", "operation", "entry_id"},
    "clear": {"carrier", "operation", "entry_id"},
    "set": {"carrier", "operation", "items", "clear"},  # an item is a literal, a retain, a replace (same id) or a new secret
    "set_credentials": {"carrier", "operation", "url"},
    "clear_credentials": {"carrier", "operation"},
}


class CommandError(ValueError):
    """A command was refused. ``code`` is stable; the message never holds a submitted value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Outcome:
    """What a call stored and what it replaced, so the caller can finish or undo it."""

    created: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    changed: bool = False


def carrier_kind(carrier: Any) -> str | None:
    """``headers``, ``env``, ``argv``, ``url`` or ``secret`` for a carrier path the record model declares; None otherwise."""
    if not isinstance(carrier, str):
        return None
    if carrier == "config.api_key":
        return "provider_key"
    if carrier == record_carriers.HEADER_PATH:
        return "headers"
    container, _, key = carrier.rpartition(".")
    if key == "env" and container in record_carriers.ENV_CONTAINERS:
        return "env"
    if key in record_carriers.COMMAND_KEYS and container in record_carriers.COMMAND_CONTAINERS:
        return "argv"
    if carrier in record_carriers.URL_FIELD_PATHS or _is_observation_url(carrier) or _is_model_url(carrier):
        return "url"
    if carrier in record_carriers.SCALAR_SECRET_PATHS:
        return "secret"
    return None


def _is_model_url(carrier: str) -> bool:
    return re.fullmatch(r"config\.models\[\d{1,4}\]\.url", carrier) is not None


def _is_observation_url(carrier: str) -> bool:
    parts = carrier.split(".")
    return len(parts) == 4 and parts[0] == "observations" and parts[2] == "http" and parts[3] in ("path", "url")


def validate_value(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CommandError("invalid_value", "a secret value is required and must be non-empty text")
    if len(value) > MAX_VALUE or "\x00" in value:
        raise CommandError("invalid_value", "a secret value is too long or holds a character that cannot be stored")
    if value.strip() in RESERVED_TEXTS:
        raise CommandError("reserved_value", "that text is reserved and is never a secret value")
    return value


def refuse_reserved_texts(value: Any, *, where: str = "this record") -> None:
    """Raise ``ValueError`` when a text in ``value`` is one of the reserved marker texts.

    No read shows a marker any more, so a marker in a write is text someone typed or a stale client echoed. It
    would be stored as a secret value, so it is refused instead.
    """
    if isinstance(value, str):
        if value.strip() in RESERVED_TEXTS:
            raise ValueError(f"{where} holds a text that is reserved and never a value; type the value")
    elif isinstance(value, Mapping):
        for item in value.values():
            refuse_reserved_texts(item, where=where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            refuse_reserved_texts(item, where=where)


def parse_commands(body: Any) -> list[dict[str, Any]]:
    """The commands of a request body, checked for shape only: unknown keys, duplicate entry ids, bad types."""
    if not isinstance(body, Mapping) or set(body) - {"commands"}:
        raise CommandError("invalid_commands", "the body must be an object with one key, commands")
    commands = body.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > MAX_COMMANDS:
        raise CommandError("invalid_commands", f"commands must be a list of 1 to {MAX_COMMANDS} commands")
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping):
            raise CommandError("invalid_command", f"command {index + 1} must be an object")
        operation = command.get("operation")
        if operation not in OPERATIONS:
            raise CommandError("invalid_command", f"command {index + 1} has an unknown operation")
        extra = set(command) - _ALLOWED_KEYS[operation]
        if extra:
            raise CommandError("invalid_command", f"command {index + 1} has a field the {operation} operation does not take")
        if carrier_kind(command.get("carrier")) is None:
            raise CommandError("unknown_carrier", f"command {index + 1} names a carrier the record model does not have")
        entry_id = command.get("entry_id")
        if "entry_id" in _ALLOWED_KEYS[operation]:
            if not secret_refs.is_entry_id(entry_id):
                raise CommandError("invalid_entry_id", f"command {index + 1} needs the id of an entry")
            key = (str(command["carrier"]), entry_id)
            if key in seen:
                raise CommandError("duplicate_entry", f"command {index + 1} repeats an entry already named in this request")
            seen.add(key)
        out.append(dict(command))
    return out


# ---------------------------------------------------------------------------
# Applying commands to one record
# ---------------------------------------------------------------------------


def _parent(record: dict[str, Any], carrier: str) -> tuple[dict[str, Any], str]:
    parent_path, _, key = carrier.rpartition(".")
    parent: Any = record
    for part in parent_path.split("."):
        indexed = re.fullmatch(r"([^\[\]]+)\[(\d{1,4})\]", part)
        if indexed:
            rows = parent.get(indexed.group(1)) if isinstance(parent, dict) else None
            parent = rows[int(indexed.group(2))] if isinstance(rows, list) and int(indexed.group(2)) < len(rows) else None
            continue
        parent = parent.get(part) if isinstance(parent, dict) else None
    if not isinstance(parent, dict):
        raise CommandError("carrier_unavailable", "the record has no place for that carrier")
    return parent, key


def _find(items: list[Any], entry_id: str) -> int:
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and item.get("id") == entry_id:
            return index
    raise CommandError("unknown_entry", "no entry with that id in that carrier")


def _header_name(name: Any) -> str:
    from gateway.litellm_substrate import header_problem

    if not isinstance(name, str) or not name.strip():
        raise CommandError("invalid_name", "a header needs a name")
    clean = name.strip()
    if header_problem(clean, "x") in ("not_a_name", "transport"):
        raise CommandError("invalid_name", "that is not a header name Azizam can send")
    return clean


def _headers(record: dict[str, Any], command: Mapping[str, Any], put: Put, origin: str | None, out: Outcome) -> None:
    parent, key = _parent(record, command["carrier"])
    rows = parent.setdefault(key, [])
    if not isinstance(rows, list):
        raise CommandError("carrier_unavailable", "the headers of this record are not a list")
    operation = command["operation"]
    if operation == "add":
        name = _header_name(command.get("name"))
        value = validate_value(command.get("value"))
        if command.get("public") is True:
            if record_carriers.names_credential_header(name):
                raise CommandError("credential_header_public", "a header that carries a credential cannot be public; add it as a secret")
            rows.append({"id": secret_refs.new_entry_id(), "name": name, "value": value, "public": True})
        else:
            ref = put(value, origin)
            out.created.append(ref)
            rows.append(secret_refs.entry(ref, name=name))
        out.changed = True
        return
    index = _find(rows, command["entry_id"])
    if operation == "retain":
        return
    row = rows[index]
    held = secret_refs.is_secret_entry(row)
    if operation == "clear":
        if held:
            out.retired.append(row["secret_ref"])
        del rows[index]
    elif operation == "rename":
        renamed = _header_name(command.get("name"))
        if not held and record_carriers.names_credential_header(renamed):
            raise CommandError("credential_header_public", "a header that carries a credential cannot be public; add it as a secret")
        rows[index] = {**row, "name": renamed}
    else:  # replace
        value = validate_value(command.get("value"))
        if held:
            ref = put(value, origin)
            out.created.append(ref)
            out.retired.append(row["secret_ref"])
            rows[index] = {**row, "secret_ref": ref}
        else:
            rows[index] = {**row, "value": value}
    out.changed = True


def _env_find(env: dict[str, Any], entry_id: str) -> str:
    for name, item in env.items():
        if isinstance(item, Mapping) and item.get("id") == entry_id:
            return name
    raise CommandError("unknown_entry", "no entry with that id in that carrier")


def _env(record: dict[str, Any], command: Mapping[str, Any], put: Put, out: Outcome) -> None:
    parent, key = _parent(record, command["carrier"])
    env = parent.setdefault(key, {})
    if not isinstance(env, dict):
        raise CommandError("carrier_unavailable", "the environment of this record is not a mapping")
    operation = command["operation"]
    if operation == "add":
        name = command.get("name")
        if not isinstance(name, str) or not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
            raise CommandError("invalid_name", "an environment name holds letters, digits and _ only")
        if name in env:
            raise CommandError("duplicate_name", "that environment name already has a value; replace it by its entry id")
        value = validate_value(command.get("value"))
        if command.get("public") is True:
            env[name] = {"id": secret_refs.new_entry_id(), "value": value, "public": True}
        else:
            ref = put(value, None)
            out.created.append(ref)
            env[name] = secret_refs.entry(ref)
        out.changed = True
        return
    name = _env_find(env, command["entry_id"])
    if operation == "retain":
        return
    held = secret_refs.is_secret_entry(env[name])
    if operation == "clear":
        if held:
            out.retired.append(env[name]["secret_ref"])
        del env[name]
    elif operation == "rename":
        raise CommandError("invalid_command", "an environment entry is named by its variable; add a new one and clear this one")
    else:
        value = validate_value(command.get("value"))
        if held:
            ref = put(value, None)
            out.created.append(ref)
            out.retired.append(env[name]["secret_ref"])
            env[name] = {**env[name], "secret_ref": ref}
        else:
            env[name] = {**env[name], "value": value}
    out.changed = True


def _argv(record: dict[str, Any], command: Mapping[str, Any], put: Put, out: Outcome) -> None:
    if command["operation"] != "set":
        raise CommandError("invalid_command", "a command is changed as a whole list with the set operation")
    parent, key = _parent(record, command["carrier"])
    stored = parent.get(key)
    stored_items = stored if isinstance(stored, list) else []
    stored_entries = {item["id"]: item for item in stored_items if secret_refs.is_secret_entry(item)}
    items = command.get("items")
    clear = command.get("clear", [])
    if not isinstance(items, list) or not isinstance(clear, list) or not all(secret_refs.is_entry_id(item) for item in clear):
        raise CommandError("invalid_command", "set needs a list of items and, optionally, a list of entry ids to clear")
    new_items: list[Any] = []
    kept: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or not 1 <= len(item) <= 3 or not set(item) <= {"literal", "retain", "secret", "prefix", "replace"}:
            raise CommandError("invalid_item", f"item {index + 1} must be a literal, a retain, a replace or a secret")
        if "replace" in item:
            # a new value for an entry that keeps its id (decision D27: the id is stable across a replace)
            if not set(item) <= {"replace", "secret", "prefix"} or "secret" not in item or item["replace"] not in stored_entries or item["replace"] in kept:
                raise CommandError("unknown_entry", f"item {index + 1} replaces an entry that is not in this command or is named twice")
            old = stored_entries[item["replace"]]
            prefix = item.get("prefix", old.get("prefix", ""))
            if not isinstance(prefix, str) or "\x00" in prefix:
                raise CommandError("invalid_item", f"item {index + 1}: a prefix is text")
            ref = put(validate_value(item["secret"]), None)
            out.created.append(ref)
            out.retired.append(old["secret_ref"])
            kept.add(item["replace"])
            new_items.append({**{key: value for key, value in old.items() if key != "prefix"}, "secret_ref": ref,
                              **({"prefix": prefix} if prefix else {})})
        elif "literal" in item:
            text = item["literal"]
            if set(item) != {"literal"} or not isinstance(text, str) or not text or "\x00" in text:
                raise CommandError("invalid_item", f"item {index + 1}: a literal is non-empty text")
            new_items.append(text)
        elif "retain" in item:
            if set(item) != {"retain"} or item["retain"] not in stored_entries or item["retain"] in kept:
                raise CommandError("unknown_entry", f"item {index + 1} retains an entry that is not in this command or is named twice")
            kept.add(item["retain"])
            new_items.append(stored_entries[item["retain"]])
        else:
            prefix = item.get("prefix", "")
            if not isinstance(prefix, str) or "\x00" in prefix or not set(item) <= {"secret", "prefix"}:
                raise CommandError("invalid_item", f"item {index + 1}: a secret may carry a public prefix and nothing else")
            ref = put(validate_value(item["secret"]), None)
            out.created.append(ref)
            new_items.append(secret_refs.entry(ref, **({"prefix": prefix} if prefix else {})))
    dropped = set(stored_entries) - kept
    if dropped != set(clear):
        raise CommandError("unmentioned_entry", "every stored secret entry of this command must be retained or named in clear, and clear names only entries the new list drops")
    # The command is structured: each item says what it is, so a literal is public as declared. No flag name is read
    # to guess otherwise (decision D29); only a plain line or list of text, written before D26, is read by the rule.
    out.retired.extend(stored_entries[item_id]["secret_ref"] for item_id in sorted(dropped))
    parent[key] = new_items
    out.changed = True


def provider_key_entry_id(record: Mapping[str, Any]) -> str:
    """A server-issued identity for an engine's inference-key slot, independent of its value or source."""
    return "ent_" + hashlib.sha256((str(record.get("id") or "") + ":inference-key").encode()).hexdigest()[:16]


def bind_protocol_inference(config: dict[str, Any]) -> None:
    """Project the selected inference-key binding into explicitly opted-in protocol data.

    The control credential remains independent. References and environment names
    are copied as metadata; a management write never reads the key back.
    """
    connection = config.get("connection")
    if not isinstance(connection, dict) or connection.get("dispatch") != "protocol":
        return
    reference = config.get("api_key_secret_id")
    environment = config.get("api_key_env") or config.get("api_key_ref")
    if environment:
        reference = "os.environ/" + str(environment)
    if not reference:
        return
    origin = config.get("credential_origin")
    if not origin or record_carriers.endpoint_origin({"address": connection.get("address")}) != origin:
        return
    credentials = connection.setdefault("credentials", {})
    credentials["inference"] = {"secret_ref": reference, "origin": origin, "header": "Authorization", "scheme": "Bearer"}


def _provider_key(record: dict[str, Any], command: Mapping[str, Any], put: Put, origin: str | None, out: Outcome) -> None:
    config = record.setdefault("config", {})
    held = bool(config.get("api_key_secret_id") or config.get("api_key_env") or config.get("api_key_ref")
                or (record.get("secret_refs") or {}).get("api_key"))
    operation = command["operation"]
    if operation == "add":
        if held:
            raise CommandError("duplicate_name", "an inference key is already configured; replace it by its entry id")
    elif not held or command.get("entry_id") != provider_key_entry_id(record):
        raise CommandError("unknown_entry", "no inference-key entry with that id")
    if operation == "retain":
        return
    if operation not in ("add", "replace", "clear"):
        raise CommandError("invalid_command", "that operation does not apply to an inference key")
    if config.get("api_key_secret_id"):
        out.retired.append(str(config["api_key_secret_id"]))
    if operation in ("add", "replace"):
        if not origin:
            raise CommandError("carrier_unavailable", "the inference key needs an engine address with an origin")
        reference = put(validate_value(command.get("value")), origin)
        out.created.append(reference)
        config["api_key_secret_id"] = reference
        config["credential_origin"] = origin
    else:
        config.pop("api_key_secret_id", None)
        config.pop("credential_origin", None)
    config.pop("api_key_env", None)
    config.pop("api_key_ref", None)
    record.get("secret_refs", {}).pop("api_key", None)
    if operation == "clear":
        connection = config.get("connection")
        if isinstance(connection, dict) and connection.get("dispatch") == "protocol":
            connection.get("credentials", {}).pop("inference", None)
    else:
        bind_protocol_inference(config)
    out.changed = True


def _scalar(record: dict[str, Any], command: Mapping[str, Any], put: Put, out: Outcome) -> None:
    parent, key = _parent(record, command["carrier"])
    held = parent.get(key)
    operation = command["operation"]
    if operation == "add":
        if held not in (None, ""):
            raise CommandError("duplicate_name", "that slot already has a value; replace it by its entry id")
        ref = put(validate_value(command.get("value")), None)
        out.created.append(ref)
        parent[key] = secret_refs.entry(ref)
        out.changed = True
        return
    if not (isinstance(held, Mapping) and held.get("id") == command["entry_id"]) or not secret_refs.is_secret_entry(held):
        raise CommandError("unknown_entry", "no entry with that id in that carrier")
    if operation == "retain":
        return
    if operation == "clear":
        out.retired.append(held["secret_ref"])
        del parent[key]
    elif operation == "replace":
        ref = put(validate_value(command.get("value")), None)
        out.created.append(ref)
        out.retired.append(held["secret_ref"])
        parent[key] = {**held, "secret_ref": ref}
    else:
        raise CommandError("invalid_command", "that operation does not apply to a single secret value")
    out.changed = True


def _url(record: dict[str, Any], command: Mapping[str, Any], put: Put, out: Outcome) -> None:
    parent, key = _parent(record, command["carrier"])
    secrets_key = key + record_carriers.SECRETS_SUFFIX
    out.retired.extend(record_carriers.refs_in(parent.get(secrets_key)))
    parent.pop(secrets_key, None)
    if command["operation"] == "clear_credentials":
        out.changed = True
        return
    url = command.get("url")
    if not isinstance(url, str) or not url:
        raise CommandError("invalid_value", "a URL is required")
    parent[key] = url
    before = len(out.created)

    def counting_put(value: str, origin: str | None = None) -> str:
        ref = put(value, origin)
        out.created.append(ref)
        return ref

    try:
        record_carriers.externalize_url(parent, key, counting_put)
    except record_carriers.AmbiguousUrl:
        raise CommandError("invalid_value", "that URL cannot be read") from None
    if len(out.created) == before:
        raise CommandError("invalid_value", "that URL holds no credentials to store; use clear_credentials to remove them")
    out.changed = True


def apply_commands(record: Mapping[str, Any], commands: list[dict[str, Any]], put: Put) -> tuple[dict[str, Any], Outcome]:
    """``record`` with ``commands`` applied, and what was stored and retired. Raises ``CommandError``.

    On an error nothing is returned: the caller deletes ``Outcome.created`` (which this function fills as it goes
    and attaches to the error as ``error.created``).
    """
    out_record = copy.deepcopy(dict(record))
    outcome = Outcome()
    origin = record_carriers.endpoint_origin(out_record.get("config"))
    try:
        for command in commands:
            kind = carrier_kind(command["carrier"])
            if kind == "provider_key":
                _provider_key(out_record, command, put, origin, outcome)
            elif kind == "headers":
                _headers(out_record, command, put, origin, outcome)
            elif kind == "env":
                _env(out_record, command, put, outcome)
            elif kind == "argv":
                _argv(out_record, command, put, outcome)
            elif kind == "secret":
                _scalar(out_record, command, put, outcome)
            else:
                _url(out_record, command, put, outcome)
    except CommandError as error:
        error.created = list(outcome.created)  # type: ignore[attr-defined]
        raise
    if outcome.changed and any(command["operation"] != "retain" and (
        command["carrier"] in {"config.headers", "config.api_key", "config.host", "config.address", "config.url",
                                "config.base_url", "config.api_base", "config.default_base_url", "config.endpoint",
                                "config.upstream", "config.gateway_lifecycle_base_url", "config.connection.address"}
        or _is_model_url(command["carrier"])
    ) for command in commands):
        config = out_record.get("config")
        if isinstance(config, dict):
            config.pop("engine_check", None)
            config.pop("engine_tests", None)
    return out_record, outcome
