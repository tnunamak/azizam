"""The fixed mapping language of ``executor: http-v1``.

A map is a list of rules that builds one JSON object from a source document.
There are nine rules and no others: ``copy``, ``rename``, ``set`` (constant),
``default``, ``drop``, ``enum`` (table), ``clamp``, ``stringify`` and ``when``
(a condition on presence, equality or membership). A rule names JSON pointers
and plain values. There are no loops, no expressions, no functions and no side
effects, so a map is straight-line: its cost is the number of rules times the
size of the values they touch, and the rule count is fixed when the file loads.

A map is compiled once, when a description loads, into closures over parsed
pointers. ``Program.apply`` then does no parsing.

The source document of a request map is ``{"body", "params", "files"}``; of a
response map ``{"body", "status", "headers", "params"}`` (and ``"event"``, the
SSE event name, in a stream). The base of the output is empty, or a copy of
``/body`` (``passthrough``).
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import Any, Callable, Mapping, Sequence

from gateway.engine_protocol import pointer
from gateway.engine_protocol.budgets import DEFAULT_BUDGETS, Budgets
from gateway.engine_protocol.errors import BudgetExceeded, DescriptionError, MappingError

REQUEST_ROOTS = frozenset({"body", "params", "files"})
RESPONSE_ROOTS = frozenset({"body", "status", "headers", "params", "event"})

Step = Callable[[Mapping[str, Any], dict], None]
Predicate = Callable[[Mapping[str, Any]], bool]
MISSING = pointer.MISSING
_get, _set, _clone = pointer.get, pointer.set_value, pointer.clone
# The running map's (deadline, mapping_s), so a rule nested in a condition can stop the map too.
_RUNNING: ContextVar[tuple[float, float] | None] = ContextVar("engine_protocol_mapping_deadline", default=None)

_RULE_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # rule: (required keys, optional keys)
    "copy": (frozenset({"from", "to"}), frozenset()),
    "rename": (frozenset({"from", "to"}), frozenset()),
    "set": (frozenset({"to", "value"}), frozenset()),
    "default": (frozenset({"to", "value"}), frozenset()),
    "drop": (frozenset({"path"}), frozenset()),
    "enum": (frozenset({"from", "to", "table", "unmapped"}), frozenset()),
    "clamp": (frozenset({"path"}), frozenset({"min", "max"})),
    "stringify": (frozenset({"from", "to"}), frozenset()),
    "when": (frozenset({"if", "then"}), frozenset({"else"})),
}
_PREDICATE_KEYS = ("present", "equals", "in")
_UNMAPPED = ("pass", "drop", "error")


def same_json(left: Any, right: Any) -> bool:
    """JSON equality: ``true`` is not ``1``; ``1`` equals ``1.0``."""
    if left is right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    return left == right


def json_key(value: Any) -> str:
    """How a scalar is spelled as a table key."""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _getter(segments: pointer.Segments) -> Callable[[Any], Any]:
    """A function reading ``segments`` from a document, specialised for short paths (the usual case)."""
    if len(segments) == 1:
        (a,) = segments

        def get1(doc: Any) -> Any:
            return doc.get(a, MISSING) if type(doc) is dict else MISSING
        return get1
    if len(segments) == 2:
        a, b = segments

        def get2(doc: Any) -> Any:
            inner = doc.get(a, MISSING)
            if type(inner) is dict:
                return inner.get(b, MISSING)
            return MISSING if inner is MISSING else _get(inner, (b,))
        return get2

    def getn(doc: Any) -> Any:
        cur = doc
        try:
            for key in segments:
                cur = cur[key]
        except KeyError:
            return MISSING
        except (TypeError, IndexError):  # a list (numeric key), a scalar, or an index out of range: the careful reader decides
            return _get(doc, segments)
        return cur
    return getn


def _setter(segments: pointer.Segments) -> Callable[[dict, Any], None]:
    """A function writing a value at ``segments`` of an output object, specialised for short paths."""
    numeric = any(key.isascii() and key.isdigit() for key in segments)
    if len(segments) == 1 and not numeric:
        (a,) = segments

        def set1(out: dict, value: Any) -> None:
            out[a] = value
        return set1
    if len(segments) == 2 and not numeric:
        a, b = segments

        def set2(out: dict, value: Any) -> None:
            inner = out.get(a, MISSING)
            if type(inner) is dict:
                inner[b] = value
            elif inner is MISSING:
                out[a] = {b: value}
            else:
                _set(out, segments, value)
        return set2
    return lambda out, value: _set(out, segments, value)


def _equality(expected: Any) -> Callable[[Any], bool]:
    """JSON equality with ``expected`` for a value that may be MISSING."""
    if isinstance(expected, bool) or expected is None:
        return lambda value: value is expected
    if isinstance(expected, str):
        return lambda value: value == expected and type(value) is str
    return lambda value: type(value) in (int, float) and value == expected


class Program:
    """A compiled map. ``apply`` is the only operation."""

    __slots__ = ("_steps", "_passthrough", "_writes", "_mapping_s")

    def __init__(self, steps: Sequence[Step], *, passthrough: bool, writes: bool, mapping_s: float) -> None:
        self._steps = tuple(steps)
        self._passthrough = passthrough
        self._writes = writes
        self._mapping_s = mapping_s

    def apply(self, source: Mapping[str, Any]) -> dict:
        """Build the output. It may share containers with ``source`` where no rule writes below them: read it, do not edit it.

        A passthrough map with no rule that writes returns the body itself. A rule that does write works on a copy, so
        ``source`` is never changed.
        """
        deadline = time.perf_counter() + self._mapping_s
        body = source["body"]
        if self._passthrough and type(body) is not dict:
            raise MappingError("A passthrough map needs the body to be a JSON object.")
        token = _RUNNING.set((deadline, self._mapping_s))
        try:  # the executor checks depth before it maps; the RecursionError net is for a caller that does not
            out: dict = (_clone(body) if self._writes else body) if self._passthrough else {}
            for step in self._steps:
                step(source, out)
                if time.perf_counter() > deadline:  # between rules, so a slow map stops while it runs, not after
                    raise BudgetExceeded("mapping_s", f"A mapping took longer than {self._mapping_s:g} s.")
        except RecursionError:
            raise BudgetExceeded("max_json_depth", "A mapped value nests too deep.") from None
        finally:
            _RUNNING.reset(token)
        return out


class _Ctx:
    def __init__(self, place: str, roots: frozenset[str], problems: list[str], budgets: Budgets) -> None:
        self.place = place
        self.roots = roots
        self.problems = problems
        self.budgets = budgets
        self.rules = 0
        self.over_count = False
        # Every place a rule writes or removes, and every copy with the cell that says whether it must clone.
        self.writes: list[pointer.Segments] = []
        self.copies: list[tuple[pointer.Segments, list[bool]]] = []

    def bad(self, place: str, message: str) -> None:
        self.problems.append(f"{place}: {message}")


def compile_program(rules: Any, *, base: str, roots: frozenset[str], place: str, problems: list[str],
                    budgets: Budgets = DEFAULT_BUDGETS) -> Program | None:
    """Compile ``rules``. Problems are appended to ``problems`` (every one, not only the first); ``None`` when any."""
    before = len(problems)
    ctx = _Ctx(place, roots, problems, budgets)
    steps = _compile_list(rules, ctx, place, depth=0)
    if len(problems) > before:
        return None
    # A copy shares the source's container unless some rule writes below its target; then it must own a copy.
    for target, needs_clone in ctx.copies:
        needs_clone[0] = any(len(w) > len(target) and w[:len(target)] == target for w in ctx.writes)
    return Program(steps, passthrough=base == "passthrough", writes=bool(ctx.writes), mapping_s=budgets.mapping_s)


def build(rules: Sequence[Mapping[str, Any]], *, base: str, roots: frozenset[str], budgets: Budgets = DEFAULT_BUDGETS) -> Program:
    """``compile_program`` for callers that want an exception."""
    problems: list[str] = []
    program = compile_program(rules, base=base, roots=roots, place="map", problems=problems, budgets=budgets)
    if program is None:
        raise DescriptionError("map", problems)
    return program


def _compile_list(rules: Any, ctx: _Ctx, place: str, *, depth: int) -> list[Step]:
    if not isinstance(rules, list):
        ctx.bad(place, "must be a list of rules")
        return []
    steps: list[Step] = []
    for index, rule in enumerate(rules):
        step = _compile_rule(rule, ctx, f"{place}[{index}]", depth)
        if step is not None:
            steps.append(step)
    return steps


def _compile_rule(rule: Any, ctx: _Ctx, place: str, depth: int) -> Step | None:
    ctx.rules += 1
    if ctx.rules > ctx.budgets.max_rules:
        if not ctx.over_count:
            ctx.over_count = True
            ctx.bad(ctx.place, f"more than {ctx.budgets.max_rules} rules in one map")
        return None
    if not isinstance(rule, dict) or len(rule) != 1:
        ctx.bad(place, "a rule is an object with exactly one key (copy, rename, set, default, drop, enum, clamp, stringify or when)")
        return None
    kind, spec = next(iter(rule.items()))
    if kind not in _RULE_KEYS:
        ctx.bad(place, f"unknown rule '{kind}' (the rules are {', '.join(_RULE_KEYS)})")
        return None
    place = f"{place}.{kind}"
    if not isinstance(spec, dict):
        ctx.bad(place, "must be an object")
        return None
    required, optional = _RULE_KEYS[kind]
    ok = True
    for key in sorted(required - spec.keys()):
        ctx.bad(place, f"missing '{key}'")
        ok = False
    for key in sorted(spec.keys() - required - optional):
        ctx.bad(place, f"unknown key '{key}'")
        ok = False
    if not ok:
        return None
    return _COMPILERS[kind](spec, ctx, place, depth)


def _source(text: Any, ctx: _Ctx, place: str, label: str = "from") -> pointer.Segments | None:
    try:
        segments = pointer.parse_pointer(text)
    except ValueError as exc:
        ctx.bad(place, f"'{label}' is not a valid pointer: {exc}")
        return None
    if segments[0] not in ctx.roots:
        ctx.bad(place, f"'{label}' is a source pointer: it must start with " + ", ".join("/" + root for root in sorted(ctx.roots)))
        return None
    return segments


def _target(text: Any, ctx: _Ctx, place: str, label: str = "to") -> pointer.Segments | None:
    try:
        return pointer.parse_pointer(text)
    except ValueError as exc:
        ctx.bad(place, f"'{label}' is not a valid pointer: {exc}")
        return None


def _constant(value: Any, ctx: _Ctx, place: str) -> tuple[bool, Any]:
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        ctx.bad(place, "'value' must be plain JSON")
        return False, None
    if size > ctx.budgets.max_constant_bytes:
        ctx.bad(place, f"constant larger than {ctx.budgets.max_constant_bytes} bytes")
        return False, None
    if pointer.depth_exceeds(value, ctx.budgets.max_json_depth):
        ctx.bad(place, f"constant nests deeper than {ctx.budgets.max_json_depth}")
        return False, None
    return True, value


def _c_copy(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    src, dst = _source(spec["from"], ctx, place), _target(spec["to"], ctx, place)
    if src is None or dst is None:
        return None
    ctx.writes.append(dst)
    needs_clone = [True]
    ctx.copies.append((dst, needs_clone))

    read, write = _getter(src), _setter(dst)

    def copy_rule(source: Mapping[str, Any], out: dict) -> None:
        value = read(source)
        if value is not MISSING:
            write(out, _clone(value) if needs_clone[0] and type(value) in (dict, list) else value)
    return copy_rule


def _c_rename(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    src, dst = _target(spec["from"], ctx, place, "from"), _target(spec["to"], ctx, place)
    if src is None or dst is None:
        return None
    ctx.writes += [src, dst]

    def rename_rule(source: Mapping[str, Any], out: dict) -> None:
        value = _get(out, src)
        if value is not MISSING:
            pointer.remove(out, src)
            _set(out, dst, value)
    return rename_rule


def _c_set(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    dst = _target(spec["to"], ctx, place)
    ok, value = _constant(spec["value"], ctx, place)
    if dst is None or not ok:
        return None
    ctx.writes.append(dst)
    write = _setter(dst)
    if type(value) in (dict, list):
        def set_container(source: Mapping[str, Any], out: dict) -> None:
            write(out, _clone(value))
        return set_container

    def set_rule(source: Mapping[str, Any], out: dict) -> None:
        write(out, value)
    return set_rule


def _c_default(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    dst = _target(spec["to"], ctx, place)
    ok, value = _constant(spec["value"], ctx, place)
    if dst is None or not ok:
        return None
    ctx.writes.append(dst)

    read, write = _getter(dst), _setter(dst)

    def default_rule(source: Mapping[str, Any], out: dict) -> None:
        if read(out) is MISSING:
            write(out, _clone(value))
    return default_rule


def _c_drop(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    target = _target(spec["path"], ctx, place, "path")
    if target is None:
        return None
    ctx.writes.append(target)

    def drop_rule(source: Mapping[str, Any], out: dict) -> None:
        pointer.remove(out, target)
    return drop_rule


def _c_enum(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    src, dst = _source(spec["from"], ctx, place), _target(spec["to"], ctx, place)
    table, unmapped = spec["table"], spec["unmapped"]
    ok = src is not None and dst is not None
    if not isinstance(table, dict) or not table:
        ctx.bad(place, "'table' must be a non-empty object")
        ok = False
    elif not all(isinstance(v, (str, int, float, bool, type(None))) for v in table.values()):
        ctx.bad(place, "'table' values must be plain JSON scalars")
        ok = False
    if unmapped not in _UNMAPPED:
        ctx.bad(place, f"'unmapped' must be one of {', '.join(_UNMAPPED)}")
        ok = False
    if not ok:
        return None
    ctx.writes.append(dst)
    table = dict(table)
    read, write = _getter(src), _setter(dst)

    def enum_rule(source: Mapping[str, Any], out: dict) -> None:
        value = read(source)
        if value is MISSING:
            return
        key = value if type(value) is str else json_key(value)
        if key in table:
            write(out, table[key])
        elif unmapped == "pass":
            write(out, _clone(value))
        elif unmapped == "error":
            raise MappingError(f"The value {key!r} has no entry in the table for {'/'.join(src)}.")
    return enum_rule


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _c_clamp(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    target = _target(spec["path"], ctx, place, "path")
    low, high = spec.get("min"), spec.get("max")
    ok = target is not None
    if low is None and high is None:
        ctx.bad(place, "needs 'min', 'max' or both")
        ok = False
    for label, bound in (("min", low), ("max", high)):
        if bound is not None and not _is_number(bound):
            ctx.bad(place, f"'{label}' must be a number")
            ok = False
    if ok and low is not None and high is not None and low > high:
        ctx.bad(place, "'min' is greater than 'max'")
        ok = False
    if not ok:
        return None
    ctx.writes.append(target)

    read, write = _getter(target), _setter(target)

    def clamp_rule(source: Mapping[str, Any], out: dict) -> None:
        value = read(out)
        if value is MISSING:
            return
        if not _is_number(value):
            raise MappingError(f"Cannot clamp /{'/'.join(target)}: the value is not a number.")
        if low is not None and value < low:
            value = low
        if high is not None and value > high:
            value = high
        write(out, value)
    return clamp_rule


def _c_stringify(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    src, dst = _source(spec["from"], ctx, place), _target(spec["to"], ctx, place)
    if src is None or dst is None:
        return None
    ctx.writes.append(dst)

    read, write = _getter(src), _setter(dst)

    def stringify_rule(source: Mapping[str, Any], out: dict) -> None:
        value = read(source)
        if value is not MISSING:
            try:
                write(out, json.dumps(value, separators=(",", ":"), ensure_ascii=False))
            except (TypeError, ValueError) as exc:  # an asset reference, a non-text key
                raise MappingError(f"Cannot stringify {'/'.join(src)}: {exc}") from None
    return stringify_rule


def compile_predicate(spec: Any, ctx_roots: frozenset[str], place: str, problems: list[str]) -> Predicate | None:
    """A condition on presence, equality or membership: ``{path, present | equals | in}``."""
    ctx = _Ctx(place, ctx_roots, problems, DEFAULT_BUDGETS)
    if not isinstance(spec, dict):
        ctx.bad(place, "a predicate is an object {path, present | equals | in}")
        return None
    extra = sorted(set(spec) - {"path", "present", "equals", "in"})
    if extra:
        ctx.bad(place, f"unknown predicate key '{extra[0]}'")
        return None
    chosen = [key for key in _PREDICATE_KEYS if key in spec]
    if "path" not in spec or len(chosen) != 1:
        ctx.bad(place, "a predicate needs 'path' and exactly one of present, equals, in")
        return None
    segments = _source(spec["path"], ctx, place, "path")
    if segments is None:
        return None
    kind = chosen[0]
    read = _getter(segments)
    if kind == "present":
        want = bool(spec["present"])
        if want:
            return lambda source: read(source) is not MISSING
        return lambda source: read(source) is MISSING
    if kind == "equals":
        same = _equality(spec["equals"])
        return lambda source: same(read(source))
    options = list(spec["in"])

    def member(source: Mapping[str, Any]) -> bool:
        value = read(source)
        return value is not MISSING and any(same_json(value, option) for option in options)
    return member


def _c_when(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    if depth + 1 > ctx.budgets.max_when_depth:
        ctx.bad(place, f"conditions nest more than {ctx.budgets.max_when_depth} deep")
        return None
    predicate = compile_predicate(spec["if"], ctx.roots, f"{place}.if", ctx.problems)
    before = len(ctx.problems)
    then = _compile_list(spec["then"], ctx, f"{place}.then", depth=depth + 1)
    other = _compile_list(spec["else"], ctx, f"{place}.else", depth=depth + 1) if "else" in spec else []
    if predicate is None or len(ctx.problems) > before:
        return None

    def when_rule(source: Mapping[str, Any], out: dict) -> None:
        for step in (then if predicate(source) else other):
            step(source, out)
            running = _RUNNING.get()
            if running is not None and time.perf_counter() > running[0]:
                raise BudgetExceeded("mapping_s", f"A mapping took longer than {running[1]:g} s.")
    return when_rule


_COMPILERS: dict[str, Callable[[dict, _Ctx, str, int], Step | None]] = {
    "copy": _c_copy, "rename": _c_rename, "set": _c_set, "default": _c_default, "drop": _c_drop,
    "enum": _c_enum, "clamp": _c_clamp, "stringify": _c_stringify, "when": _c_when,
}
