"""The gate's default port range is not another tool's.

scripts/ci-local gives each fixture gateway ten ports from BASE (default 18640)
up to BASE + 89. A tool that documents or defaults to a port inside that range
collides with a gate run on the same host (the perf harness once did, at 18710,
18720, 18730 and 18740). Tools that start fixtures pick ports outside it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = (ROOT / "scripts/ci-local").read_text()
BASE = int(re.search(r"^signoff=0 .*\bport=(\d+)", GATE, re.M).group(1))
SPAN = int(re.search(r"\(\(port \+ (\d+) > 18998\)\)", GATE).group(1))
# Tools that start gateways of their own beside the gate.
TOOL_FILES = sorted((ROOT / "scripts/perf").glob("*.*"))


def test_the_gate_range_is_read_from_ci_local():
    assert (BASE, SPAN) == (18640, 89)


def test_no_tool_documents_or_defaults_to_a_port_in_the_gate_range():
    clashes = []
    for path in TOOL_FILES:
        if path.suffix not in {".py", ".md", ".sh", ""}:
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for text in re.findall(r"\b18\d{3}\b", line):
                if BASE <= int(text) <= BASE + SPAN:
                    clashes.append(f"{path.relative_to(ROOT)}:{number}: {text}")
    assert clashes == [], "ports inside the gate's range " + f"{BASE}-{BASE + SPAN}"
