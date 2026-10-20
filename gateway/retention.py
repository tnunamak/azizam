"""Request retention: one contract, one decision, one write path.

``registry/retention.yml`` holds every default of the contract and
``registry/retention.schema.json`` is its closed schema; this module loads and
validates them and holds no default of its own (docs/design/REQUEST_RETENTION.md
sections 3 and 12). It has no dependency on the settings or member stores, so
the stores can import it.

Slice 1 moved what ``prompt_memory`` and the Responses route decided on their
own into this module without changing a decision:

* ``decide``: whether a request's words are kept, in a fixed order;
* ``guard`` and ``write``: the one writer, which holds the household setting's
  write lock and refuses a write that a later setting forbids;
* ``conversation_store`` and ``conversation_row_visible``: the Responses rule
  (when a reply is stored) and the conversation scope rule (who can read it);
* the validator for the bundled file and for the household setting.

The two kinds that exist today share one member switch, the group ``prompts``.
The household setting is stored as operator setting ``retention``. The settings
screens still speak the group's view of it, with the names they always used
(``default_remember``, ``never_keep``, ``retention_cap``); ``prompts_view`` and
``apply_prompts_view`` are the two directions of that mapping.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import jsonschema
import yaml

from gateway.sqlite_startup import connect

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "retention.yml"
SCHEMA_PATH = _REGISTRY_DIR / "retention.schema.json"

#: The household policy: operator setting ``retention``.
SETTING_KEY = "retention"
#: ``put_operator_setting`` accepts the prompts group's view under this key (see ``apply_prompts_view``).
PROMPTS_VIEW_KEY = "prompt_memory"
#: The member switch of the group ``prompts`` lives in this table; its rows are that group's rows.
MEMBER_TABLE = "prompt_memory_preferences"
ERASURE_CUTOFF_TABLE = "prompt_memory_erasure_cutoffs"
RETENTION_LOCK_TIMEOUT_SECONDS = 0.25

#: Per-request control: ``extensions["com.azizam.prompts"] = {"mode": "forget"}``.
NAMESPACE = "com.azizam.prompts"

# Core defines the kinds, because each kind needs a capture point in code. Data gives each its values.
LIBRARY_PROMPT = "library_prompt"
CONVERSATION = "conversation"
KINDS = (LIBRARY_PROMPT, CONVERSATION)
#: The group whose view the settings screens speak.
GROUP_PROMPTS = "prompts"
#: What a record of each kind can hold at most: the boundaries the code can capture.
CAPTURE_POINTS = {LIBRARY_PROMPT: ("accepted",), CONVERSATION: ("accepted", "received")}
#: Only reader rules enforced by the existing Library and Responses routes belong in slice 1.
READERS = {LIBRARY_PROMPT: ("maker",), CONVERSATION: ("scope",)}

#: The names of the prompts group's view (the settings screens' wire names).
PROMPTS_VIEW_FIELDS = frozenset({"default_remember", "never_keep", "retention_cap"})


class RetentionDataError(ValueError):
    """The retention file cannot be used. The message names the file and the rule broken."""


class PromptModeError(ValueError):
    pass


# ---------------------------------------------------------------- the bundled file


@dataclass(frozen=True)
class Contract:
    """The validated bundled file. ``data`` is never mutated; callers get copies."""

    data: Mapping[str, Any]
    source: str

    def group_of(self, kind: str) -> str:
        for name, group in self.data["groups"].items():
            if kind in group["kinds"]:
                return name
        raise KeyError(kind)

    def group(self, kind: str) -> Mapping[str, Any]:
        return self.data["groups"][self.group_of(kind)]

    def seconds_map(self) -> dict[str, int | None]:
        return dict(self.data["retention_choices"])

    def choices(self, kind: str) -> list[str]:
        return list(self.group(kind)["choices"])

    def household_defaults(self, kind: str) -> dict[str, Any]:
        """The household's values before the owner sets any, in the settings screens' names."""
        group = self.group(kind)
        return {
            "default_remember": group["default_keep"],
            "never_keep": self.data["household"]["never_keep"],
            "retention_cap": group["default_cap"],
        }

    def member_defaults(self, kind: str) -> dict[str, Any]:
        group = self.group(kind)
        return {"remember": group["default_keep"], "retention": group["default_retention"]}

    def parts(self, kind: str) -> tuple[str, ...]:
        return tuple(self.data["kinds"][kind]["parts"]["default"])

    def no_expiry(self, kind: str) -> str:
        """The choice that keeps content with no deadline; the group's default period when it has none."""
        seconds = self.data["retention_choices"]
        group = self.group(kind)
        return next((name for name in group["choices"] if seconds[name] is None), group["default_retention"])

    def public(self, kind: str = LIBRARY_PROMPT) -> dict[str, Any]:
        """What the settings screens need to draw one group: its choices (listing order) and every default."""
        seconds = self.data["retention_choices"]
        result = {
            "choices": [{"id": name, "seconds": seconds[name]} for name in self.group(kind)["choices"]],
            "defaults": {**self.household_defaults(kind), **self.member_defaults(kind)},
        }
        unoffered = [{"id": name, "seconds": duration} for name, duration in seconds.items()
                     if name not in self.group(kind)["choices"]]
        if unoffered:
            result["known_unoffered"] = unoffered
        return result


