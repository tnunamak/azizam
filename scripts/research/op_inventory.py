#!/usr/bin/env python3
"""Operation-vocabulary and implicit-coupling inventory for Azizam.

Deterministic, re-runnable, read-only. Stdlib ast/re/json plus PyYAML
(the repository already uses PyYAML).

Usage:
    python scripts/research/op_inventory.py <tree> <outdir>
    python scripts/research/op_inventory.py <tree> <outdir> --notes <path>
    python scripts/research/op_inventory.py --self-test

Outputs (into <outdir>; the JSON outputs stay out of the patch):
    inventory.json  everything extracted, machine-readable
    pairs.jsonl     one line per candidate pair of distinct vocabulary words

The --notes path (e.g. docs/reviews/c2d2-op-inventory-muse-1113.md) gets the
human-readable summary described in the assignment brief.
"""

import ast
import bisect
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Vocabulary seeding: operation-like keys in data files.
# ---------------------------------------------------------------------------

# String values (or string items of lists) at these keys are vocabulary words.
VALUE_KEYS = frozenset({
    "intent", "intents", "operation", "operations",
    "execution_intents", "route_intents",
    "capabilities", "capability", "task", "tasks", "serves", "kind",
    "modality", "modalities", "input_modalities", "output_modalities",
    "modality_words", "media_type_modalities",
    "protocol_words", "type_capabilities",
    "receipt_protocols", "policy_protocol",
    "shared_operations", "hidden_by", "recommended_alias",
})

# Contextual keys: collected only in the stated context.
ID_PARENT_LISTS = frozenset({"capabilities", "alias_operations", "rows"})
NAME_PARENT_LISTS = frozenset({"alias_operations"})

# Data files that seed the vocabulary.
SEED_GLOBS = [
    ("registry", (".yml", ".yaml", ".json")),
    ("manifests/workflows", (".yml", ".yaml")),
    ("providers/catalog", (".yml", ".yaml")),
]

# Production code scanned for vocabulary literals.
TS_SKIP_SUFFIXES = (".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx")

# ---------------------------------------------------------------------------
# Implicit-coupling markers.
# ---------------------------------------------------------------------------

# Identifier-like markers: whole-word match. Prefix/substring markers:
# activation_, x-litellm, nvidia-smi, <think>.
IDENT_MARKERS = [
    "class_type", "extra_pnginfo", "SaveImage", "LoadImage",
    "widgets_values", "model_path", "gguf", "mmproj",
    "response_cost", "chat_template_kwargs", "enable_thinking",
    "GPU0", "GPU1",
]
SUBSTRING_MARKERS = ["activation_", "x-litellm", "nvidia-smi", "<think>"]
# Endpoint path literals, each written with a leading slash in code.
ENDPOINT_MARKERS = [
    "/history", "/prompt", "/view", "/object_info",
    "/upload/image", "/system_stats", "/props",
]

ENGINE_NAMES = ["llama", "vllm", "comfy", "ollama", "kobold", "sglang",
                 "safetensors"]


def marker_patterns():
    """name -> compiled regex, in a fixed order."""
    pats = {}
    for name in IDENT_MARKERS:
        pats[name] = re.compile(r"\b" + re.escape(name) + r"\b")
    for name in SUBSTRING_MARKERS:
        pats[name] = re.compile(re.escape(name))
    for path in ENDPOINT_MARKERS:
        # A full path segment: not followed by a word character, so
        # "/view" does not match "/overview" and "/prompt" not "/prompts".
        pats[path] = re.compile(re.escape(path) + r"(?![A-Za-z0-9_])")
    return pats


MARKER_PATTERNS = marker_patterns()
ALL_MARKERS = list(MARKER_PATTERNS)

BLIND_SPOTS = [
    "Dynamic strings: f-strings, concatenation, .format(), and values built "
    "at runtime are not seen; only string constants are scanned.",
    "Unquoted TypeScript object keys (e.g. {chat: ...}) are not string "
    "literals, so they are not counted as occurrences or co-mappings.",
    "YAML/JSON values at keys outside the operation-like key set are not "
    "seeded, even when they name an operation (see Decisions in the notes).",
    "Words that appear only in comments, docstrings, tests, or docs are "
    "not counted as code occurrences (comments are skipped in the TS scan; "
    "docstrings are string constants, so a vocabulary word in a docstring "
    "does count).",
    "Closed sets spanning a call boundary (e.g. set(x) | set(y)) are not "
    "reconstructed; only one literal is reported.",
    "A vocabulary word used as a bare identifier (not a string literal) in "
    "Python is not a code occurrence; markers are the exception, because "
    "they are scanned textually.",
]


# ---------------------------------------------------------------------------
# Seeding from data files.
# ---------------------------------------------------------------------------

def _join_path(segments):
    """['rows[3]', 'intent'] -> 'rows[3].intent'."""
    return ".".join(segments)


