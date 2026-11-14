"""Ratchet: implicit engine coupling under neutral names may only fall.

tests/test_engine_knowledge_boundary.py counts engine NAMES in production Python and that count is 0. Engine
coupling also hides under neutral names: graph-engine node fields (class_type, widgets_values, SaveImage), graph
server paths (/prompt, /history, /view), a vendor GPU tool (nvidia-smi, GPU0), llama-style probe paths and model
file formats (/props, model_path, gguf, mmproj), chat template switches (chat_template_kwargs, enable_thinking) and
the LLM library's response fields (x-litellm, response_cost). See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md and
the code to data phase 2 audit.

This test counts those markers per production file (proxy.py, root modules, gateway/**; not tests, not web) and
compares with tests/implicit_marker_baseline.json. A rise or a new file fails. A fall fails until the baseline is
lowered, so a removal cannot be given back:

    python tests/test_implicit_marker_ratchet.py --lower-baseline

The tool refuses to raise a count. The fix for a rise is to move the knowledge into an adapter or into
service-record data, never to raise the baseline.

A hit is one marker on one physical line, outside comments and docstrings. A line with two different markers
counts twice; the same marker twice on a line counts once.
"""
from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = Path(__file__).parent / "implicit_marker_baseline.json"
_LOWER = "lower the baseline (python tests/test_implicit_marker_ratchet.py --lower-baseline)"

_PLAIN = [
    "class_type", "extra_pnginfo", "SaveImage", "LoadImage", "widgets_values", "model_path", "gguf", "mmproj",
    "response_cost", "chat_template_kwargs", "enable_thinking", "GPU0", "GPU1", "activation_", "x-litellm",
    "nvidia-smi", "<think>",
]
_PATHS = ["/history", "/prompt", "/view", "/object_info", "/upload/image", "/system_stats", "/props"]
MARKERS: dict[str, re.Pattern[str]] = {
    **{m: re.compile(re.escape(m)) for m in _PLAIN},
    # A full path segment: "/view" must not match "/overview" or "/viewer".
    **{m: re.compile(re.escape(m) + r"""(?=["'/?\s]|$)""") for m in _PATHS},
}


def production_files() -> list[Path]:
    """proxy.py, root modules and gateway/**. Unlike the engine-name ratchet, no test fakes."""
    return [*sorted(_REPO_ROOT.glob("*.py")), *sorted((_REPO_ROOT / "gateway").rglob("*.py"))]