def _where(path) -> str:
    return "/".join(str(part) for part in path) or "the file"


def _semantic_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    seconds = data["retention_choices"]
    if data["household"]["never_keep"]:
        errors.append("household/never_keep: slice 5 adds startup erasure; slice 1 accepts this switch through the owner setting only")
    seen_in: dict[str, str] = {}
    if set(data["groups"]) != {GROUP_PROMPTS}:
        errors.append("groups: slice 5 adds another group; slice 1 enforces only prompts")
    elif set(data["groups"][GROUP_PROMPTS]["kinds"]) != set(KINDS):
        errors.append("groups/prompts/kinds: slice 5 adds group routing; slice 1 keeps both existing kinds in prompts")
    for kind in data["kinds"]:
        if kind not in KINDS:
            errors.append(f"kinds/{kind}: the kind is not one the gateway can capture ({', '.join(KINDS)})")
    for kind in KINDS:
        if kind not in data["kinds"]:
            errors.append(f"kinds/{kind}: the kind has no values")
    for name, group in data["groups"].items():
        for kind in group["kinds"]:
            if kind not in data["kinds"]:
                errors.append(f"groups/{name}/kinds: {kind} is not defined under kinds")
            elif kind in seen_in:
                errors.append(f"groups/{name}/kinds: {kind} is already in the group {seen_in[kind]}; a kind is in one group")
            seen_in.setdefault(kind, name)
        for choice in group["choices"]:
            if choice not in seconds:
                errors.append(f"groups/{name}/choices: {choice} is not in retention_choices")
        if not any(seconds.get(choice) is not None for choice in group["choices"] if choice in seconds):
            errors.append(f"groups/{name}/choices: slice 1 needs a finite choice for deleted stored periods")
        for field in ("default_retention", "default_cap"):
            if group[field] not in group["choices"]:
                errors.append(f"groups/{name}/{field}: {group[field]} is not one of the group's choices")
    for kind in KINDS:
        if kind in data["kinds"] and kind not in seen_in:
            errors.append(f"kinds/{kind}: the kind is in no group")
    for kind, values in data["kinds"].items():
        if kind not in KINDS:
            continue
        if values["readers"] not in READERS[kind]:
            errors.append(f"kinds/{kind}/readers: slice 5 adds reader controls; slice 1 enforces only {', '.join(READERS[kind])}")
        parts = values["parts"]
        if set(parts["allowed"]) != set(CAPTURE_POINTS[kind]) or set(parts["default"]) != set(CAPTURE_POINTS[kind]):
            errors.append(f"kinds/{kind}/parts: slice 5 adds part-aware capture; slice 1 enforces only the current parts")
        for part in parts["allowed"]:
            if part not in CAPTURE_POINTS[kind]:
                errors.append(f"kinds/{kind}/parts/allowed: {part} is not captured for {kind} (captured: {', '.join(CAPTURE_POINTS[kind])})")
        for part in parts["default"]:
            if part not in parts["allowed"]:
                errors.append(f"kinds/{kind}/parts/default: {part} is not in allowed")
    return errors


