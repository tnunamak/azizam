"""A definition's ``process_template``, resolved once into the literal ``process`` block ProcessDriver reads.

The owner never types a command line. A template (shipped data) names an argv with ``{slot}`` tokens, and each slot
has a type: a ``port`` or a ``path`` (a file or a folder). The owner gives slot values; ``resolve`` checks each one
and returns the literal block: an argv list, a fixed loopback port, the ``process_ready`` observation and the stop
time. Nothing of the template survives into the record, so ProcessDriver stays as it is and no template engine runs
when an engine starts (the same idea as llama-swap's ``${PORT}`` and GPUStack's ``{{port}}``).

Pure: the folder list and the port check come in as arguments. ``allowed_folders`` and ``port_is_free`` read the
live owner setting and the live port.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping

from gateway import admin_store

SETTING = "engine_run_folders"
LOOPBACK = "127.0.0.1"
MAX_STOP_S = 30.0
MIN_PORT, MAX_PORT = 1024, 65535
NO_FOLDERS_WHY = "Add this folder to the folders Azizam may run engines from first."

TOKEN = re.compile(r"\{([^{}]*)\}")
SLOT_NAME = re.compile(r"[a-z][a-z0-9_]*")
# Programs that run a command line they are given: never the first word of an engine's argv.
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "ash", "busybox",
                    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"})


@dataclass(frozen=True)
class Problem:
    """One refusal: a stable code, the slot it concerns (``""`` for the command), and a plain reason."""

    code: str
    slot: str
    why: str

    def public(self) -> dict[str, str]:
        return {"code": self.code, "slot": self.slot, "why": self.why}


class TemplateRefused(ValueError):
    """The values do not make a safe command. ``problems`` lists every reason found, the first one is the message."""

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems = list(problems)
        super().__init__(self.problems[0].why if self.problems else "The template cannot be resolved.")


@dataclass(frozen=True)
class Resolved:
    argv: list[str]
    cwd: str | None
    port: int
    ready: dict[str, Any]
    stop_timeout_s: float
    idle_stop_s: float | None
    env_names: list[str] = field(default_factory=list)

    @property
    def address(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def process_block(self) -> dict[str, Any]:
        """The literal block ProcessDriver reads (``engine_process._validated``). No env values, no template text."""
        block: dict[str, Any] = {"driver": "process", "start": list(self.argv), "port": {"fixed": self.port},
                                 "stop_timeout_s": self.stop_timeout_s}
        if self.cwd:
            block["cwd"] = self.cwd
        return block

    def observations(self) -> dict[str, Any]:
        return {"process_ready": {"http": dict(self.ready["http"])}}

    def residency(self) -> dict[str, Any]:
        if self.idle_stop_s:
            return {"idle_owner": "gateway", "idle_stop_s": self.idle_stop_s}
        return {"idle_owner": "engine"}

    def preview(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "cwd": self.cwd, "env_names": list(self.env_names), "port": self.port,
                "ready": dict(self.ready)}


# ---------------------------------------------------------------------------
# The template itself: rules a JSON Schema cannot say
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def template_errors(template: Mapping[str, Any]) -> list[str]:
    """Faults in a template as data, for the catalog's load-time check. Empty when it is sound."""
    errors: list[str] = []
    slots = template.get("slots") or {}
    for name in slots:
        if not SLOT_NAME.fullmatch(name):
            errors.append(f"slots.{name}: a slot name is lowercase letters, digits and _")
    ports = [name for name, spec in slots.items() if spec.get("type") == "port"]
    if len(ports) != 1:
        errors.append("slots: a template needs exactly one port slot")
    for name, spec in slots.items():
        for marker in spec.get("locate") or ():
            if os.path.isabs(marker) or ".." in Path(marker).parts or "\x00" in marker:
                errors.append(f"slots.{name}.locate: {marker!r} must be a file name inside the folder")
        if spec.get("type") == "path" and spec.get("locate") and spec.get("kind") != "dir":
            errors.append(f"slots.{name}.locate: markers are looked up inside a folder, so the slot's kind must be dir")
    used: set[str] = set()
    where = [(f"argv[{i}]", text) for i, text in enumerate(template.get("argv") or ())]
    if template.get("cwd") is not None:
        where.append(("cwd", template["cwd"]))
    for place, text in where:
        for token in _tokens(text):
            if token not in slots:
                errors.append(f"{place}: the token {{{token}}} names no declared slot")
            used.add(token)
        if "\x00" in text:
            errors.append(f"{place}: a null byte")
    for name in slots:
        if name not in used:
            errors.append(f"slots.{name}: no argv word or cwd uses this slot")
    argv = template.get("argv") or []
    if argv:
        first = argv[0]
        head = _tokens(first)
        if not (os.path.isabs(first) or (head and slots.get(head[0], {}).get("type") == "path" and first.startswith("{"))):
            errors.append("argv[0]: the program must be an absolute path or start with a path slot")
        if any(slots.get(token, {}).get("type") == "port" for token in head):
            errors.append("argv[0]: the program cannot come from a port slot")
    return errors


