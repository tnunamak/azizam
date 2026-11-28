"""Keep the API's client-action table aligned with the gateway's closed code set."""

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_CELL = re.compile(r"`([a-z][a-z0-9_]*)`")
# What the table must say per code: the states a caller can see with it, and whether it gives `retry_after_s`.
STATES = {
    "backend_unavailable": ("unavailable", "unknown"),
    "engine_transitioning": ("unknown",),
    "model_not_deployed": ("unavailable",),
    "current_model_unavailable": ("unavailable",),
    "service_disabled": ("unavailable", "unknown"),
    "service_maintenance": ("unavailable", "unknown"),
    "health_unknown": ("unknown",),
    "required_file_missing": ("unavailable",),
}
RETRY_AFTER = {"service_maintenance"}


def test_availability_reason_table_matches_gateway() -> None:
    module = ast.parse((ROOT / "gateway/availability_reasons.py").read_text(encoding="utf-8"))
    mapping = next(
        node.value
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "REASONS"
    )
    expected = Counter(ast.literal_eval(mapping).keys())

    doc = (ROOT / "docs/api/MODEL_METADATA.md").read_text(encoding="utf-8")
    section = doc.split("\n### Reason codes\n", 1)[1].split("\n### ", 1)[0]
    table = re.search(
        r"^\| Code \| State \| Client action \| `retry_after_s` \|\n"
        r"\|[-| ]+\|\n((?:\|[^\n]+\n)+)",
        section,
        re.MULTILINE,
    )
    assert table is not None, "Reason codes must have a Client action table"
    actual: Counter[str] = Counter()
    for row in table.group(1).splitlines():
        code, state, action, retry = (cell.strip() for cell in row[1:-1].split("|"))
        match = CODE_CELL.fullmatch(code)
        assert match is not None, f"Malformed availability reason: {code}"
        name = match.group(1)
        assert name in STATES, f"No expected states for {name}: add it to STATES and RETRY_AFTER"
        assert re.findall(r"`([a-z]+)`", state) == list(STATES[name]), f"State cell of {name}: {state}"
        assert retry.startswith("yes") == (name in RETRY_AFTER), f"retry_after_s cell of {name}: {retry}"
        assert action.endswith(".") and len(action) > 1, f"Missing client action for {code}"
        actual[match.group(1)] += 1

    assert actual == expected


def test_alternative_availability_reason_precedence_and_workflow_field() -> None:
    doc = (ROOT / "docs/api/MODEL_METADATA.md").read_text(encoding="utf-8")
    section = doc.split("\n### Reason codes\n", 1)[1].split("\n### ", 1)[0]
    normalized = " ".join(section.split())
    assert "A starting alternative takes precedence and gives `engine_transitioning`" in normalized
    assert "first alternative in configured order" in normalized
    assert "the reason can depend on order" in normalized
    assert "reversing them gives `health_unknown`" in normalized
    for code in ("backend_unavailable", "service_disabled", "service_maintenance"):
        assert f"| `{code}` | `unavailable`, `unknown` |" in section
    assert "workflow availability row carries a `reason_code`" in normalized
    assert "`reason_code` is not a member of `/v1/models` availability" in normalized
