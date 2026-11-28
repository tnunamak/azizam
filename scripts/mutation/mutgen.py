"""Generate source mutants for scripts/mutate (docs/design/TEST_STRATEGY.md 4.5).

Each mutant is a text splice at an AST position: one operator, constant,
condition or statement changed, so the mutated file differs from the original
in exactly one place.  Docstrings, annotations, decorators, f-string text,
logging calls and lines marked `pragma: no mutate` are not mutated.

A mutant's ID is `path:line:kind:old->new`, plus `#n` when one line holds the
same change more than once.  It does not depend on column, on other lines, or
on the order of generation, so an ID still names the same mutant after edits
elsewhere in the file and can key the survivor classifications in
tests/mutation/survivors.toml.
"""
import ast
import hashlib
import re
import sys

CMP = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
       ast.Gt: (">", ">="), ast.GtE: (">=", ">"), ast.Is: ("is", "is not"), ast.IsNot: ("is not", "is"),
       ast.In: ("in", "not in"), ast.NotIn: ("not in", "in")}
BIN = {ast.Add: ("+", "-"), ast.Sub: ("-", "+"), ast.Mult: ("*", "/"), ast.Div: ("/", "*"),
       ast.FloorDiv: ("//", "*"), ast.Mod: ("%", "*")}
LOGMETHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def offsets(src):
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line.encode()))
    return starts


class Gen(ast.NodeVisitor):
    def __init__(self, src):
        self.src = src
        self.b = src.encode()
        self.starts = offsets(src)
        self.lines = src.splitlines()
        self.muts = []
        self.stack = []
        self.func = []
        self.skip_depth = 0

    def pos(self, n):
        return (self.starts[n.lineno - 1] + n.col_offset, self.starts[n.end_lineno - 1] + n.end_col_offset)

    def nomut(self, lineno):
        l = self.lines[lineno - 1]
        return "pragma: no mutate" in l or "pragma: no cover" in l

    def add(self, s, e, new, op, node):
        old = self.b[s:e].decode()
        if old == new:
            return
        self.muts.append(dict(start=s, end=e, new=new, old=old, op=op, line=node.lineno,
                              func=".".join(self.func) or "<module>"))

    def generic_visit(self, node):
        super().generic_visit(node)

    def visit_FunctionDef(self, n):
        # skip annotations & decorators from mutation; visit defaults and body
        self.func.append(n.name)
        for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
            self.visit(d)
        body = n.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        for s in body:
            self.visit(s)
        self.func.pop()
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, n):
        self.func.append(n.name)
        body = n.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        for s in body:
            self.visit(s)
        self.func.pop()

    def visit_AnnAssign(self, n):
        if n.value is not None:
            self.visit(n.value)

    def visit_Expr(self, n):
        v = n.value
        if isinstance(v, ast.Constant):  # docstring / bare literal
            return
        if isinstance(v, ast.Call) and not self.nomut(n.lineno):
            f = v.func
            is_log = isinstance(f, ast.Attribute) and f.attr in LOGMETHODS
            if not is_log and self.func:
                s, e = self.pos(n)
                self.add(s, e, "pass", "stmt-delete", n)
        self.generic_visit(n)

    def visit_JoinedStr(self, n):
        # don't mutate string fragments of f-strings; do visit expressions
        for v in n.values:
            if isinstance(v, ast.FormattedValue):
                self.visit(v.value)

    def visit_Call(self, n):
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in LOGMETHODS and isinstance(f.value, (ast.Name, ast.Attribute)):
            return  # logging args are not behaviour
        self.generic_visit(n)

    def visit_Compare(self, n):
        if not self.nomut(n.lineno):
            left = n.left
            for op, comp in zip(n.ops, n.comparators):
                t = type(op)
                if t in CMP:
                    ls = self.pos(left)[1]
                    cs = self.pos(comp)[0]
                    seg = self.b[ls:cs].decode()
                    old, new = CMP[t]
                    m = re.search(r"(?<![\w])" + re.escape(old).replace(r"\ ", r"\s+") + r"(?![\w])" if old[0].isalpha() else re.escape(old), seg)
                    if m:
                        self.add(ls + len(seg[:m.start()].encode()), ls + len(seg[:m.end()].encode()), new, f"cmp:{old}->{new}", n)
                left = comp
        self.generic_visit(n)

    def visit_BoolOp(self, n):
        if not self.nomut(n.lineno):
            old, new = ("and", "or") if isinstance(n.op, ast.And) else ("or", "and")
            for a, b in zip(n.values, n.values[1:]):
                ls = self.pos(a)[1]; cs = self.pos(b)[0]
                seg = self.b[ls:cs].decode()
                m = re.search(r"\b" + old + r"\b", seg)
                if m:
                    self.add(ls + len(seg[:m.start()].encode()), ls + len(seg[:m.end()].encode()), new, f"bool:{old}->{new}", n)
        self.generic_visit(n)

    def visit_UnaryOp(self, n):
        if isinstance(n.op, ast.Not) and not self.nomut(n.lineno):
            s, e = self.pos(n); os_, oe = self.pos(n.operand)
            self.add(s, os_, "", "not-removed", n)
        self.generic_visit(n)

    def visit_BinOp(self, n):
        t = type(n.op)
        if t in BIN and not self.nomut(n.lineno) and not (isinstance(n.left, ast.Constant) and isinstance(n.left.value, str)):
            ls = self.pos(n.left)[1]; cs = self.pos(n.right)[0]
            seg = self.b[ls:cs].decode()
            old, new = BIN[t]
            i = seg.find(old)
            if i >= 0:
                self.add(ls + len(seg[:i].encode()), ls + len(seg[:i + len(old)].encode()), new, f"arith:{old}->{new}", n)
        self.generic_visit(n)

    def visit_AugAssign(self, n):
        t = type(n.op)
        if t in (ast.Add, ast.Sub) and not self.nomut(n.lineno):
            s, e = self.pos(n); seg = self.b[s:e].decode()
            old = "+=" if t is ast.Add else "-="; new = "-=" if t is ast.Add else "+="
            i = seg.find(old)
            if i >= 0:
                self.add(s + len(seg[:i].encode()), s + len(seg[:i + 2].encode()), new, f"aug:{old}->{new}", n)
        self.generic_visit(n)

    def visit_Constant(self, n):
        if self.nomut(n.lineno):
            return
        v = n.value; s, e = self.pos(n)
        if isinstance(v, bool):
            self.add(s, e, "False" if v else "True", "bool-const", n)
        elif isinstance(v, int):
            self.add(s, e, str(v + 1), "int-const", n)
        elif isinstance(v, float):
            self.add(s, e, repr(v + 1.0), "float-const", n)
        elif isinstance(v, str):
            raw = self.b[s:e].decode()
            if raw[:1] in "'\"" and "\n" not in raw and not raw.startswith(('"""', "'''")):
                q = raw[0]; inner = raw[1:-1]
                self.add(s, e, f"{q}XX{inner}XX{q}" if inner else f"{q}XXXX{q}", "str-const", n)

    def visit_Return(self, n):
        if n.value is not None and not self.nomut(n.lineno) and not (isinstance(n.value, ast.Constant) and n.value.value is None):
            s, e = self.pos(n.value)
            self.add(s, e, "None", "return->None", n)
        self.generic_visit(n)

    def _cond(self, n):
        if not self.nomut(n.lineno):
            s, e = self.pos(n.test)
            if not (isinstance(n.test, ast.Constant)):
                self.add(s, e, f"not ({self.b[s:e].decode()})", "cond-negated", n)
        self.generic_visit(n)
    visit_If = _cond
    visit_While = _cond

    def visit_Continue(self, n):
        s, e = self.pos(n); self.add(s, e, "break", "continue->break", n)

    def visit_Break(self, n):
        s, e = self.pos(n); self.add(s, e, "continue", "break->continue", n)


