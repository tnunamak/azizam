"""Deterministically project capability words into inert catalog data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


PROJECTED = (
    ("protocol_words", "protocol"),
    ("route_intents", "route"),
    ("type_capabilities", "type"),
)
UNEXPRESSED = (
    ("schema_version", "Source format version, not an operation fact."),
    ("modality_words", "Presentation vocabulary for modalities."),
    ("media_type_modalities", "Media-type presentation classification."),
    ("capabilities", "Display labels, explainers, order, aliases and policy hints."),
    ("audio_rule", "Source-specific classification rule, not an operation fact."),
)


def generate(source: Path) -> str:
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    expected = {name for name, _ in PROJECTED} | {name for name, _ in UNEXPRESSED}
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("Every source top-level key must be classified")
    relations = []
    for name, context in PROJECTED:
        words = document[name]
        if not isinstance(words, dict):
            raise ValueError("Projected source tables must be mappings")
        for order, (word, family) in enumerate(words.items()):
            if not isinstance(word, str) or not isinstance(family, str) or not family.strip():
                raise ValueError("Projected words and families must be text")
            relations.append({
                "kind": "family", "word": word, "family": family,
                "context": context, "order": order,
            })
    catalog = {
        "schema_version": 1,
        "operations": [],
        "relations": relations,
        "unexpressed": [
            {"source": source.name, "key": name, "reason": reason}
            for name, reason in UNEXPRESSED
        ],
    }
    # JSON is a YAML subset, with stable key order and explicit string quoting.
    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "registry" / "capability_words.yml")
    parser.add_argument("--output", type=Path, default=root / "registry" / "operations.yml")
    args = parser.parse_args()
    args.output.write_bytes(generate(args.source).encode("utf-8"))


if __name__ == "__main__":
    main()