def parse_contract(data: Any, *, schema: Mapping[str, Any], source: str = "<memory>") -> Contract:
    """Validate ``data`` against the schema and the cross-row rules. Raises with every rule broken."""
    validator = jsonschema.Draft202012Validator(schema)
    problems = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if problems:
        lines = [f"{_where(error.absolute_path)}: {error.message}" for error in problems[:10]]
        more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
        raise RetentionDataError(f"{source}: the retention file does not match its schema{more}:\n  " + "\n  ".join(lines))
    errors = _semantic_errors(data)
    if errors:
        raise RetentionDataError(f"{source}: the retention file breaks the contract:\n  " + "\n  ".join(errors))
    return Contract(data=copy.deepcopy(data), source=source)


_MEMO: dict[tuple[Path, Path], Contract] = {}


def load_contract(path: Path = BUNDLED_PATH, schema_path: Path = SCHEMA_PATH) -> Contract:
    """Read and validate the bundled file once per process; edits apply after restart and deadline repair."""
    cached = _MEMO.get((path, schema_path))
    if cached is not None:
        return cached
    try:
        path.stat()
        schema_path.stat()
    except OSError as exc:
        raise RetentionDataError(
            f"{path}: cannot read the retention file ({exc.strerror or exc}); the gateway keeps no default of its own"
        ) from None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RetentionDataError(f"{path}: cannot read the retention file ({exc})") from None
    contract = parse_contract(data, schema=schema, source=str(path))
    _MEMO[(path, schema_path)] = contract
    return contract


def bundled() -> Contract:
    return load_contract(BUNDLED_PATH, SCHEMA_PATH)


def require_bundled() -> Contract:
    """Startup check: the file is there and obeys the contract, or the gateway does not start."""
    return load_contract(BUNDLED_PATH, SCHEMA_PATH)


def seconds_map() -> dict[str, int | None]:
    return bundled().seconds_map()


def household_defaults(kind: str = LIBRARY_PROMPT) -> dict[str, Any]:
    return bundled().household_defaults(kind)


def member_defaults(kind: str = LIBRARY_PROMPT) -> dict[str, Any]:
    return bundled().member_defaults(kind)


def public_contract(kind: str = LIBRARY_PROMPT) -> dict[str, Any]:
    return bundled().public(kind)


# ----------------------------------------------------- the household setting and its view