def _snippet(text):
    text = " ".join(text.split())
    if len(text) <= 48:
        return text
    return text[:40] + "..." + hashlib.sha256(text.encode()).hexdigest()[:8]


def generate(path, relpath=None, functions=None):
    """Return (source, mutants) for the file at `path`.

    `relpath` is the path written into IDs (default `path`).  `functions`, when
    given, keeps only mutants inside those top-level functions or classes.
    """
    src = open(path).read()
    tree = ast.parse(src)
    g = Gen(src)
    # skip `if TYPE_CHECKING` blocks & __all__
    for node in tree.body:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            continue
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if isinstance(node, ast.Expr):
            continue
        g.visit(node)
    # dedupe & validity check
    out, seen = [], set()
    b = src.encode()
    for m in g.muts:
        key = (m["start"], m["end"], m["new"])
        if key in seen:
            continue
        seen.add(key)
        new_src = (b[:m["start"]] + m["new"].encode() + b[m["end"]:]).decode()
        try:
            compile(new_src, path, "exec")
        except SyntaxError:
            continue
        out.append(m)
    out.sort(key=lambda m: (m["start"], m["end"], m["new"]))
    relpath = relpath or path
    occurrences = {}
    for m in out:
        m["kind"] = m["op"].split(":")[0]
        m["end_line"] = b[:m["end"]].count(b"\n") + 1
        base = f'{relpath}:{m["line"]}:{m["kind"]}:{_snippet(m["old"])}->{_snippet(m["new"])}'
        occurrences[base] = occurrences.get(base, 0) + 1
        m["id"] = base if occurrences[base] == 1 else f"{base}#{occurrences[base]}"
    if functions is not None:
        out = [m for m in out if m["func"].split(".")[0] in functions]
    return src, out


def apply(src, mutant):
    """Return `src` with `mutant` spliced in."""
    b = src.encode()
    return (b[:mutant["start"]] + mutant["new"].encode() + b[mutant["end"]:]).decode()


if __name__ == "__main__":
    from collections import Counter
    _, mutants = generate(sys.argv[1])
    print(len(mutants), Counter(m["kind"] for m in mutants))