# ---------------------------------------------------------------------------
# Live inputs
# ---------------------------------------------------------------------------


def allowed_folders(db_path: Path) -> list[str]:
    """The folders the owner approved for engines to run from. Empty (nothing allowed) when the setting is unset."""
    try:
        record = admin_store.get_operator_setting(SETTING, path=db_path)
    except Exception:  # noqa: BLE001 - an unreadable setting allows nothing
        return []
    folders = ((record or {}).get("value") or {}).get("folders")
    return [f for f in folders if isinstance(f, str)] if isinstance(folders, list) else []


class FolderRefused(ValueError):
    """A folder the owner gave cannot be allowed."""


MAX_FOLDERS = 50


def clean_folders(folders: list[Any]) -> list[str]:
    """The owner's list as stored: full paths of folders that exist, no duplicates. The root folder is never allowed."""
    out: list[str] = []
    if len(folders) > MAX_FOLDERS:
        raise FolderRefused(f"At most {MAX_FOLDERS} folders.")
    for item in folders:
        if not isinstance(item, str) or not item or "\x00" in item or not os.path.isabs(item):
            raise FolderRefused("Each folder is a full path, starting with /.")
        path = os.path.normpath(item)
        if path == os.sep:
            raise FolderRefused("Azizam cannot run engines from the whole computer. Choose one folder.")
        if not os.path.isdir(path):
            raise FolderRefused(f"{path} is not a folder that exists.")
        if path not in out:
            out.append(path)
    return out


