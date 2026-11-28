"""Ratchet: implicit engine coupling under neutral names may only fall.

tests/test_engine_knowledge_boundary.py counts engine NAMES in production Python and that count is 0. Engine
coupling also hides under neutral names: graph-engine node fields (class_type, widgets_values, SaveImage), graph
server paths (/prompt, /history, /view), a vendor GPU tool (nvidia-smi, GPU0), llama-style probe paths and model
file formats (/props, model_path, gguf, mmproj), chat template switches (chat_template_kwargs, enable_thinking) and
the LLM library's response fields (x-litellm, response_cost). See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md and
the code to data phase 2 audit.

This test counts those markers per production file (proxy.py, root modules, gateway/**; not tests, not web) and
compares with tests/implicit_marker_baseline.json. Non-adapter per-file rises fail.
Only files listed in baseline adapter_files may rise, and only if that marker's
total across all files does not rise. Offsetting falls in such a containment
transfer are accepted together; ordinary falls require lowering the baseline.
Baseline file keys must equal files with current markers, and no baseline entry
may be empty. Missing, orphan and empty entries fail with the file name:

    python tests/test_implicit_marker_ratchet.py --lower-baseline

The tool refuses non-adapter increases and adapter increases that raise a marker
total. --lower-baseline records permitted transfers as well as reductions, drops
empty/orphan entries and preserves adapter_files. Containment is explicit data,
not a hard-coded module exception.

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


def _totals(counts: dict[str, dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for markers in counts.values():
        for marker, count in markers.items():
            totals[marker] = totals.get(marker, 0) + count
    return totals


def _allowed_rises(
    counts: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
    adapter_files: list[str] | tuple[str, ...],
) -> set[tuple[str, str]]:
    now_totals, base_totals = _totals(counts), _totals(baseline)
    return {
        (rel, marker)
        for rel in adapter_files
        for marker, n in counts.get(rel, {}).items()
        if n > baseline.get(rel, {}).get(marker, 0)
        and now_totals.get(marker, 0) <= base_totals.get(marker, 0)
    }


def ratchet_violations(
    counts: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
    adapter_files: list[str] | tuple[str, ...] = (),
) -> list[str]:
    counts = {rel: markers for rel, markers in counts.items() if markers}
    allowed = _allowed_rises(counts, baseline, adapter_files)
    transferred = {marker for _, marker in allowed}
    violations: list[str] = []
    now_totals, base_totals = _totals(counts), _totals(baseline)
    for marker in sorted(transferred):
        n, b = now_totals.get(marker, 0), base_totals.get(marker, 0)
        if n < b:
            violations.append(f"total: {marker!r} fell {b} -> {n}; {_LOWER}")
    for rel in sorted(set(counts) | set(baseline)):
        now, base = counts.get(rel, {}), baseline.get(rel)
        if base is None:
            violations.append(f"{rel}: new file with implicit markers {now}")
            continue
        for marker in sorted(set(now) | set(base)):
            n, b = now.get(marker, 0), base.get(marker, 0)
            if n > b and (rel, marker) not in allowed:
                violations.append(f"{rel}: {marker!r} rose {b} -> {n}; move it into an adapter or record data")
            elif n < b and marker not in transferred:
                violations.append(f"{rel}: {marker!r} fell {b} -> {n}; {_LOWER}")
        if not base:
            violations.append(f"{rel}: empty baseline entry; {_LOWER}")
        if rel not in counts:
            violations.append(f"{rel}: orphan baseline entry has no current markers; {_LOWER}")
    return violations


def lower_baseline(
    counts: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
    adapter_files: list[str] | tuple[str, ...] = (),
) -> dict:
    counts = {rel: markers for rel, markers in counts.items() if markers}
    allowed = _allowed_rises(counts, baseline, adapter_files)
    raised = [
        f"{rel} {m}" for rel, ms in counts.items() for m, n in ms.items()
        if n > baseline.get(rel, {}).get(m, 0) and (rel, m) not in allowed
    ]
    if raised:
        raise SystemExit("refusing to raise the baseline for: " + ", ".join(raised))
    return {"adapter_files": list(adapter_files), "files": counts}


def _load_baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _write(doc: dict) -> None:
    _BASELINE_PATH.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_implicit_markers_only_fall() -> None:
    doc = _load_baseline()
    violations = ratchet_violations(current_counts(), doc["files"], doc.get("adapter_files", []))
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
    assert lower_baseline({"a.py": {"gguf": 1}}, base) == {"adapter_files": [], "files": {"a.py": {"gguf": 1}}}
    try:
        lower_baseline({"a.py": {"gguf": 3}}, base)
    except SystemExit as exc:
        assert "refusing to raise" in str(exc)
    else:
        raise AssertionError("a raise was accepted")



def test_adapter_rise_with_equal_fall_passes() -> None:
    base = {"adapter.py": {"class_type": 3}, "core.py": {"class_type": 7}}
    now = {"adapter.py": {"class_type": 8}, "core.py": {"class_type": 2}}
    assert ratchet_violations(now, base, ["adapter.py"]) == []


def test_adapter_transfer_with_net_fall_requires_lowering() -> None:
    base = {"adapter_file": {"class_type": 3}, "core_file": {"class_type": 7}}
    now = {"adapter_file": {"class_type": 8}, "core_file": {"class_type": 1}}
    adapters = ["adapter_file"]
    assert ratchet_violations(now, base, adapters) == [
        f"total: 'class_type' fell 10 -> 9; {_LOWER}"
    ]

    lowered = lower_baseline(now, base, adapters)
    assert lowered == {"adapter_files": adapters, "files": now}
    assert ratchet_violations(now, lowered["files"], lowered["adapter_files"]) == []

    restored = {"adapter_file": {"class_type": 8}, "core_file": {"class_type": 2}}
    assert ratchet_violations(restored, lowered["files"], lowered["adapter_files"]) == [
        "core_file: 'class_type' rose 1 -> 2; move it into an adapter or record data"
    ]


def test_adapter_rise_without_fall_fails() -> None:
    base = {"adapter.py": {"class_type": 3}, "core.py": {"class_type": 7}}
    now = {"adapter.py": {"class_type": 8}, "core.py": {"class_type": 7}}
    assert any("adapter.py" in v and "rose" in v for v in ratchet_violations(now, base, ["adapter.py"]))


def test_non_adapter_rise_with_fall_fails() -> None:
    base = {"adapter.py": {"class_type": 7}, "core.py": {"class_type": 3}}
    now = {"adapter.py": {"class_type": 2}, "core.py": {"class_type": 8}}
    assert any("core.py" in v and "rose" in v for v in ratchet_violations(now, base, ["adapter.py"]))


def test_orphan_empty_baseline_entry_fails() -> None:
    violations = ratchet_violations({}, {"orphan.py": {}})
    assert any("orphan.py" in v and "empty" in v for v in violations)
    assert any("orphan.py" in v and "orphan" in v for v in violations)


def test_lower_tool_records_containment_and_preserves_adapter_data() -> None:
    base = {"adapter.py": {"class_type": 3}, "core.py": {"class_type": 7}, "orphan.py": {}}
    now = {"adapter.py": {"class_type": 8}, "core.py": {"class_type": 2}}
    assert lower_baseline(now, base, ["adapter.py"]) == {"adapter_files": ["adapter.py"], "files": now}
    smaller = {"adapter.py": {"class_type": 8}, "core.py": {"class_type": 1}}
    assert lower_baseline(smaller, base, ["adapter.py"])["files"] == smaller


def test_lower_tool_refuses_adapter_total_rise_and_core_transfer() -> None:
    base = {"adapter.py": {"class_type": 3}, "core.py": {"class_type": 7}}
    for now in [
        {"adapter.py": {"class_type": 8}, "core.py": {"class_type": 3}},
        {"adapter.py": {"class_type": 2}, "core.py": {"class_type": 8}},
        {"adapter.py": {"class_type": 2, "model_path": 1}, "core.py": {"class_type": 7}},
    ]:
        try:
            lower_baseline(now, base, ["adapter.py"])
        except SystemExit as exc:
            assert "refusing to raise" in str(exc)
        else:
            raise AssertionError("a forbidden rise was accepted")


def test_empty_current_rows_do_not_count_as_marker_files() -> None:
    assert any("orphan.py" in v for v in ratchet_violations({"orphan.py": {}}, {"orphan.py": {}}))
    assert lower_baseline({"orphan.py": {}}, {"orphan.py": {}})["files"] == {}

if __name__ == "__main__":
    if sys.argv[1:] == ["--lower-baseline"]:
        doc = _load_baseline()
        _write(lower_baseline(current_counts(), doc["files"], doc.get("adapter_files", [])))
    elif sys.argv[1:] == ["--write-initial-baseline"] and not _BASELINE_PATH.exists():
        _write({"adapter_files": [], "files": current_counts()})
    else:
        raise SystemExit("usage: --lower-baseline")
    doc = _load_baseline()["files"]
    print(f"baseline: {sum(sum(m.values()) for m in doc.values())} hits in {len(doc)} files")
