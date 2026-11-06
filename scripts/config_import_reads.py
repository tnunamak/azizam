#!/usr/bin/env python3
"""Inventory of import-time config reads in proxy.py (docs/design/CONFIG_HOMES.md, section 1.2 and appendix A).

An AST scan of what runs when proxy.py is imported. Function and lambda *bodies* do not run, so they are pruned
(``_executed``); what does run is scanned: module-level statements, the bodies of ``if``/``for``/``while``/``try``/
``with``/``match`` blocks, class bodies, decorators, default arguments and comprehensions. It starts from the roots
(``config``, ``auth_cfg``, ``admin_cfg``, ``_bootstrap_config``, and a call through ``runtime`` or ``CONFIG_HOME``),
follows every name assigned from a root or from a name already derived (assignment, walrus, ``for`` and ``with``
targets, and a module-level container filled by ``.update``/``.append``/item assignment), and reports one row per
statement in that closure.

The ratchet counts *named values*: the distinct names those statements assign, minus

- the roots themselves;
- lowercase ``*_cfg`` / ``*_config`` holders (an intermediate that carries one section to the constants after it);
- ``NOT_SETTINGS``: runtime objects that a setting feeds but that are not settings (stores, probes, a problem record).

A statement that assigns nothing but passes a root to a call (``app.add_middleware(X, limits=Limits.from_config(config))``)
is one counted value, named ``<callee>@<line>``: the call reads the config once at import and keeps what it read.

The scan cannot see a read inside a function that a module-level statement calls. ``HIDDEN`` lists those names by
hand; each must still be assigned at module level, which the test checks.

Each counted name belongs to one source section: the section its own statement reads, or the section that reached it
through the names it reads. When a name sees several sections, the alphabetically first one owns it (stable, and
shown in the report); ``whole_config`` is the section of a name built from the whole config mapping.

Run ``python scripts/config_import_reads.py [proxy.py] [--rows]`` to print the table.
"""
from __future__ import annotations

import ast
import collections
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ROOTS = {"config", "auth_cfg", "admin_cfg", "_bootstrap_config"}
# Names whose call or attribute reads the published snapshot: a read through them is a root read too.
SNAPSHOT_ROOTS = {"runtime", "CONFIG_HOME"}
# ``runtime().config`` is the snapshot's config mapping; a ``.get("k")`` on it reads section k like a root does.
RUNTIME_CONFIG = "runtime().config"
# A callee that reads one named section from the whole config it is handed (the scan cannot look inside it).
CALLEE_SECTIONS = {"RequestBodyLimits.from_config": "request_body_limits"}
MUTATORS = {"update", "append", "extend", "add", "setdefault", "insert", "appendleft", "extendleft"}
# A root that is itself one section: ``auth_cfg = config.get("auth", {})``.
ROOT_SECTION = {"auth_cfg": "auth", "admin_cfg": "admin"}
WHOLE_CONFIG = "whole_config"

# Runtime objects built from settings. They are not settings, so they are not ratchet entries.
NOT_SETTINGS = frozenset({
    "ADMIN_AUTH", "ADMIN_STORE", "CONFIG_PROBLEMS", "REGISTRY_APPLY_PROBLEM", "SIGNIN_API",
    "STT_MEASUREMENTS", "_public_conflicts", "_removed_auth",
})
# Config-derived values computed inside a function that a module-level statement calls: the scan cannot see them.
HIDDEN = {
    "ADMIN_OIDC_CONFIG": "admin",  # the file fallback of the stored OIDC provider
}


@dataclass(frozen=True)
class Row:
    line: int
    targets: tuple[str, ...]
    reads: tuple[str, ...]
    sections: tuple[str, ...]  # sections this statement reads directly, plus the ones its names carry
    source: str


@dataclass(frozen=True)
class Inventory:
    rows: tuple[Row, ...]
    assigned: frozenset[str]  # every name the closure assigns, counted or not
    names: dict[str, str]  # counted name -> owning section (scanned names, then hidden ones)
    scanned: int  # counted names the scan found
    statements: int  # statements in the closure
    direct_statements: int  # statements that read a root name


def _executed(node: ast.AST) -> Iterator[ast.AST]:
    """``node`` and what runs when it is evaluated: a lambda or a def runs only its defaults and decorators."""
    yield node
    if isinstance(node, ast.Lambda):
        inner = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inner = [*node.decorator_list, *node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
    else:
        inner = list(ast.iter_child_nodes(node))
    for child in inner:
        yield from _executed(child)


@dataclass
class _Unit:
    """One thing that runs at import: expressions to read, and the names it binds."""

    line: int
    exprs: list[ast.AST]
    targets: list[str] = field(default_factory=list)
    call: ast.Call | None = None  # set for a bare call statement
    # name -> the sub-expression it is assigned from, for `X, Y = a, b` (a tuple target paired with a tuple value)
    own: dict[str, ast.AST] = field(default_factory=dict)
    label: str = ""  # set for a def whose decorators or defaults run


def _store_names(node: ast.AST, prefix: str = "") -> list[str]:
    return [prefix + n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _paired(target: ast.AST, value: ast.AST, prefix: str) -> dict[str, ast.AST]:
    """The names of a tuple/list ``target`` that line up one-to-one with a tuple/list ``value``, with the part of
    ``value`` each is assigned from. Empty when the shapes do not line up (a starred name, a call returning a tuple)."""
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        if len(target.elts) != len(value.elts) or any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts)):
            return {}
        out: dict[str, ast.AST] = {}
        for t, v in zip(target.elts, value.elts):
            out.update(_paired(t, v, prefix))
        return out
    if isinstance(target, ast.Name):
        return {prefix + target.id: value}
    return {}


