"""Keep public gateway availability reasons actionable in the web client."""

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gateway_availability_reasons_have_web_handlers() -> None:
    source = ROOT / "gateway" / "availability_reasons.py"
    declarations = [
        node.value
        for node in ast.parse(source.read_text(encoding="utf-8")).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "REASONS"
    ]
    assert len(declarations) == 1 and isinstance(declarations[0], ast.Dict), (
        "Expected one closed REASONS mapping in gateway/availability_reasons.py"
    )
    reasons = ast.literal_eval(declarations[0])
    assert reasons and all(isinstance(code, str) for code in reasons)

    web = ROOT / "web" / "src" / "lib"
    files = [
        (web / name).read_text(encoding="utf-8")
        for name in ("workflow-availability.ts", "status-copy.ts")
    ]
    missing = []
    for reason in sorted(reasons):
        key = re.escape(reason)
        literal = rf"""(?:'{key}'|"{key}")"""
        # Match a keyed handler or a literal branch, not a type-union entry or prose.
        handler = re.compile(
            rf"^\s*(?:{key}|{literal})\s*:"
            rf"|^\s*case\s+{literal}\s*:"
            rf"|===\s*{literal}"
            rf"|{literal}\s*===",
            re.MULTILINE,
        )
        if not any(handler.search(text) for text in files):
            missing.append(reason)

    assert not missing, "Gateway availability reasons missing a web handler: " + ", ".join(missing)