def _code_lines(source: str) -> list[str]:
    """Source lines with comments and docstrings blanked; code after a docstring on the same line stays."""
    lines = source.splitlines()
    # (start line, start col, end line, end col, cols are UTF-8 bytes?) tokenize counts characters, ast counts bytes.
    spans: list[tuple[int, int, int, int, bool]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            spans.append((*tok.start, *tok.end, False))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc = body[0].value
                spans.append((doc.lineno, doc.col_offset, doc.end_lineno or doc.lineno, doc.end_col_offset or 0, True))
    for start_line, start_col, end_line, end_col, in_bytes in spans:
        for n in range(start_line, end_line + 1):
            text = lines[n - 1]
            unit = text.encode("utf-8") if in_bytes else text
            a = start_col if n == start_line else 0
            b = end_col if n == end_line else len(unit)
            blanked = unit[:a] + (b" " if in_bytes else " ") * (b - a) + unit[b:]
            lines[n - 1] = blanked.decode("utf-8", errors="replace") if in_bytes else blanked
    return lines


def count_markers(source: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in _code_lines(source):
        for name, pattern in MARKERS.items():
            if pattern.search(line):
                counts[name] = counts.get(name, 0) + 1
    return counts


def current_counts() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for path in production_files():
        counts = count_markers(path.read_text(encoding="utf-8"))
        if counts:
            out[str(path.relative_to(_REPO_ROOT))] = dict(sorted(counts.items()))
    return out


def ratchet_violations(counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> list[str]:
    violations: list[str] = []
    for rel in sorted(set(counts) | set(baseline)):
        now, base = counts.get(rel, {}), baseline.get(rel)
        if base is None:
            violations.append(f"{rel}: new file with implicit markers {now}")
            continue
        for marker in sorted(set(now) | set(base)):
            n, b = now.get(marker, 0), base.get(marker, 0)
            if n > b:
                violations.append(f"{rel}: {marker!r} rose {b} -> {n}; move it into an adapter or record data")
            elif n < b:
                violations.append(f"{rel}: {marker!r} fell {b} -> {n}; {_LOWER}")
    return violations


def lower_baseline(counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> dict:
    raised = [f"{rel} {m}" for rel, ms in counts.items() for m, n in ms.items() if n > baseline.get(rel, {}).get(m, 0)]
    if raised:
        raise SystemExit("refusing to raise the baseline for: " + ", ".join(raised))
    return {"files": counts}


def _load_baseline() -> dict[str, dict[str, int]]:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))["files"]


def _write(doc: dict) -> None:
    _BASELINE_PATH.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_implicit_markers_only_fall() -> None:
    violations = ratchet_violations(current_counts(), _load_baseline())
    assert not violations, "Implicit engine markers changed:\n" + "\n".join(violations)


def test_counts_code_but_not_comments_or_docstrings() -> None:
    source = (
        '"""Module doc names class_type and /prompt."""\n'
        "def f():\n"
        '    """Function doc: SaveImage."""\n'
        '    node = {"class_type": "x"}  # comment: nvidia-smi\n'
        '    return node["class_type"], "/prompt"\n'
        "x = 1  # GPU0\n"
    )
    assert count_markers(source) == {"class_type": 2, "/prompt": 1}


def test_path_markers_match_whole_segments_only() -> None:
    source = 'a = "/view"\nb = "/overview"\nc = "/viewer"\nd = "/view?x=1"\ne = "/props/"\nf = "/proposal"\n'
    assert count_markers(source) == {"/view": 2, "/props": 1}


def test_one_hit_per_marker_per_line() -> None:
    assert count_markers('x = ("gguf", "gguf", "mmproj")\n') == {"gguf": 1, "mmproj": 1}


def test_code_after_a_docstring_on_the_same_line_counts() -> None:
    assert count_markers('def f(): """doc"""; return "enable_thinking"\n') == {"enable_thinking": 1}


def test_rise_new_file_and_fall_fail() -> None:
    base = {"a.py": {"gguf": 2}}
    assert ratchet_violations({"a.py": {"gguf": 2}}, base) == []
    assert "rose 2 -> 3" in ratchet_violations({"a.py": {"gguf": 3}}, base)[0]
    assert "fell 2 -> 1" in ratchet_violations({"a.py": {"gguf": 1}}, base)[0]
    assert "fell 2 -> 0" in ratchet_violations({}, base)[0]
    assert ratchet_violations({"a.py": {"gguf": 2}, "b.py": {"GPU0": 1}}, base) == ["b.py: new file with implicit markers {'GPU0': 1}"]


def test_lower_tool_refuses_a_raise() -> None:
    base = {"a.py": {"gguf": 2}}
    assert lower_baseline({"a.py": {"gguf": 1}}, base) == {"files": {"a.py": {"gguf": 1}}}
    try:
        lower_baseline({"a.py": {"gguf": 3}}, base)
    except SystemExit as exc:
        assert "refusing to raise" in str(exc)
    else:
        raise AssertionError("a raise was accepted")


if __name__ == "__main__":
    if sys.argv[1:] == ["--lower-baseline"]:
        _write(lower_baseline(current_counts(), _load_baseline()))
    elif sys.argv[1:] == ["--write-initial-baseline"] and not _BASELINE_PATH.exists():
        _write({"files": current_counts()})
    else:
        raise SystemExit("usage: --lower-baseline")
    doc = _load_baseline()
    print(f"baseline: {sum(sum(m.values()) for m in doc.values())} hits in {len(doc)} files")
