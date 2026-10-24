"""Keep the TypeScript capability labels aligned with the gateway table."""

import ast
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def gateway_capability_labels() -> dict[str, str]:
    module = ast.parse((ROOT / "gateway/capability_ids.py").read_text())
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "CAPABILITY_LABELS" for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise AssertionError("gateway CAPABILITY_LABELS assignment was not found")


def typescript_capability_labels() -> dict[str, str]:
    source = (ROOT / "web/src/lib/terms.ts").read_text()
    match = re.search(r"export const capabilityLabels = \{(.*?)\} as const;", source, re.S)
    assert match, "TypeScript capabilityLabels object was not found"

    labels = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        entry = re.fullmatch(r'\s*([a-z_]+):\s*("(?:[^"\\]|\\.)*"),?\s*', line)
        assert entry, f"Unrecognized capability label entry: {line.strip()}"
        labels[entry.group(1)] = json.loads(entry.group(2))
    return labels


def test_typescript_capability_labels_match_gateway():
    assert typescript_capability_labels() == gateway_capability_labels()


def test_gateway_capability_labels_use_the_voice_glossary_terms():
    labels = gateway_capability_labels()
    assert labels["speech"] == "Voice"
    assert labels["audio"] == "Audio"
    assert labels["decide"] == "Decisions"
    assert labels["embed"] == "Embeddings"
    assert labels["rerank"] == "Reranking"
