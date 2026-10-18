"""The fixed mapping language of ``executor: http-v1``.

A map is a list of rules that builds one JSON object from a source document.
The scalar rules are: ``copy``, ``rename``, ``set`` (constant),
``default``, ``drop``, ``enum`` (table), ``clamp``, ``stringify`` and ``when``
(a condition on presence, equality or membership). A rule names JSON pointers
and plain values. ``each`` maps a bounded array into fresh objects, with lexical
``/item`` and ``/index`` roots. There are no expressions or general loops.
Rule count and combined each/when nesting are fixed when the file loads.

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
from itertools import filterfalse
from io import StringIO
from typing import Any, Callable, Mapping, Sequence

from gateway.engine_protocol import pointer
from gateway.engine_protocol.budgets import DEFAULT_BUDGETS, Budgets
from gateway.engine_protocol.errors import BudgetExceeded, DescriptionError, MappingError

REQUEST_ROOTS = frozenset({"body", "params", "files"})
RESPONSE_ROOTS = frozenset({"body", "status", "headers", "params", "event"})

Step = Callable[[Mapping[str, Any], dict], None]
Predicate = Callable[[Mapping[str, Any]], bool]
MISSING = pointer.MISSING
_get = pointer.get
# The running map's (deadline, mapping_s), so a rule nested in a condition can stop the map too.
_RUNNING: ContextVar[tuple[float, float] | None] = ContextVar("engine_protocol_mapping_deadline", default=None)

_RULE_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # rule: (required keys, optional keys)
    "each": (frozenset({"from", "to", "map"}), frozenset()),
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
_EACH_RUNNING: ContextVar[list[int] | None] = ContextVar("engine_protocol_each_selected", default=None)

# Per-invocation facts retain their objects so a recycled id can never hit stale data.
_FACTS: ContextVar[_JsonFacts | None] = ContextVar("engine_protocol_json_facts", default=None)
_CLOCK_STEPS: ContextVar[list[int] | None] = ContextVar("engine_protocol_clock_steps", default=None)


class _JsonFacts:
    """Exact compact UTF-8 size and container depth, shared by source and owned copies.

    Writes invalidate ancestor facts before changing a container. Unchanged subtrees
    keep their facts, including across defensive copies. Unknown non-JSON source
    values are allowed until selected, as they were before this accounting pass.
    """

    def __init__(self, budgets: Budgets) -> None:
        self.budgets = budgets
        self.nodes: dict[int, tuple[Any, int | None, int]] = {}
        self.scalars: dict[tuple[type, Any], int] = {}
        self.numbers: dict[type, dict[Any, int]] = {int: {}, float: {}}

    def measure(self, value: Any, level: int = 1) -> tuple[int | None, int]:
        kind = type(value)
        if kind not in (dict, list):
            if kind not in (str, int, float, bool, type(None)):
                return None, 0
            key = (kind, repr(value) if kind is float and value == 0 else value)
            size = self.scalars.get(key)
            if size is None:
                try:
                    if kind is str and len(value) > 4096:
                        size = 2
                        for start in range(0, len(value), 4096):
                            _check_deadline(force=True)
                            size += _json_size(value[start:start + 4096]) - 2
                    else:
                        size = _json_size(value)
                except (TypeError, ValueError, UnicodeEncodeError):
                    return None, 0
                if len(self.scalars) < 4096:
                    self.scalars[key] = size
            return size, 0
        cached = self.nodes.get(id(value))
        if cached is not None:
            _, size, depth = cached
            if level + depth - 1 > self.budgets.max_json_depth:
                raise BudgetExceeded("max_json_depth", "A mapped value nests too deep.")
            return size, depth
        if level > self.budgets.max_json_depth:
            raise BudgetExceeded("max_json_depth", "A mapped value nests too deep.")
        if kind is list and value and type(value[0]) in (int, float):
            numeric_size = self.numeric_list_size(value)
            if numeric_size is not None:
                self.nodes[id(value)] = (value, numeric_size, 1)
                return numeric_size, 1
        size, depth = 2, 1
        for index, (key, item) in enumerate(value.items() if kind is dict else enumerate(value)):
            if index % 64 == 0:
                _check_deadline(force=index > 0)
            child_size, child_depth = self.measure(item, level + 1)
            depth = max(depth, 1 + child_depth)
            if kind is dict:
                # Match json.dumps' spelling of non-string JSON object keys too.
                if type(key) is str:
                    key_size, _ = self.measure(key)
                elif type(key) in (int, float, bool, type(None)):
                    key_size, _ = self.measure(json.dumps(key))
                else:
                    key_size = None
                if size is not None:
                    size = None if key_size is None else size + key_size + 1
            if size is not None:
                size = None if child_size is None else size + child_size + 1
        if value and size is not None:
            size -= 1  # no trailing comma
        self.nodes[id(value)] = (value, size, depth)
        return size, depth

    def numeric_list_size(self, value: list) -> int | None:
        # C-level set/map/sum avoid a Python recursive call for every number.
        # Chunks bound uninterrupted work, including on lists supplied directly
        # by a caller rather than the byte-limited HTTP parser.
        size = len(value) + 1
        for start in range(0, len(value), 256):
            _check_deadline(force=True)
            chunk = value[start:start + 256]
            kinds = set(map(type, chunk))
            if len(kinds) != 1 or not kinds <= {int, float}:
                return None
            kind = next(iter(kinds))
            sizes = self.numbers[kind]
            missing = set(chunk).difference(sizes)
            # Bound per-invocation scalar cache growth on unique-valued input.
            if len(sizes) + len(missing) > 4096:
                sizes = {}
                missing = set(chunk)
            try:
                for number in missing:
                    _check_deadline()
                    if kind is int and number.bit_length() > 1024:
                        _check_deadline(force=True)
                    sizes[number] = _json_size(number)
                if kind is float and 0.0 in chunk:
                    sizes[0.0] = 3
                    # filterfalse scans in C; only zero values need sign inspection.
                    size += sum(1 for zero in filterfalse(None, chunk) if repr(zero).startswith("-"))
                size += sum(map(sizes.__getitem__, chunk))
            except (ValueError, OverflowError):
                return None
        return size

    def invalidate(self, out: Any, segments: pointer.Segments) -> None:
        cur = out
        for key in segments:
            self.nodes.pop(id(cur), None)
            cur = _get(cur, (key,))
            if cur is MISSING:
                break

    def clone(self, value: Any) -> Any:
        kind = type(value)
        if kind not in (dict, list):
            return value
        cached = self.nodes.get(id(value))
        if kind is list:
            result = []
            flat = cached is not None and cached[2] == 1
            for start in range(0, len(value), 256):
                _check_deadline()
                chunk = value[start:start + 256]
                result.extend(chunk if flat else (self.clone(item) for item in chunk))
        else:
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index % 64 == 0:
                    _check_deadline()
                result[key] = self.clone(item)
        if cached is not None:
            self.nodes[id(result)] = (result, cached[1], cached[2])
        return result


def _clone(value: Any) -> Any:
    facts = _FACTS.get()
    if facts is None:
        return pointer.clone(value)
    _check_deadline(force=True)
    result = facts.clone(value)
    _check_deadline(force=True)
    return result


def _set(out: Any, segments: pointer.Segments, value: Any) -> None:
    facts = _FACTS.get()
    if facts is not None:
        facts.invalidate(out, segments)
    pointer.set_value(out, segments, value)


def _remove(out: Any, segments: pointer.Segments) -> None:
    facts = _FACTS.get()
    if facts is not None:
        facts.invalidate(out, segments)
    pointer.remove(out, segments)


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
    return _json_text(value, compact=False, ensure_ascii=True)


def _json_text(value: Any, *, compact: bool, ensure_ascii: bool) -> str:
    """The standard JSON spelling, with polling inside each-map serialization.

    The ordinary mapping path keeps the native encoder. During each, neither a
    large array nor a long string may hide unbounded work inside one rule.
    """
    if _FACTS.get() is None:
        return json.dumps(value, ensure_ascii=ensure_ascii, **({"separators": (",", ":")} if compact else {}))
    output = StringIO()
    markers: set[int] = set()
    comma, colon = (",", ":") if compact else (", ", ": ")

    def string(text: str) -> None:
        output.write('"')
        for start in range(0, len(text), 4096):
            _check_deadline(force=True)
            output.write(json.dumps(text[start:start + 4096], ensure_ascii=ensure_ascii)[1:-1])
        output.write('"')

    def emit(node: Any) -> None:
        _check_deadline()
        if isinstance(node, str):
            string(node)
        elif isinstance(node, (dict, list, tuple)):
            identity = id(node)
            if identity in markers:
                raise ValueError("Circular reference detected")
            markers.add(identity)
            is_object = isinstance(node, dict)
            output.write("{" if is_object else "[")
            for index, (key, child) in enumerate(node.items() if is_object else enumerate(node)):
                if index:
                    output.write(comma)
                if is_object:
                    if isinstance(key, str):
                        string(key)
                    elif key is None or isinstance(key, (int, float, bool)):
                        string(json.dumps(key))
                    else:
                        raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")
                    output.write(colon)
                emit(child)
            output.write("}" if is_object else "]")
            markers.remove(identity)
        else:
            _check_deadline(force=isinstance(node, int) and node.bit_length() > 1024)
            output.write(json.dumps(node, ensure_ascii=ensure_ascii))

    emit(value)
    _check_deadline(force=True)
    result = output.getvalue()
    _check_deadline(force=True)
    return result


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


def _raw_setter(segments: pointer.Segments) -> Callable[[dict, Any], None]:
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


def _setter(segments: pointer.Segments) -> Callable[[dict, Any], None]:
    write = _raw_setter(segments)

    def set_value(out: dict, value: Any) -> None:
        facts = _FACTS.get()
        if facts is not None:
            facts.invalidate(out, segments)
        write(out, value)
    return set_value


def _equality(expected: Any) -> Callable[[Any], bool]:
    """JSON equality with ``expected`` for a value that may be MISSING."""
    if isinstance(expected, bool) or expected is None:
        return lambda value: value is expected
    if isinstance(expected, str):
        return lambda value: value == expected and type(value) is str
    return lambda value: type(value) in (int, float) and value == expected


class Program:
    """A compiled map. ``apply`` is the only operation."""

    __slots__ = ("_steps", "_passthrough", "_writes", "_mapping_s", "_each_budgets", "_output_budget")

    def __init__(self, steps: Sequence[Step], *, passthrough: bool, writes: bool, mapping_s: float,
                 each_budgets: Budgets | None = None, output_budget: str = "max_request_bytes") -> None:
        self._steps = tuple(steps)
        self._passthrough = passthrough
        self._writes = writes
        self._mapping_s = mapping_s
        self._each_budgets = each_budgets
        self._output_budget = output_budget

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
        each_token = _EACH_RUNNING.set([0]) if self._each_budgets else None
        facts_token = _FACTS.set(_JsonFacts(self._each_budgets)) if self._each_budgets else None
        clock_token = _CLOCK_STEPS.set([0]) if self._each_budgets else None
        try:  # the executor checks depth before it maps; the RecursionError net is for a caller that does not
            if self._each_budgets:
                for value in source.values():
                    _FACTS.get().measure(value)
            out: dict = (_clone(body) if self._writes else body) if self._passthrough else {}
            for step in self._steps:
                step(source, out)
                if time.perf_counter() > deadline:  # between rules, so a slow map stops while it runs, not after
                    raise BudgetExceeded("mapping_s", f"A mapping took longer than {self._mapping_s:g} s.")
            if self._each_budgets:
                size, _ = _FACTS.get().measure(out)
                _check_size(_json_size(out) if size is None else size, self._each_budgets, self._output_budget)
                _check_deadline(force=True)
        except RecursionError:
            raise BudgetExceeded("max_json_depth", "A mapped value nests too deep.") from None
        finally:
            _RUNNING.reset(token)
            if each_token is not None:
                _EACH_RUNNING.reset(each_token)
            if facts_token is not None:
                _FACTS.reset(facts_token)
                _CLOCK_STEPS.reset(clock_token)
        return out


class _Ctx:
    def __init__(self, place: str, roots: frozenset[str], problems: list[str], budgets: Budgets) -> None:
        self.place = place
        self.roots = roots
        self.problems = problems
        self.budgets = budgets
        self.rules = 0
        self.over_count = False
        self.has_each = False
        self.in_each = False
        self.output_budget = "max_response_bytes" if roots & {"status", "headers", "event"} else "max_request_bytes"
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
    return Program(steps, passthrough=base == "passthrough", writes=bool(ctx.writes), mapping_s=budgets.mapping_s,
                   each_budgets=budgets if ctx.has_each else None, output_budget=ctx.output_budget)


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
        ctx.bad(place, "a rule is an object with exactly one key (copy, rename, set, default, drop, enum, clamp, stringify, when or each)")
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
    if not ctx.in_each:
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
            _remove(out, src)
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
        _remove(out, target)
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
                write(out, _json_text(value, compact=True, ensure_ascii=False))
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
    options = spec["in"]
    if (not isinstance(options, list) or not 1 <= len(options) <= 32 or
            any(value is not None and type(value) not in (str, int, float, bool) for value in options)):
        ctx.bad(place, "'in' must be a list of 1 to 32 JSON scalar values")
        return None
    options = list(options)

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


def _check_deadline(*, force: bool = False) -> None:
    steps = _CLOCK_STEPS.get()
    if not force and steps is not None:
        steps[0] += 1
        if steps[0] % 64:
            return
    running = _RUNNING.get()
    if running is not None and time.perf_counter() > running[0]:
        raise BudgetExceeded("mapping_s", f"A mapping took longer than {running[1]:g} s.")


def _check_depth(value: Any, budgets: Budgets) -> None:
    if pointer.depth_exceeds(value, budgets.max_json_depth):
        raise BudgetExceeded("max_json_depth", "A mapped value nests too deep.")


def _json_size(value: Any) -> int:
    kind = type(value)
    if kind is str:
        return len(json.encoder.encode_basestring(value).encode("utf-8"))
    if kind is int:
        return len(str(value))
    if kind is float:
        if value != value:
            return 3  # NaN, matching json.dumps' default allow_nan=True
        if value == float("inf"):
            return 8
        if value == -float("inf"):
            return 9
        return len(repr(value))
    if value is None or value is True:
        return 4
    if value is False:
        return 5
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _check_size(size: int, budgets: Budgets, name: str) -> None:
    if size > getattr(budgets, name):
        raise BudgetExceeded(name, "A mapped output exceeds its byte budget.")


def _c_each(spec: dict, ctx: _Ctx, place: str, depth: int) -> Step | None:
    if depth + 1 > ctx.budgets.max_when_depth:
        ctx.bad(place, f"conditions and each nest more than {ctx.budgets.max_when_depth} deep")
        return None
    src, dst = _source(spec["from"], ctx, place), _target(spec["to"], ctx, place)
    if src is None or dst is None:
        return None
    ctx.has_each = True
    ctx.writes.append(dst)
    roots, in_each, writes = ctx.roots, ctx.in_each, ctx.writes
    ctx.roots = roots | {"item", "index"}
    ctx.in_each = True
    ctx.writes = []
    try:
        children = _compile_list(spec["map"], ctx, f"{place}.map", depth=depth + 1)
    finally:
        ctx.roots, ctx.in_each, ctx.writes = roots, in_each, writes
    read, write = _getter(src), _setter(dst)
    budgets, output_budget = ctx.budgets, ctx.output_budget

    def each_rule(source: Mapping[str, Any], out: dict) -> None:
        values = read(source)
        if values is MISSING:
            return
        if type(values) is not list:
            raise MappingError("The source of each must be an array.")
        _check_deadline(force=True)
        selected = _EACH_RUNNING.get()
        assert selected is not None
        selected[0] += len(values)
        if selected[0] > budgets.max_selected:
            raise BudgetExceeded("max_selected", "Too many items selected by each.")
        result: list[dict] = []
        size = 2  # brackets; add each encoded element once, never the growing array
        _check_size(size, budgets, output_budget)
        local = dict(source)
        for index, item in enumerate(values):
            _check_deadline()
            local["item"], local["index"] = item, index
            mapped: dict = {}
            for step in children:
                step(local, mapped)
                _check_deadline()
            item_size, _ = _FACTS.get().measure(mapped)
            size += (_json_size(mapped) if item_size is None else item_size) + (1 if index else 0)
            _check_size(size, budgets, output_budget)
            result.append(mapped)
        write(out, result)
    return each_rule


_COMPILERS: dict[str, Callable[[dict, _Ctx, str, int], Step | None]] = {
    "each": _c_each,
    "copy": _c_copy, "rename": _c_rename, "set": _c_set, "default": _c_default, "drop": _c_drop,
    "enum": _c_enum, "clamp": _c_clamp, "stringify": _c_stringify, "when": _c_when,
}
