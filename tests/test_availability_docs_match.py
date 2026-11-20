"""Keep the API's client-action table aligned with the gateway's closed code set."""

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_CELL = re.compile(r"`([a-z][a-z0-9_]*)`")


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
        code, _state, action, _retry = (cell.strip() for cell in row[1:-1].split("|"))
        match = CODE_CELL.fullmatch(code)
        assert match is not None, f"Malformed availability reason: {code}"
        assert action.endswith(".") and len(action) > 1, f"Missing client action for {code}"
        actual[match.group(1)] += 1

    assert actual == expected