def _seed_walk(node, segments, parent_list_key, vocab, data_occ, rel):
    """Walk parsed YAML/JSON; collect vocabulary words at operation keys."""
    if isinstance(node, dict):
        table = segments[0].split("[")[0] if segments else rel
        for key, value in node.items():
            key_path = _join_path(segments + [str(key)])
            if isinstance(key, str) and key in VALUE_KEYS:
                if isinstance(value, str):
                    _record_seed(value, key_path, table, vocab, data_occ, rel)
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, str):
                            _record_seed(item, key_path + "[%d]" % i, table,
                                         vocab, data_occ, rel)
                elif key == "media_type_modalities" \
                        and isinstance(value, dict):
                    # The one mapping-valued operation key: MIME type ->
                    # modality word. Other dict values are not vocabulary;
                    # the walk below still visits nested operation keys.
                    for sub_k, sub_v in value.items():
                        if isinstance(sub_v, str):
                            _record_seed(sub_v, key_path + "." + str(sub_k),
                                         table, vocab, data_occ, rel)
            if (isinstance(key, str) and key == "id"
                    and isinstance(value, str)
                    and (parent_list_key in ID_PARENT_LISTS
                         or any(k in VALUE_KEYS for k in node))):
                _record_seed(value, key_path, table, vocab, data_occ, rel)
            if (isinstance(key, str) and key == "name"
                    and isinstance(value, str)
                    and parent_list_key in NAME_PARENT_LISTS):
                _record_seed(value, key_path, table, vocab, data_occ, rel)
            _seed_walk(value, segments + [str(key)], parent_list_key,
                       vocab, data_occ, rel)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            seg = (segments[:-1] + [segments[-1] + "[%d]" % i]) if segments \
                else ["[%d]" % i]
            _seed_walk(item, seg, segments[-1].split("[")[0] if segments
                       else None, vocab, data_occ, rel)


def _record_seed(word, key_path, table, vocab, data_occ, rel):
    word = word.strip()
    if not word:
        return
    vocab.add(word)
    data_occ.setdefault(word, []).append(
        {"file": rel, "key_path": key_path, "table": table})


def _data_comapping_walk(node, segments, vocab, pairs):
    """Mappings in data where one vocabulary word maps to another."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key in vocab:
                targets = []
                if isinstance(value, str):
                    targets = [value]
                elif isinstance(value, list):
                    targets = [v for v in value if isinstance(v, str)]
                for target in targets:
                    if target in vocab and target != key:
                        pairs.setdefault((key, target), set()).add(
                            "data mapping %s" % ".".join(segments + [key]))
            _data_comapping_walk(value, segments + [str(key)], vocab, pairs)
    elif isinstance(node, list):
        for item in node:
            _data_comapping_walk(item, segments, vocab, pairs)


def seed_vocabulary(tree):
    """Return (vocab set, data_occurrences dict, parsed data docs, errors)."""
    vocab = set()
    data_occ = {}
    docs = []  # (rel path, parsed document)
    errors = []
    for subdir, suffixes in SEED_GLOBS:
        base = tree / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            rel = str(path.relative_to(tree))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                doc = yaml.safe_load(text)
            except Exception as exc:  # noqa: BLE001 - record and continue
                errors.append({"file": rel, "error": "%s: %s"
                               % (type(exc).__name__, exc)})
                continue
            if doc is None:
                continue
            docs.append((rel, doc))
            _seed_walk(doc, [], None, vocab, data_occ, rel)
    return vocab, data_occ, docs, errors


# ---------------------------------------------------------------------------
# Python code scan (ast).
# ---------------------------------------------------------------------------

COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def _const_words(node):
    """Vocabulary words among a node's direct string constants."""
    words = set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words.add(node.value)
    return words


def _collection_words(node):
    """Vocabulary words held directly by a Set/List/Tuple/Dict literal."""
    words = set()
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        for elt in node.elts:
            words |= _const_words(elt)
    elif isinstance(node, ast.Dict):
        for key in node.keys:
            if key is not None:
                words |= _const_words(key)
        for value in node.values:
            words |= _const_words(value)
    return words


def _compare_words(test):
    """Group vocabulary words compared against one expression in a test.

    Returns {expression source: set(words)} for ==, !=, in, not in.
    """
    groups = {}
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        expr = ast.unparse(node.left)
        found = set()
        for op, comp in zip(node.ops, node.comparators):
            if not isinstance(op, COMPARE_OPS):
                continue
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
            elif isinstance(comp, (ast.Set, ast.List, ast.Tuple)):
                for elt in comp.elts:
                    found |= _const_words(elt)
        if found:
            groups.setdefault(expr, set()).update(found)
    return groups