def port_is_free(port: int) -> bool:
    """True when nothing holds the loopback port now (a bind succeeds)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def _under(path: str, roots: Collection[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _roots(allowed: Iterable[str]) -> list[str]:
    out = []
    for folder in allowed:
        if isinstance(folder, str) and os.path.isabs(folder):
            out.append(os.path.realpath(folder))
    return out


def _path_slot(name: str, spec: Mapping[str, Any], value: Any, roots: list[str]) -> tuple[str | None, Problem | None]:
    def refuse(code: str, why: str) -> tuple[None, Problem]:
        return None, Problem(code, name, why)

    if not roots:
        return refuse("no_folders", NO_FOLDERS_WHY)
    if not isinstance(value, str) or not value or "\x00" in value:
        return refuse("not_a_path", f"{name}: give a full path.")
    if not os.path.isabs(value):
        return refuse("not_absolute", f"{name}: give the full path, starting with /.")
    if "{" in value or "}" in value:
        return refuse("brace_in_path", f"{name}: a path with a brace in it cannot be used.")
    real = os.path.realpath(value)
    if not os.path.exists(real):
        return refuse("missing", f"{name}: nothing exists at that path.")
    kind = spec.get("kind")
    if kind == "dir" and not os.path.isdir(real):
        return refuse("wrong_kind", f"{name}: this must be a folder.")
    if kind == "file" and not os.path.isfile(real):
        return refuse("wrong_kind", f"{name}: this must be a file.")
    if not _under(real, roots):
        return refuse("outside_allowed_folders", f"{name}: this is not inside a folder Azizam may run engines from. "
                                                 "Add its folder to the allowed folders first.")
    base = real if kind == "dir" else os.path.dirname(real)
    for marker in spec.get("locate") or ():
        found = os.path.realpath(os.path.join(base, marker))
        if not os.path.isfile(found) or not _under(found, roots):
            return refuse("marker_missing", f"{name}: this folder has no {marker}, so it does not look like the right one.")
    return real, None


def _port_slot(name: str, value: Any, *, port_free: Callable[[int], bool],
               reserved_ports: Collection[int]) -> tuple[int | None, Problem | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, Problem("bad_port", name, f"{name}: the port is a whole number.")
    if not MIN_PORT <= value <= MAX_PORT:
        return None, Problem("bad_port", name, f"{name}: the port is between {MIN_PORT} and {MAX_PORT}.")
    if value in reserved_ports:
        return None, Problem("reserved_port", name, f"{name}: port {value} belongs to Azizam or another engine. Pick another.")
    if not port_free(value):
        return None, Problem("port_busy", name, f"{name}: something already uses port {value}. Pick another.")
    return value, None


def _program_problem(argv0: str, roots: list[str]) -> Problem | None:
    if not os.path.isabs(argv0):
        return Problem("program_not_absolute", "", "The program must be a full path.")
    lexical = os.path.normpath(argv0)
    if not _under(lexical, roots):
        return Problem("program_outside_allowed_folders", "",
                       "The program is not inside a folder Azizam may run engines from.")
    names = {os.path.basename(lexical).lower(), os.path.basename(os.path.realpath(lexical)).lower()}
    if names & SHELLS:
        return Problem("shell_program", "", "A shell cannot be the program. Azizam runs the program itself, with no shell.")
    if not (os.path.isfile(lexical) and os.access(lexical, os.X_OK)):
        return Problem("program_missing", "", "The program does not exist or cannot run.")
    return None


def resolve(template: Mapping[str, Any], values: Any, *, allowed_folders: Iterable[str],
            port_free: Callable[[int], bool] = port_is_free,
            reserved_ports: Collection[int] = ()) -> Resolved:
    """The literal process block for these slot values, or ``TemplateRefused`` listing every reason.

    ``reserved_ports`` holds the ports a new engine may not take (the gateway's own listeners, other engines').
    """
    slots: Mapping[str, Mapping[str, Any]] = template.get("slots") or {}
    problems: list[Problem] = []
    if not isinstance(values, Mapping):
        raise TemplateRefused([Problem("bad_slots", "", "The slot values are a list of name and value.")])
    for name in sorted(set(values) - set(slots)):
        problems.append(Problem("unknown_slot", name, f"{name}: this kind of engine has no such setting."))
    # A token the template names must be a slot it declares; the schema check at load makes this a second guard.
    for place in [*(template.get("argv") or ()), template.get("cwd") or ""]:
        for token in _tokens(place):
            if token not in slots:
                problems.append(Problem("unknown_token", token, f"The command names {{{token}}}, which is not a setting."))
    roots = _roots(allowed_folders)
    resolved: dict[str, str] = {}
    port: int | None = None
    for name, spec in slots.items():
        if name not in values or values[name] in (None, ""):
            problems.append(Problem("missing_slot", name, f"{name}: give a value."))
            continue
        if spec.get("type") == "port":
            number, problem = _port_slot(name, values[name], port_free=port_free, reserved_ports=reserved_ports)
            if problem:
                problems.append(problem)
            else:
                port = number
                resolved[name] = str(number)
        else:
            text, problem = _path_slot(name, spec, values[name], roots)
            if problem:
                problems.append(problem)
            else:
                resolved[name] = text  # type: ignore[assignment]
    if problems:
        raise TemplateRefused(_dedupe(problems))

    def fill(text: str) -> str:
        return TOKEN.sub(lambda match: resolved[match.group(1)], text)

    argv = [fill(word) for word in template["argv"]]
    cwd = fill(template["cwd"]) if template.get("cwd") else None
    problem = _program_problem(argv[0], roots)
    if problem is None and cwd is not None:
        real_cwd = os.path.realpath(cwd)
        if not os.path.isabs(cwd) or not os.path.isdir(real_cwd) or not _under(real_cwd, roots):
            problem = Problem("cwd_outside_allowed_folders", "", "The working folder is not inside a folder Azizam may run engines from.")
    if problem is None and any(not word or "\x00" in word or TOKEN.search(word) for word in argv):
        problem = Problem("token_left", "", "A setting was not filled in.")
    if problem is not None:
        raise TemplateRefused([problem])
    assert port is not None  # template_errors guarantees one port slot
    stop = template.get("stop") or {}
    timeout = min(float(stop.get("timeout_s", 5)), MAX_STOP_S)
    idle = template.get("idle_stop_s")
    return Resolved(argv=argv, cwd=cwd, port=port, ready={"http": dict(template["ready"]["http"])},
                    stop_timeout_s=timeout, idle_stop_s=float(idle) if idle else None,
                    env_names=list(template.get("env") or ()))


def _dedupe(problems: list[Problem]) -> list[Problem]:
    seen: set[tuple[str, str]] = set()
    out = []
    for problem in problems:
        key = (problem.code, problem.slot)
        if key not in seen:
            seen.add(key)
            out.append(problem)
    return out