def _walrus_targets(exprs: list[ast.AST], prefix: str) -> list[str]:
    return [
        prefix + child.target.id for expr in exprs for child in _executed(expr)
        if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name)
    ]


def _units(body, prefix: str = "") -> Iterator[_Unit]:
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _Unit(st.lineno, [st], label=f"def {prefix}{st.name}")
        elif isinstance(st, ast.ClassDef):
            yield _Unit(st.lineno, [*st.decorator_list, *st.bases, *[k.value for k in st.keywords]], label=f"class {prefix}{st.name}")
            yield from _units(st.body, f"{prefix}{st.name}.")
        elif isinstance(st, (ast.If, ast.While)):
            yield _Unit(st.lineno, [st.test], _walrus_targets([st.test], prefix))
            yield from _units(st.body, prefix)
            yield from _units(st.orelse, prefix)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            yield _Unit(st.lineno, [st.iter], _store_names(st.target, prefix) + _walrus_targets([st.iter], prefix))
            yield from _units(st.body, prefix)
            yield from _units(st.orelse, prefix)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            exprs = [item.context_expr for item in st.items]
            names = [n for item in st.items if item.optional_vars is not None for n in _store_names(item.optional_vars, prefix)]
            yield _Unit(st.lineno, exprs, names + _walrus_targets(exprs, prefix))
            yield from _units(st.body, prefix)
        elif isinstance(st, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            yield from _units(st.body, prefix)
            for handler in st.handlers:
                if handler.type is not None:
                    yield _Unit(handler.lineno, [handler.type])
                yield from _units(handler.body, prefix)
            yield from _units(st.orelse, prefix)
            yield from _units(st.finalbody, prefix)
        elif isinstance(st, ast.Match):
            yield _Unit(st.lineno, [st.subject], _walrus_targets([st.subject], prefix))
            for case in st.cases:
                yield from _units(case.body, prefix)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            raw = st.targets if isinstance(st, ast.Assign) else [st.target]
            own: dict[str, ast.AST] = {}
            if isinstance(st, ast.Assign):
                for t in raw:
                    own.update(_paired(t, st.value, prefix))
            yield _Unit(st.lineno, [st], [n for t in raw for n in _store_names(t, prefix)] + _walrus_targets([st], prefix), own=own)
        elif isinstance(st, ast.Expr):
            call = st.value if isinstance(st.value, ast.Call) else None
            container = []
            if (call is not None and isinstance(call.func, ast.Attribute) and call.func.attr in MUTATORS
                    and isinstance(call.func.value, ast.Name)):
                container = [prefix + call.func.value.id]  # X.update(...) fills a container the module holds
            yield _Unit(st.lineno, [st], container + _walrus_targets([st], prefix), call=call if not container else None)
        else:
            yield _Unit(st.lineno, [st], _walrus_targets([st], prefix))


def _module_statements(body):
    """The units of ``body`` as ``(node, is_header)`` pairs; kept for the callers that only need what runs."""
    for unit in _units(body):
        yield unit, False


def _is_runtime_config(node: ast.AST) -> bool:
    """``runtime().config``: the snapshot's config mapping."""
    return (isinstance(node, ast.Attribute) and node.attr == "config" and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "runtime")


def _loaded_names(nodes: list[ast.AST]) -> set[str]:
    """Names read when ``nodes`` run. ``runtime`` and ``CONFIG_HOME`` count only when called or read from; handing the
    ``runtime`` function itself to something (``current=runtime``) reads no config."""
    out: set[str] = set()
    for node in nodes:
        children = list(_executed(node))
        reads_snapshot = {
            id(child.func) for child in children if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        } | {id(child.value) for child in children if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)}
        for child in children:
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in SNAPSHOT_ROOTS or id(child) in reads_snapshot:
                    out.add(child.id)
    return out


def _direct_sections(nodes: list[ast.AST], derived: set[str]) -> set[str]:
    """Sections named by ``X.get("k")`` or ``X["k"]`` where X is a derived name or ``runtime().config``."""
    found: set[str] = set()
    for root in nodes:
        for child in _executed(root):
            base = key = None
            owner: ast.AST | None = None
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "get"
                    and child.args and isinstance(child.args[0], ast.Constant)):
                owner, key = child.func.value, child.args[0].value
            elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
                owner, key = child.value, child.slice.value
            if owner is None or not isinstance(key, str):
                continue
            if isinstance(owner, ast.Name) and owner.id in derived:
                base = owner.id
            elif _is_runtime_config(owner):
                base = RUNTIME_CONFIG
            if base is None:
                continue
            found.add(ROOT_SECTION.get(base) or (key if base in ROOTS or base == RUNTIME_CONFIG else ""))
    found.discard("")
    return found