def scan_python_file(path, rel, vocab):
    """Scan one Python file. Returns dicts of findings (relative paths)."""
    out = {"occurrences": [], "closed_sets": [], "if_chains": [],
           "match_statements": [], "co_mappings": [], "parse_error": None}
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=rel)
    except Exception as exc:  # noqa: BLE001 - record and continue
        out["parse_error"] = {"file": rel, "error": "%s: %s"
                              % (type(exc).__name__, exc)}
        return out
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node):
        names = []
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                names.append(cur.name)
        return ".".join(reversed(names)) if names else "<module>"

    def context_of(node):
        parent = parents.get(node)
        if parent is None:
            return "other"
        if isinstance(parent, ast.Dict):
            if any(k is node for k in parent.keys):
                return "dict-key"
            return "dict-value"
        if isinstance(parent, ast.Compare):
            return "compare"
        if isinstance(parent, ast.MatchValue):
            return "compare"
        if isinstance(parent, ast.Set):
            return "set member"
        if isinstance(parent, ast.List):
            grand = parents.get(parent)
            if (isinstance(grand, ast.Call)
                    and isinstance(grand.func, ast.Name)
                    and grand.func.id == "frozenset"):
                return "frozenset member"
            if (isinstance(grand, ast.Call)
                    and isinstance(grand.func, ast.Name)
                    and grand.func.id == "set"):
                return "set member"
            return "list member"
        if isinstance(parent, ast.Tuple):
            grand = parents.get(parent)
            if (isinstance(grand, ast.Call)
                    and isinstance(grand.func, ast.Name)
                    and grand.func.id in ("frozenset", "set")):
                return grand.func.id + " member"
            return "tuple member"
        if isinstance(parent, ast.Subscript):
            return "subscript"
        if isinstance(parent, ast.Return):
            return "return"
        if isinstance(parent, ast.Call):
            return "call-arg"
        if isinstance(parent, ast.keyword):
            return "call-arg"
        if isinstance(parent, ast.JoinedStr):
            return "other"
        return "other"

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in vocab:
            out["occurrences"].append({
                "file": rel, "line": node.lineno,
                "enclosing": enclosing(node), "context": context_of(node),
                "word": node.value})
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            words = _collection_words(node)
            words &= vocab
            if len(words) >= 2:
                kind = {ast.Set: "set", ast.List: "list",
                        ast.Tuple: "tuple"}[type(node)]
                parent = parents.get(node)
                if (isinstance(parent, ast.Call)
                        and isinstance(parent.func, ast.Name)
                        and parent.func.id in ("frozenset", "set")):
                    kind = parent.func.id
                out["closed_sets"].append({
                    "file": rel, "line": node.lineno, "kind": kind,
                    "words": sorted(words)})
        elif isinstance(node, ast.Dict):
            words = _collection_words(node) & vocab
            if len(words) >= 2:
                out["closed_sets"].append({
                    "file": rel, "line": node.lineno, "kind": "dict",
                    "words": sorted(words)})
            for key, value in zip(node.keys, node.values):
                if key is None or not isinstance(key, ast.Constant) \
                        or not isinstance(key.value, str) \
                        or key.value not in vocab:
                    continue
                targets = set()
                if isinstance(value, ast.Constant) \
                        and isinstance(value.value, str):
                    targets = {value.value}
                elif isinstance(value, (ast.Set, ast.List, ast.Tuple)):
                    for elt in value.elts:
                        targets |= _const_words(elt)
                for target in sorted(targets & vocab):
                    if target != key.value:
                        out["co_mappings"].append({
                            "a": key.value, "b": target,
                            "evidence": "code dict %s:%d"
                            % (rel, node.lineno)})
        elif isinstance(node, ast.If):
            parent = parents.get(node)
            if isinstance(parent, ast.If) and len(parent.orelse) == 1 \
                    and parent.orelse[0] is node:
                continue  # elif: handled as part of the head If's chain
            chain = [node]
            cur = node
            while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
                chain.append(cur)
            # One expression compared across the whole chain counts together.
            merged = {}
            for if_node in chain:
                for expr, words in _compare_words(if_node.test).items():
                    merged.setdefault(expr, set()).update(words & vocab)
            for expr, words in merged.items():
                if len(words) >= 2:
                    out["if_chains"].append({
                        "file": rel, "line": node.lineno,
                        "expression": expr[:120], "words": sorted(words)})
        elif isinstance(node, ast.Match):
            words = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.MatchValue) \
                        and isinstance(sub.value, ast.Constant) \
                        and isinstance(sub.value.value, str):
                    words.add(sub.value.value)
            words &= vocab
            if len(words) >= 2:
                out["match_statements"].append({
                    "file": rel, "line": node.lineno,
                    "subject": ast.unparse(node.subject)[:120],
                    "words": sorted(words)})
    return out

# ---------------------------------------------------------------------------
# TypeScript scan (regular expressions / character scanner).
# ---------------------------------------------------------------------------