def prompts_view(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """The prompts group's view of the household setting: only the values it holds, in the screens' names."""
    document = document if isinstance(document, Mapping) else {}
    view: dict[str, Any] = {}
    if "never_keep" in document:
        view["never_keep"] = document["never_keep"]
    group = (document.get("groups") or {}).get(GROUP_PROMPTS) if isinstance(document.get("groups"), Mapping) else None
    if isinstance(group, Mapping):
        if "default_keep" in group:
            view["default_remember"] = group["default_keep"]
        if "retention_cap" in group:
            view["retention_cap"] = normalize_period(group["retention_cap"], kind=LIBRARY_PROMPT, owner_cap=True)
    return view


def document_from_prompts_view(view: Mapping[str, Any]) -> dict[str, Any]:
    """The household setting that holds exactly the values of a prompts view."""
    document: dict[str, Any] = {}
    if "never_keep" in view:
        document["never_keep"] = view["never_keep"]
    group: dict[str, Any] = {}
    if "default_remember" in view:
        group["default_keep"] = view["default_remember"]
    if "retention_cap" in view:
        group["retention_cap"] = view["retention_cap"]
    if group:
        document["groups"] = {GROUP_PROMPTS: group}
    return document


def apply_prompts_view(document: Mapping[str, Any] | None, view: Mapping[str, Any]) -> dict[str, Any]:
    """The household setting after the prompts group is set to ``view``: its values replace the group's and
    ``never_keep``; any other group stays as it was."""
    document = dict(document) if isinstance(document, Mapping) else {}
    document.pop("never_keep", None)
    groups = dict(document.get("groups") or {})
    groups.pop(GROUP_PROMPTS, None)
    replacement = document_from_prompts_view(view)
    if "never_keep" in replacement:
        document["never_keep"] = replacement["never_keep"]
    groups.update(replacement.get("groups", {}))
    if groups:
        document["groups"] = groups
    else:
        document.pop("groups", None)
    return document


def validate_prompts_view(view: Mapping[str, Any], *, existing_cap: str | None = None) -> dict[str, Any]:
    """The validator for the prompts group's household values. Raises ValueError naming the rule."""
    contract = bundled()
    defaults = contract.household_defaults(LIBRARY_PROMPT)
    default_remember = view.get("default_remember", defaults["default_remember"])
    never_keep = view.get("never_keep", defaults["never_keep"])
    retention_cap = view.get("retention_cap", defaults["retention_cap"])
    if not isinstance(default_remember, bool) or not isinstance(never_keep, bool):
        raise ValueError("prompt memory switches must be booleans")
    choices = contract.choices(LIBRARY_PROMPT)
    if retention_cap not in choices and not (existing_cap is not None and retention_cap == existing_cap):
        raise ValueError("retention_cap must be " + ", ".join(choices[:-1]) + f", or {choices[-1]}")
    return {"default_remember": default_remember, "never_keep": never_keep, "retention_cap": retention_cap}


def household(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """The prompts group's household values: the owner's, over the bundled defaults."""
    return {**household_defaults(), **prompts_view(document)}


def unoffered_household_cap(document: Mapping[str, Any] | None) -> str | None:
    """The saved owner cap, if it is no longer selectable."""
    groups = document.get("groups") if isinstance(document, Mapping) else None
    group = groups.get(GROUP_PROMPTS) if isinstance(groups, Mapping) else None
    return unoffered_period(group.get("retention_cap")) if isinstance(group, Mapping) else None


# ---------------------------------------------------------------------- periods


def normalize_period(value: str, *, kind: str, owner_cap: bool) -> str:
    """Keep known stored IDs; a deleted ID uses the shortest finite offered period."""
    contract = bundled()
    seconds = contract.seconds_map()
    if value in seconds:
        return value
    finite = [choice for choice in contract.choices(kind) if seconds[choice] is not None]
    return min(finite, key=lambda choice: seconds[choice])


def unoffered_period(value: Any, *, kind: str = LIBRARY_PROMPT) -> str | None:
    """Return a saved ID only when the group no longer offers it for selection."""
    return value if isinstance(value, str) and value not in bundled().choices(kind) else None


def effective_period(member_period: str, cap: str, kind: str = LIBRARY_PROMPT) -> str:
    """The shorter normalized period; display, decisions and writes use this same result."""
    contract = bundled()
    seconds = contract.seconds_map()
    periods = (normalize_period(member_period, kind=kind, owner_cap=False),
               normalize_period(cap, kind=kind, owner_cap=True))
    finite = [value for value in periods if seconds[value] is not None]
    if not finite:
        return contract.no_expiry(kind)
    return min(finite, key=lambda value: seconds[value] or 0)


def effective(member: Mapping[str, Any] | None, household_policy: Mapping[str, Any] | None,
              kind: str = LIBRARY_PROMPT) -> dict[str, Any]:
    """What a member's choice and the household's policy leave: keep or not, and for how long."""
    contract = bundled()
    prefs = {**contract.member_defaults(kind), **(member or {})}
    policy = {**contract.household_defaults(kind), **(household_policy or {})}
    keep = bool(prefs["remember"]) and not bool(policy.get("never_keep"))
    return {
        "remember": keep,
        "retention": effective_period(
            str(prefs.get("retention") or contract.member_defaults(kind)["retention"]),
            str(policy.get("retention_cap") or contract.household_defaults(kind)["retention_cap"]),
            kind,
        ),
    }


# --------------------------------------------------------------------- the decision


@dataclass(frozen=True)
class Decision:
    person_id: str | None
    keep: bool
    reason: str
    kept_until: int | None = None
    created_at: int | None = None
    kind: str = LIBRARY_PROMPT
    parts: tuple[str, ...] = ()


def requested_mode(body: Mapping[str, Any] | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    extensions = body.get("extensions")
    if extensions is None:
        return None
    if not isinstance(extensions, Mapping):
        raise PromptModeError("extensions must be an object")
    block = extensions.get(NAMESPACE)
    if block is None:
        return None
    if not isinstance(block, Mapping) or set(block) != {"mode"} or block.get("mode") != "forget":
        raise PromptModeError(f"extensions.{NAMESPACE} must be {{'mode':'forget'}}")
    return "forget"


def decide(
    kind: str, *, person_id: str | None, member: Mapping[str, Any] | None,
    household_policy: Mapping[str, Any] | None, request_body: Mapping[str, Any] | None,
    responses_store: bool | None = None, now: int | None = None,
) -> Decision:
    """Resolve policy before any payload, media or Responses persistence.

    The order is fixed: the kill switch, the request's own forget, no person, the member's switch,
    then keep with the shorter of the member's period and the household cap.
    """
    contract = bundled()
    mode = requested_mode(request_body)
    policy = {**contract.household_defaults(kind), **(household_policy or {})}
    if policy["never_keep"]:
        return Decision(person_id, False, "server_disabled", kind=kind)
    if mode == "forget" or responses_store is False:
        return Decision(person_id, False, "request_forget", kind=kind)
    if not person_id:
        return Decision(None, False, "no_person", kind=kind)
    preference = {**contract.member_defaults(kind), "remember": policy["default_remember"], **(member or {})}
    if not preference.get("remember", policy["default_remember"]):
        return Decision(person_id, False, "member_disabled", kind=kind)
    started = int(time.time()) if now is None else now
    period = effective_period(preference.get("retention", contract.member_defaults(kind)["retention"]),
                              policy.get("retention_cap", contract.household_defaults(kind)["retention_cap"]), kind)
    seconds = contract.seconds_map()[period]
    expiry = started + seconds if seconds is not None else None
    return Decision(person_id, True, "remembered", expiry, started, kind, contract.parts(kind))


def conversation_store(decision: Decision, requested: Any, *, has_scope: bool) -> bool:
    """Whether a Responses request is stored: its own ``store``, else whether it has a person; never without a
    stable App or credential scope; and never against the decision (a no-person request keeps its explicit store)."""
    store = requested
    if store is None:
        store = decision.person_id is not None
    if not has_scope:
        store = False
    if not decision.keep and decision.reason != "no_person":
        store = False
    return bool(store)


def conversation_row_visible(row: Mapping[str, Any] | None, scope: Mapping[str, Any] | None) -> bool:
    """The conversation scope rule: the creating person and the stable App (or, without an App, the credential).

    ``scope`` is ``{"person_id", "app_id", "owner_key_hash"}``, or None when the caller has no stable scope.
    A row with no person is visible only through that App or credential scope.
    """
    if not row:
        return False
    if scope is None or "person_id" not in row or row.get("person_id") != scope["person_id"]:
        return False
    if scope["app_id"]:
        return row.get("app_id") == scope["app_id"]
    return not row.get("app_id") and row.get("owner_key_hash") == scope["owner_key_hash"]


# ------------------------------------------------------------------ the one writer


def ensure_erasure_cutoff_table(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ERASURE_CUTOFF_TABLE,),
    ).fetchone() is None:
        conn.execute(
            f"CREATE TABLE {ERASURE_CUTOFF_TABLE} ("
            "person_id TEXT PRIMARY KEY, cutoff_at INTEGER NOT NULL)"
        )


@contextmanager
def guard(admin_path: Path | None, person_id: str | None = None) -> Iterator[dict[str, Any]]:
    """Hold the admin setting's write lock through a content-bearing write.

    A kill-switch update waits for this write, then purges it. A write that
    starts after the update sees the new policy and is refused. The lock works
    across gateway processes because it is a SQLite transaction.
    """
    contract = bundled()
    defaults = contract.household_defaults(LIBRARY_PROMPT)
    member_default = contract.member_defaults(LIBRARY_PROMPT)
    if admin_path is None:
        yield {**defaults, **member_default}
        return
    conn = connect(admin_path, timeout=RETENTION_LOCK_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout={int(RETENTION_LOCK_TIMEOUT_SECONDS * 1000)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (SETTING_KEY,)).fetchone()
        household_policy = {**defaults, **(prompts_view(json.loads(row[0])) if row else {})}
        ensure_erasure_cutoff_table(conn)
        erased = conn.execute(
            f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id='*'",
        ).fetchone()
        household_cutoff = int(erased[0]) if erased else 0
        member = None
        person_enabled = True
        forgot_before = household_cutoff
        if person_id:
            erased = conn.execute(
                f"SELECT cutoff_at FROM {ERASURE_CUTOFF_TABLE} WHERE person_id=?", (person_id,),
            ).fetchone()
            forgot_before = max(household_cutoff, int(erased[0]) if erased else 0)
            member = conn.execute(
                f"SELECT remember, retention FROM {MEMBER_TABLE} WHERE user_id=?", (person_id,),
            ).fetchone()
            user_exists = conn.execute("SELECT 1 FROM users WHERE id=?", (person_id,)).fetchone() is not None
            if not user_exists:
                person_enabled = False
            else:
                if member is None:
                    conn.execute(
                        f"INSERT OR IGNORE INTO {MEMBER_TABLE}"
                        "(user_id, remember, retention, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (person_id, 1 if household_policy["default_remember"] else 0,
                         member_default["retention"], int(time.time())),
                    )
                    member = conn.execute(
                        f"SELECT remember, retention FROM {MEMBER_TABLE} WHERE user_id=?", (person_id,),
                    ).fetchone()
                account_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_access'"
                ).fetchone()
                if account_table is not None:
                    access = conn.execute(
                        "SELECT disabled_at, removed_at FROM account_access WHERE user_id=?", (person_id,),
                    ).fetchone()
                    person_enabled = access is None or (access[0] is None and access[1] is None)
        policy = {**household_policy,
                  "remember": bool(member[0]) if member else household_policy["default_remember"],
                  "retention": member[1] if member else member_default["retention"],
                  "forgot_before": forgot_before}
        policy["remember"] = bool(policy["remember"]) and person_enabled
        yield policy
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _library_prompt_gate(policy: Mapping[str, Any], started: int, kept_until: int | None) -> tuple[bool, int | None]:
    if policy["never_keep"] or not policy["remember"] or started <= policy.get("forgot_before", 0):
        return False, None
    period = effective_period(policy["retention"], policy["retention_cap"])
    seconds = seconds_map()[period]
    limits = [kept_until] if kept_until is not None else []
    if seconds is not None:
        limits.append(started + seconds)
    deadline = min(limits) if limits else None
    if deadline is not None and deadline <= int(time.time()):
        return False, None
    return True, deadline


def _conversation_gate(policy: Mapping[str, Any], decision: Decision, created_at: int) -> bool:
    return bool(
        not policy["never_keep"]
        and created_at > int(policy.get("forgot_before") or 0)
        and (decision.person_id is None or (
            policy["remember"]
            and int(decision.created_at or 0) > int(policy.get("forgot_before") or 0)
        ))
    )


def write(
    kind: str, *, admin_path: Path | None, person_id: str | None, created_at: int,
    commit: Callable[[int | None], Any], decision: Decision | None = None, kept_until: int | None = None,
) -> bool:
    """Run ``commit(deadline)`` only if the policy in force now still allows the write.

    The policy is read under the household setting's write lock, which is held through ``commit``. A
    later kill switch, member choice or erasure cutoff therefore refuses the write; a write that wins the
    lock is purged by the change that waits behind it. Returns whether the write happened.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    with guard(admin_path, person_id) as policy:
        if kind == LIBRARY_PROMPT:
            allowed, deadline = _library_prompt_gate(policy, created_at, kept_until)
        else:
            if decision is None:
                raise ValueError("a conversation write needs the decision made for the request")
            allowed, deadline = _conversation_gate(policy, decision, created_at), None
        if not allowed:
            return False
        commit(deadline)
        return True