def _is_holder(name: str) -> bool:
    return name == name.lower() and name.lstrip("_").endswith(("_cfg", "_config"))


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "<expr>")
    return ".".join(reversed(parts))


def _sections_of(nodes: list[ast.AST], derived: set[str], carried: dict[str, set[str]]) -> set[str]:
    """The sections ``nodes`` read: named directly, carried by a derived name they read, or known from the callee."""
    sections = _direct_sections(nodes, derived)
    for name in _loaded_names(nodes) & (derived | SNAPSHOT_ROOTS):
        sections |= carried.get(name, set())
    sections |= {
        CALLEE_SECTIONS[_dotted(child.func)] for expr in nodes for child in _executed(expr)
        if isinstance(child, ast.Call) and _dotted(child.func) in CALLEE_SECTIONS
    }
    return sections or {WHOLE_CONFIG}


def scan(source: str, hidden: dict[str, str] | None = None) -> Inventory:
    """Scan ``source``. ``hidden`` is the hand-listed names for proxy.py (``HIDDEN``); pass ``{}`` for a fragment."""
    hidden = HIDDEN if hidden is None else hidden
    tree = ast.parse(source)
    derived = set(ROOTS)
    sources = ROOTS | SNAPSHOT_ROOTS  # names whose read starts a closure
    carried: dict[str, set[str]] = {name: {ROOT_SECTION[name]} for name in ROOT_SECTION}
    rows: list[Row] = []
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for unit in _units(tree.body):
        reads = _loaded_names(unit.exprs) & (derived | SNAPSHOT_ROOTS)
        if not reads:
            continue
        targets = list(dict.fromkeys(unit.targets))
        sections = _sections_of(unit.exprs, derived, carried)
        passed: list[ast.AST] = []
        if unit.call is not None:
            passed = [*unit.call.args, *[k.value for k in unit.call.keywords]]
        if not targets and (passed or unit.label):
            # a call that is handed a root, or a def whose decorators or defaults read one: it keeps what it read
            at_root = _loaded_names(passed or unit.exprs) & sources
            if at_root:
                targets = [f"{_dotted(unit.call.func) if unit.call is not None else unit.label}@{unit.line}"]
        for target in targets:
            mine = sections
            if target in unit.own:  # `X, Y = a, b`: X belongs to what `a` reads, and to nothing if `a` reads no config
                if not _loaded_names([unit.own[target]]) & (derived | SNAPSHOT_ROOTS):
                    continue
                mine = _sections_of([unit.own[target]], derived, carried)
            derived.add(target)
            carried.setdefault(target, set()).update(mine)
            owners[target].update(mine)
        rows.append(Row(unit.line, tuple(targets), tuple(sorted(reads)), tuple(sorted(sections)),
                        (" ".join((ast.get_source_segment(source, unit.exprs[0]) or "").split()))[:160]))
    counted = {
        name: sorted(sections)[0] for name, sections in owners.items()
        if name not in ROOTS and name not in NOT_SETTINGS and not _is_holder(name)
    }
    names = dict(counted)
    for name, section in hidden.items():
        names.setdefault(name, section)
    return Inventory(
        rows=tuple(rows), assigned=frozenset(owners), names=names, scanned=len(counted), statements=len(rows),
        direct_statements=sum(1 for row in rows if set(row.reads) & sources),
    )


def assigned_at_module_level(source: str) -> set[str]:
    """Every name an import-time statement of ``source`` assigns (used to check HIDDEN and NOT_SETTINGS)."""
    out: set[str] = set()
    for unit in _units(ast.parse(source).body):
        out.update(unit.targets)
    return out


def per_section(inventory: Inventory) -> dict[str, int]:
    return dict(sorted(collections.Counter(inventory.names.values()).items()))


def main(argv: list[str]) -> int:
    paths = [a for a in argv if not a.startswith("--")]
    path = Path(paths[0]) if paths else Path(__file__).resolve().parent.parent / "proxy.py"
    inventory = scan(path.read_text(encoding="utf-8"))
    if "--rows" in argv:
        for row in inventory.rows:
            print(f"{row.line}\t{','.join(row.targets)}\t{','.join(row.sections)}\t{row.source[:80]}")
    for name, section in sorted(inventory.names.items(), key=lambda item: (item[1], item[0])):
        print(f"{section}\t{name}")
    print(f"statements_in_closure\t{inventory.statements}\tdirect\t{inventory.direct_statements}")
    print(f"scanned\t{inventory.scanned}\thidden\t{len(inventory.names) - inventory.scanned}\ttotal\t{len(inventory.names)}")
    for section, count in per_section(inventory).items():
        print(f"import_time_config_reads.{section}\t{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