_TS_STRING_RE = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""")
_TS_DECL_RE = re.compile(
    r"(?:class|function)\s+([A-Za-z_$][\w$]*)"
    r"|const\s+([A-Za-z_$][\w$]*)\s*="
    r"|([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")
_CMP_BEFORE_RE = re.compile(r"(===?|!==?|==|!=)\s*$")
_CMP_AFTER_RE = re.compile(r"^\s*(===?|!==?|==|!=)")


def _ts_tokens(text):
    """Yield string tokens: (content, line, stack, prev_char, next_char).

    stack is a list of (bracket, span_id, line) open at the token. Comments
    are skipped; template-literal chunks between ${...} are separate tokens.
    """
    tokens = []
    spans = {}  # span_id -> [open_char, line, close_pos]
    stack = []
    next_span = 0
    i, n = 0, len(text)
    line = 1

    def note_string(content, start_line, start_i, end_i):
        prev = text[:start_i].rstrip()
        nxt = text[end_i:].lstrip()
        tokens.append({
            "content": content,
            "line": start_line,
            "offset": start_i,
            "stack": list(stack),
            "prev": prev[-3:] if prev else "",
            "next": nxt[:3] if nxt else "",
        })

    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if ch in "[{(":
            next_span += 1
            spans[next_span] = [ch, line, None]
            stack.append((ch, next_span, line))
            i += 1
            continue
        if ch in "]})":
            if stack:
                _, span_id, _ = stack.pop()
                spans[span_id][2] = i
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            j = i + 1
            buf = []
            while j < n and text[j] != quote:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    if text[j] == "\n":
                        break
                    buf.append(text[j])
                    j += 1
            if j < n and text[j] == quote:
                note_string("".join(buf), line, i, j + 1)
                i = j + 1
            else:
                i = j
            continue
        if ch == "`":
            j = i + 1
            chunk_start = j
            chunk_line = line
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "`":
                    note_string(text[chunk_start:j], chunk_line, i, j + 1)
                    j += 1
                    break
                if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                    note_string(text[chunk_start:j], chunk_line, i, j)
                    # Skip the interpolation; resume after its closing }.
                    depth = 1
                    j += 2
                    while j < n and depth:
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                        elif text[j] == "\n":
                            line += 1
                        j += 1
                    chunk_start = j
                    chunk_line = line
                    continue
                if text[j] == "\n":
                    line += 1
                j += 1
            i = j
            continue
        i += 1
    return tokens, spans


def _ts_declarations(text):
    """[(offset, name)] of class/function/const declarations in the file."""
    decls = []
    for match in _TS_DECL_RE.finditer(text):
        name = match.group(1) or match.group(2) or match.group(3)
        if name and name not in ("if", "for", "while", "switch", "catch"):
            decls.append((match.start(), name))
    return decls


def _ts_enclosing(decls, pos):
    """Nearest preceding declaration (approximate)."""
    idx = bisect.bisect_right([d[0] for d in decls], pos) - 1
    return decls[idx][1] if idx >= 0 else "<module>"


def scan_ts_file(path, rel, vocab):
    """Scan one TS/TSX file with regexes. Returns findings dicts."""
    out = {"occurrences": [], "closed_sets": [], "if_chains": [],
           "co_mappings": [], "parse_error": None}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - record and continue
        out["parse_error"] = {"file": rel, "error": "%s: %s"
                              % (type(exc).__name__, exc)}
        return out
    tokens, spans = _ts_tokens(text)
    decls = _ts_declarations(text)
    vocab_tokens = [t for t in tokens if t["content"] in vocab]
    for tok in vocab_tokens:
        stack = tok["stack"]
        top = stack[-1][0] if stack else ""
        if tok["next"].startswith(":") and top == "{":
            context = "dict-key"
        elif tok["prev"].endswith(":"):
            context = "dict-value"
        elif _CMP_BEFORE_RE.search(tok["prev"]) \
                or _CMP_AFTER_RE.match(tok["next"]):
            context = "compare"
        elif top == "[":
            context = "array member"
        elif top == "(":
            context = "call-arg"
        else:
            context = "other"
        out["occurrences"].append({
            "file": rel, "line": tok["line"],
            "enclosing": _ts_enclosing(decls, tok["offset"]),
            "context": context, "word": tok["content"]})
    # Closed sets: array spans holding 2+ distinct vocabulary words.
    by_array = {}
    by_object = {}
    for tok in vocab_tokens:
        innermost_bracket = None
        innermost_brace = None
        for ch, span_id, _ in reversed(tok["stack"]):
            if innermost_bracket is None and ch == "[":
                innermost_bracket = span_id
            if innermost_brace is None and ch == "{":
                innermost_brace = span_id
            if innermost_bracket is not None and innermost_brace is not None:
                break
        if innermost_bracket is not None:
            by_array.setdefault(innermost_bracket, set()).add(tok["content"])
        if innermost_brace is not None:
            by_object.setdefault(innermost_brace, []).append(tok)
    for span_id, words in by_array.items():
        if len(words) >= 2:
            out["closed_sets"].append({
                "file": rel, "line": spans[span_id][1], "kind": "array",
                "words": sorted(words)})
    # Co-mappings: one object span with a vocabulary key and a distinct
    # vocabulary value.
    for span_id, toks in by_object.items():
        keys = {t["content"] for t in toks
                if t["next"].startswith(":") and t["stack"]
                and t["stack"][-1][0] == "{"}
        values = {t["content"] for t in toks
                  if t["prev"].endswith(":")}
        for key in sorted(keys):
            for value in sorted(values):
                if value != key:
                    out["co_mappings"].append({
                        "a": key, "b": value,
                        "evidence": "code object %s:%d"
                        % (rel, spans[span_id][1])})
    # if/elif chains: a single line comparing one identifier to 2+ words.
    for match in re.finditer(
            r"\bif\s*\(([^)]*)\)", text):
        cond = match.group(1)
        lits = re.findall(r"""['"]([^'"]+)['"]""", cond)
        words = {w for w in lits if w in vocab}
        if len(words) >= 2:
            line = text.count("\n", 0, match.start()) + 1
            out["if_chains"].append({
                "file": rel, "line": line, "expression": cond.strip()[:120],
                "words": sorted(words)})
    return out


# ---------------------------------------------------------------------------
# Markers and engine names (textual scans).
# ---------------------------------------------------------------------------

def scan_markers(path, rel):
    """Line-based marker scan. Returns {marker: [hits]} for this file."""
    hits = {name: [] for name in ALL_MARKERS}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace") \
            .splitlines()
    except Exception:  # noqa: BLE001 - unreadable file: no hits
        return hits
    for lineno, line in enumerate(lines, 1):
        for name, pattern in MARKER_PATTERNS.items():
            if pattern.search(line):
                hits[name].append({"file": rel, "line": lineno,
                                   "text": line.strip()[:200]})
    return hits


_ENGINE_PATTERNS = {
    "llama": re.compile(r"(?<![A-Za-z])llama", re.IGNORECASE),
    "vllm": re.compile(r"\bvllm\b", re.IGNORECASE),
    "comfy": re.compile(r"comfy", re.IGNORECASE),
    "ollama": re.compile(r"ollama", re.IGNORECASE),
    "kobold": re.compile(r"kobold", re.IGNORECASE),
    "sglang": re.compile(r"sglang", re.IGNORECASE),
    "safetensors": re.compile(r"safetensors", re.IGNORECASE),
}


def scan_engine_names(path, rel):
    """Engine names found in a non-Python data file under gateway/."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - unreadable file: none found
        return {}
    found = {}
    for name, pattern in _ENGINE_PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            found[name] = count
    return found

# ---------------------------------------------------------------------------
# Tree collection, pairs, outputs.
# ---------------------------------------------------------------------------

def collect_code_files(tree):
    """(python_files, ts_files, gateway_data_files) as relative paths."""
    py_files, ts_files, data_files = [], [], []
    root_py = sorted(p for p in tree.glob("*.py") if p.is_file())
    py_files.extend(root_py)
    gw = tree / "gateway"
    if gw.is_dir():
        for path in sorted(gw.rglob("*")):
            if not path.is_file():
                continue
            if "tests" in path.relative_to(tree).parts:
                continue
            if path.suffix == ".py":
                py_files.append(path)
            else:
                data_files.append(path)
    web_src = tree / "web" / "src"
    if web_src.is_dir():
        for path in sorted(web_src.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(tree)
            if "node_modules" in rel.parts:
                continue
            if path.suffix in (".ts", ".tsx") and not any(
                    path.name.endswith(suf) for suf in TS_SKIP_SUFFIXES):
                ts_files.append(path)
    return py_files, ts_files, data_files


def split_tokens(word):
    return [t for t in re.split(r"[._\-/]+", word) if t]


def build_pairs(vocab, closed_sets, co_mappings):
    """Candidate equivalence pairs: {a, b} -> sorted evidence list."""
    pairs = {}
    words = sorted(vocab)

    def add(a, b, evidence):
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        pairs.setdefault(key, set()).add(evidence)

    tokens_of = {w: set(split_tokens(w)) for w in words}
    for i, a in enumerate(words):
        for b in words[i + 1:]:
            shared = tokens_of[a] & tokens_of[b]
            if shared:
                add(a, b, "shared token: %s" % ", ".join(sorted(shared)))
    for mapping in co_mappings:
        add(mapping["a"], mapping["b"],
            "co-mapped: %s" % mapping["evidence"])
    for closed in closed_sets:
        ws = closed["words"]
        for i, a in enumerate(ws):
            for b in ws[i + 1:]:
                add(a, b, "same closed set %s:%d (%s)"
                    % (closed["file"], closed["line"], closed["kind"]))
    return [{"a": a, "b": b, "evidence": sorted(ev)}
            for (a, b), ev in sorted(pairs.items())]


def run_inventory(tree):
    """Run every extractor over the tree. Returns the inventory dict."""
    tree = Path(tree)
    vocab, data_occ, docs, seed_errors = seed_vocabulary(tree)

    # Co-mappings in data (needs the finished vocabulary).
    data_pairs = {}
    for rel, doc in docs:
        _data_comapping_walk(doc, [], vocab, data_pairs)
    co_mappings = [{"a": a, "b": b, "evidence": sorted(ev)}
                   for (a, b), ev in sorted(data_pairs.items())]

    py_files, ts_files, data_files = collect_code_files(tree)
    code_occ = {}
    closed_sets, if_chains, match_stmts = [], [], []
    parse_errors = list(seed_errors)
    for path in py_files:
        rel = str(path.relative_to(tree))
        res = scan_python_file(path, rel, vocab)
        if res["parse_error"]:
            parse_errors.append(res["parse_error"])
        for occ in res["occurrences"]:
            code_occ.setdefault(occ["word"], []).append({
                "file": occ["file"], "line": occ["line"],
                "enclosing": occ["enclosing"], "context": occ["context"]})
        closed_sets.extend(res["closed_sets"])
        if_chains.extend(res["if_chains"])
        match_stmts.extend(res["match_statements"])
        co_mappings.extend(res["co_mappings"])
    for path in ts_files:
        rel = str(path.relative_to(tree))
        res = scan_ts_file(path, rel, vocab)
        if res["parse_error"]:
            parse_errors.append(res["parse_error"])
        for occ in res["occurrences"]:
            code_occ.setdefault(occ["word"], []).append({
                "file": occ["file"], "line": occ["line"],
                "enclosing": occ["enclosing"], "context": occ["context"]})
        closed_sets.extend(res["closed_sets"])
        if_chains.extend(res["if_chains"])
        co_mappings.extend(res["co_mappings"])

    # Markers in production Python and web/src.
    markers = {name: {"total": 0, "files": {}, "hits": []}
               for name in ALL_MARKERS}
    markers_by_file = {}
    for path in py_files + ts_files:
        rel = str(path.relative_to(tree))
        hits = scan_markers(path, rel)
        for name, file_hits in hits.items():
            if not file_hits:
                continue
            markers[name]["total"] += len(file_hits)
            markers[name]["files"][rel] = len(file_hits)
            markers[name]["hits"].extend(file_hits)
            markers_by_file.setdefault(rel, {})[name] = len(file_hits)
    for name in markers:
        markers[name]["hits"].sort(key=lambda h: (h["file"], h["line"]))

    # Engine names in non-Python data under gateway/.
    engine_names = {}
    for path in data_files:
        rel = str(path.relative_to(tree))
        found = scan_engine_names(path, rel)
        if found:
            engine_names[rel] = found

    # De-duplicate co-mappings (same pair, merged evidence).
    merged = {}
    for mapping in co_mappings:
        key = (mapping["a"], mapping["b"])
        if isinstance(mapping["evidence"], str):
            ev = {mapping["evidence"]}
        else:
            ev = set(mapping["evidence"])
        merged.setdefault(key, set()).update(ev)
    co_mappings = [{"a": a, "b": b, "evidence": sorted(ev)}
                   for (a, b), ev in sorted(merged.items())]

    pairs = build_pairs(vocab, closed_sets, co_mappings)
    vocabulary = [{
        "word": word,
        "data": sorted(data_occ.get(word, []),
                       key=lambda d: (d["file"], d["key_path"])),
        "code": sorted(code_occ.get(word, []),
                       key=lambda c: (c["file"], c["line"])),
    } for word in sorted(vocab)]
    counts = {
        "data_files": len(docs),
        "data_occurrences": sum(len(v) for v in data_occ.values()),
        "code_files_scanned": len(py_files) + len(ts_files),
        "code_occurrences": sum(len(v) for v in code_occ.values()),
        "closed_sets": len(closed_sets),
        "if_chains": len(if_chains),
        "match_statements": len(match_stmts),
        "co_mapping_pairs": len(co_mappings),
        "candidate_pairs": len(pairs),
        "marker_hits": sum(m["total"] for m in markers.values()),
    }
    inventory = {
        "tool": "op_inventory.py",
        "tree": str(tree.resolve()),
        "operation_key_set": {
            "value_keys": sorted(VALUE_KEYS),
            "contextual_keys": {
                "id": "recorded when the row also has an operation-like "
                      "key, or the parent list is capabilities / "
                      "alias_operations / rows",
                "name": "recorded only inside alias_operations rows",
            },
        },
        "vocabulary_size": len(vocab),
        "vocabulary": vocabulary,
        "closed_sets": sorted(closed_sets,
                              key=lambda c: (c["file"], c["line"], c["kind"])),
        "if_chains": sorted(if_chains,
                            key=lambda c: (c["file"], c["line"])),
        "match_statements": sorted(match_stmts,
                                   key=lambda c: (c["file"], c["line"])),
        "co_mappings": co_mappings,
        "markers": markers,
        "markers_by_file": markers_by_file,
        "engine_names": engine_names,
        "counts": counts,
        "parse_errors": parse_errors,
        "blind_spots": BLIND_SPOTS,
    }
    return inventory, pairs


# ---------------------------------------------------------------------------
# Summary notes (docs/reviews/...).
# ---------------------------------------------------------------------------

def _top_files(counter, limit):
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def render_notes(inv):
    """Summary markdown: (a) size, (b) word table, (c) sets/chains,
    (d) co-mappings, (e) markers per file, (f) literals per file,
    (g) blind spots."""
    lines = []
    lines.append("# Operation-vocabulary and implicit-coupling inventory")
    lines.append("")
    lines.append("Generated by `scripts/research/op_inventory.py` on the "
                 "repository root. Outputs: `inventory.json` (everything, "
                 "machine-readable) and `pairs.jsonl` (candidate equivalence "
                 "pairs). The JSON outputs stay out of the patch.")
    lines.append("")
    lines.append("## (a) Vocabulary size")
    lines.append("")
    lines.append("**%d distinct operation words** seeded from data "
                 "(%d data occurrences, %d code occurrences in %d files "
                 "scanned)." % (
                     inv["vocabulary_size"],
                     inv["counts"]["data_occurrences"],
                     inv["counts"]["code_occurrences"],
                     inv["counts"]["code_files_scanned"]))
    lines.append("")
    lines.append("## (b) Word -> occurrence counts (data / code)")
    lines.append("")
    lines.append("Sorted by word. Full table: every seeded word, even with "
                 "zero code occurrences.")
    lines.append("")
    lines.append("| word | data | code |")
    lines.append("| --- | ---: | ---: |")
    for entry in inv["vocabulary"]:
        lines.append("| `%s` | %d | %d |" % (
            entry["word"], len(entry["data"]), len(entry["code"])))
    lines.append("")
    lines.append("## (c) Closed sets, if/elif chains, match statements")
    lines.append("")
    lines.append("Closed sets: %d. If/elif chains: %d. Match statements: %d."
                 % (inv["counts"]["closed_sets"], inv["counts"]["if_chains"],
                    inv["counts"]["match_statements"]))
    lines.append("")
    lines.append("### Closed sets")
    lines.append("")
    for item in inv["closed_sets"]:
        lines.append("- `%s:%d` (%s): %s" % (
            item["file"], item["line"], item["kind"],
            ", ".join("`%s`" % w for w in item["words"])))
    lines.append("")
    lines.append("### If/elif chains and match statements")
    lines.append("")
    for item in inv["if_chains"]:
        lines.append("- `%s:%d` if-chain on `%s`: %s" % (
            item["file"], item["line"], item["expression"],
            ", ".join("`%s`" % w for w in item["words"])))
    for item in inv["match_statements"]:
        lines.append("- `%s:%d` match on `%s`: %s" % (
            item["file"], item["line"], item["subject"],
            ", ".join("`%s`" % w for w in item["words"])))
    lines.append("")
    lines.append("## (d) Co-mapping pairs")
    lines.append("")
    lines.append("%d candidate equivalence pairs from dicts/mappings where "
                 "one vocabulary word maps to another." %
                 inv["counts"]["co_mapping_pairs"])
    lines.append("")
    for item in inv["co_mappings"]:
        lines.append("- `%s` -> `%s` (%s)" % (
            item["a"], item["b"], "; ".join(item["evidence"])))
    lines.append("")
    lines.append("## (e) Marker counts per file (top 30)")
    lines.append("")
    per_file = {f: sum(m.values())
                for f, m in inv["markers_by_file"].items()}
    lines.append("| file | marker hits |")
    lines.append("| --- | ---: |")
    for path, total in _top_files(per_file, 30):
        lines.append("| `%s` | %d |" % (path, total))
    lines.append("")
    lines.append("Marker totals: %s." % ", ".join(
        "`%s`: %d" % (name, inv["markers"][name]["total"])
        for name in ALL_MARKERS if inv["markers"][name]["total"]))
    lines.append("")
    lines.append("## (f) Vocabulary literals per file (top 30)")
    lines.append("")
    lit_per_file = {}
    for entry in inv["vocabulary"]:
        for occ in entry["code"]:
            lit_per_file[occ["file"]] = lit_per_file.get(occ["file"], 0) + 1
    lines.append("| file | vocabulary literals |")
    lines.append("| --- | ---: |")
    for path, total in _top_files(lit_per_file, 30):
        lines.append("| `%s` | %d |" % (path, total))
    lines.append("")
    lines.append("## (g) What the tool cannot see")
    lines.append("")
    for spot in inv["blind_spots"]:
        lines.append("- %s" % spot)
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    lines.append("- Final operation-like key set: %s." %
                 ", ".join("`%s`" % k for k in sorted(VALUE_KEYS)))
    lines.append("- `id` is recorded only when the row also carries an "
                 "operation-like key, or the parent list is `capabilities`, "
                 "`alias_operations`, or `rows`. Provider ids "
                 "(providers/catalog), retention-fact ids, and other bare ids "
                 "are not vocabulary. The rule still captures a few "
                 "non-operation ids — frontend names (`openai`, `anthropic`, "
                 "`ollama`), protocol names (`openai-compatible`, "
                 "`chunked-stt`), and engine-definition metadata ids "
                 "(`model_list`, `openapi`) — because their rows carry "
                 "operation-like keys; they are visible in the table above, "
                 "not silently filtered.")
    lines.append("- Only `media_type_modalities` contributes vocabulary from "
                 "a mapping value (MIME type -> modality word). A dict value "
                 "at any other operation-like key (e.g. a JSON-schema object "
                 "under `capability`) contributes nothing itself; nested "
                 "operation keys inside it are still visited.")
    lines.append("- `name` is recorded only inside `alias_operations` rows.")
    lines.append("- `kind` is seeded as listed in the brief, but it is "
                 "noisy: it also yields non-operation values (e.g. `field`, "
                 "`json`, `template`, `none`). Those words stay in the "
                 "vocabulary table so the noise is visible, not silently "
                 "filtered.")
    lines.append("- Bare `protocol` values (e.g. `openai-chat`) are not "
                 "seeded: they name protocols, not operations. "
                 "`policy_protocol` and `receipt_protocols` are seeded.")
    lines.append("- Dotted workflow ids (e.g. "
                 "`audio.generate.ace-step-1.5-xl-turbo`) are seeded whole; "
                 "the pairs file connects them to their operation prefix via "
                 "shared tokens.")
    lines.append("- TypeScript is scanned with a character scanner (not a "
                 "parser): string literals, bracket spans, and `if (...)` "
                 "conditions. Enclosing function/class names there are "
                 "approximate.")
    lines.append("- Marker hits are textual: a line counts once per marker "
                 "even if the marker appears twice on it. Endpoint markers "
                 "match a full path segment, so `/view` does not match "
                 "`/overview`.")
    lines.append("- Engine names under `gateway/` are found by "
                 "case-insensitive text search of non-Python data files; "
                 "`llama` does not match inside `ollama`.")
    if inv["parse_errors"]:
        lines.append("")
        lines.append("## Parse errors")
        lines.append("")
        for err in inv["parse_errors"]:
            lines.append("- `%s`: %s" % (err["file"], err["error"]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test.
# ---------------------------------------------------------------------------

FIXTURE_YAML = """\
capabilities:
  - id: chat
    intent: chat.completions
    capability: chat
  - id: transcribe
    intent: speech.transcription
    capability: transcribe
alias_operations:
  - name: speech
    intent: speech.synthesis
    shared_operations: [translations]
word_map:
  chat: chat.completions
"""

FIXTURE_PY = """\
OPS = {"chat", "transcribe"}
ALIASES = {"speech": "speech.synthesis"}

# implicit coupling: class_type


def route(op):
    if op == "chat":
        return 1
    elif op == "transcribe":
        return 2
    return 0
"""

FIXTURE_TS = """\
export const OPS = ["chat", "transcribe"];
export function isChat(op: string): boolean {
  return op === "chat";
}
"""


def self_test():
    """Build a tiny fixture tree and assert each extractor finds exactly it."""
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append("%s:\n  got:  %r\n  want: %r" % (name, got, want))
        else:
            print("ok: %s" % name)

    with tempfile.TemporaryDirectory(prefix="op-inventory-test-") as tmp:
        tree = Path(tmp)
        (tree / "registry").mkdir()
        (tree / "registry" / "cap.yml").write_text(FIXTURE_YAML)
        (tree / "code.py").write_text(FIXTURE_PY)
        (tree / "web" / "src").mkdir(parents=True)
        (tree / "web" / "src" / "fixture.ts").write_text(FIXTURE_TS)
        inv, pairs = run_inventory(tree)

    check("vocabulary", {e["word"] for e in inv["vocabulary"]},
          {"chat", "chat.completions", "transcribe", "speech.transcription",
           "speech", "speech.synthesis", "translations"})
    check("data occurrences",
          {e["word"]: len(e["data"]) for e in inv["vocabulary"]},
          {"chat": 2, "chat.completions": 1, "transcribe": 2,
           "speech.transcription": 1, "speech": 1, "speech.synthesis": 1,
           "translations": 1})
    check("code occurrences",
          {e["word"]: len(e["code"]) for e in inv["vocabulary"]},
          {"chat": 4, "chat.completions": 0, "transcribe": 3,
           "speech.transcription": 0, "speech": 1, "speech.synthesis": 1,
           "translations": 0})
    check("closed sets",
          {(c["file"], c["kind"], tuple(c["words"]))
           for c in inv["closed_sets"]},
          {("code.py", "set", ("chat", "transcribe")),
           ("code.py", "dict", ("speech", "speech.synthesis")),
           ("web/src/fixture.ts", "array", ("chat", "transcribe"))})
    check("if chains",
          {(c["file"], c["expression"], tuple(c["words"]))
           for c in inv["if_chains"]},
          {("code.py", "op", ("chat", "transcribe"))})
    check("co-mappings",
          {(c["a"], c["b"]) for c in inv["co_mappings"]},
          {("speech", "speech.synthesis"), ("chat", "chat.completions")})
    check("candidate pairs",
          {(p["a"], p["b"]) for p in pairs},
          {("chat", "chat.completions"), ("chat", "transcribe"),
           ("speech", "speech.synthesis"), ("speech", "speech.transcription"),
           ("speech.synthesis", "speech.transcription")})
    # Note: ("speech.transcription", "transcribe") is NOT a pair: after
    # splitting, "transcription" != "transcribe", so they share no token.
    check("marker hits",
          [(h["file"], h["line"]) for h in
           inv["markers"]["class_type"]["hits"]],
          [("code.py", 4)])
    check("parse errors", inv["parse_errors"], [])
    if failures:
        print("\nSELF-TEST FAILED:\n" + "\n".join(failures))
        return 1
    print("self-test: all checks passed")
    return 0


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) < 2:
        print(__doc__.strip().split("\n\n")[1])
        print("usage: op_inventory.py <tree> <outdir> [--notes <path>]")
        return 2
    tree, outdir = Path(argv[0]), Path(argv[1])
    notes_path = None
    if "--notes" in argv:
        idx = argv.index("--notes")
        notes_path = Path(argv[idx + 1])
    inv, pairs = run_inventory(tree)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "inventory.json").write_text(
        json.dumps(inv, indent=2, sort_keys=True) + "\n")
    with open(outdir / "pairs.jsonl", "w") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, sort_keys=True) + "\n")
    if notes_path is not None:
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(render_notes(inv))
    print("vocabulary: %d words" % inv["vocabulary_size"])
    print("data occurrences: %d" % inv["counts"]["data_occurrences"])
    print("code occurrences: %d in %d files"
          % (inv["counts"]["code_occurrences"],
             inv["counts"]["code_files_scanned"]))
    print("closed sets: %d, if chains: %d, match statements: %d"
          % (inv["counts"]["closed_sets"], inv["counts"]["if_chains"],
             inv["counts"]["match_statements"]))
    print("co-mapping pairs: %d, candidate pairs: %d"
          % (inv["counts"]["co_mapping_pairs"],
             inv["counts"]["candidate_pairs"]))
    print("marker hits: %d" % inv["counts"]["marker_hits"])
    if inv["parse_errors"]:
        print("parse errors: %d" % len(inv["parse_errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
