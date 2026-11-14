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


MEDIA_GAP_REASON = "Operation request/result mapping is not supplied by this table."


def media_intent_gaps(source: Path) -> list[dict[str, str]]:
    """Conservatively count unmapped operations, never infer contracts from routes."""
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"intents"}:
        raise ValueError("Every media source top-level key must be classified")
    rows = document["intents"]
    if not isinstance(rows, list):
        raise ValueError("Media intents must be a list")
    required = {"intent", "modality", "submittable"}
    optional = {"route", "component", "execution_route"}
    seen = set()
    gaps = []
    for order, row in enumerate(rows):
        if not isinstance(row, dict) or not required <= set(row) or set(row) - required - optional:
            raise ValueError("Every media intent field must be classified")
        if type(row["submittable"]) is not bool:
            raise ValueError("Media submittability must be an explicit boolean")
        if any(not isinstance(value, str) or not value.strip()
               for key, value in row.items() if key != "submittable"):
            raise ValueError("Media intent fields must be nonempty text")
        if row["intent"] in seen:
            raise ValueError("Duplicate media intent")
        seen.add(row["intent"])
        gaps.append({
            "source": source.name,
            "key": f"intents[{order}]:{row['intent']}",
            "reason": MEDIA_GAP_REASON,
        })
    return gaps


LOCAL_GAP_REASON = "Local routing/default rows lack a closed operation request/result mapping."
LOCAL_LISTS = ("alias_operations", "workflow_families", "backend_rows", "image_rows")
LOCAL_DEFAULTS = ("image_defaults", "workflow_defaults")


def local_capability_gaps(source: Path) -> list[dict[str, str]]:
    """Conservatively enumerate gaps without copying payloads or inventing relations."""
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema_version", *LOCAL_LISTS, *LOCAL_DEFAULTS}:
        raise ValueError("Every local source top-level key must be classified")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported local source version")
    gaps = [{"source": source.name, "key": "schema_version",
             "reason": "Source format version, not an operation fact."}]
    for name in LOCAL_LISTS:
        rows = document[name]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Local rows must be lists of mappings")
        # Original positions remain explicit, even though payloads are unexpressed.
        gaps.extend({"source": source.name, "key": f"{name}[{order}]",
                     "reason": LOCAL_GAP_REASON} for order, _row in enumerate(rows))
    for name in LOCAL_DEFAULTS:
        if not isinstance(document[name], dict):
            raise ValueError("Local defaults must be mappings")
        gaps.append({"source": source.name, "key": name, "reason": LOCAL_GAP_REASON})
    return gaps



MODELS_GAP_REASON = "Examples and prefixes lack defined operation targets; legacy case/underscore spellings cannot target undefined operations."


def models_catalog_gaps(source: Path) -> list[dict[str, str]]:
    """Assume examples are not contracts; count rows instead of fabricating targets."""
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema_version", "row_id_placeholder", "operations"}:
        raise ValueError("Every models source top-level key must be classified")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported models source version")
    if not isinstance(document["row_id_placeholder"], str) or not document["row_id_placeholder"]:
        raise ValueError("Models placeholder must be nonempty text")
    rows = document["operations"]
    if not isinstance(rows, list):
        raise ValueError("Models operations must be a list")
    gaps = [
        {"source": source.name, "key": "schema_version", "reason": "Source format version, not an operation fact."},
        {"source": source.name, "key": "row_id_placeholder", "reason": "Example substitution marker, not an operation fact."},
    ]
    seen = set()
    for order, row in enumerate(rows):
        if not isinstance(row, dict) or not {"id", "words", "intent_prefixes"} <= set(row) or set(row) - {"id", "words", "intent_prefixes", "examples"}:
            raise ValueError("Every models operation field must be classified")
        identifier = row["id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("Models operation IDs must be unique nonempty text")
        seen.add(identifier)
        for key in ("words", "intent_prefixes"):
            if not isinstance(row[key], list) or any(not isinstance(word, str) or not word for word in row[key]):
                raise ValueError("Models words and prefixes must be text lists")
        if "examples" in row and (not isinstance(row["examples"], list) or any(not isinstance(example, dict) for example in row["examples"])):
            raise ValueError("Models examples must be mappings")
        gaps.append({"source": source.name, "key": f"operations[{order}]:{identifier}", "reason": MODELS_GAP_REASON})
    return gaps



FRONTEND_GAP_REASON = "Admission, runtime policy and receipt bindings need defined operation targets and closed relation field contracts; they are not spelling equivalences."


def protocol_frontend_gaps(source: Path) -> list[dict[str, str]]:
    """Count unresolved operation bindings without rewriting frontend behavior."""
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"records", "frontends", "input_contract"}:
        raise ValueError("Every frontend source section must be classified")
    gaps = []
    for section, route_key in (("records", "registered_routes"), ("frontends", "routes")):
        rows = document[section]
        if not isinstance(rows, list):
            raise ValueError("Frontend sections must be lists")
        seen = set()
        for order, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                raise ValueError("Frontend IDs must be unique nonempty text")
            seen.add(row["id"])
            routes = row.get(route_key, [])
            if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
                raise ValueError("Frontend operation routes must be mappings")
            # Position records source precedence without copying executable policy payloads.
            gaps.append({"source": source.name, "key": f"{section}[{order}]:{row['id']}", "reason": FRONTEND_GAP_REASON})
    return gaps


def generate(source: Path, media_source: Path | None = None, local_source: Path | None = None, models_source: Path | None = None, frontends_source: Path | None = None) -> str:
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
    if media_source is not None:
        catalog["unexpressed"].extend(media_intent_gaps(media_source))
    if local_source is not None:
        catalog["unexpressed"].extend(local_capability_gaps(local_source))
    if models_source is not None:
        catalog["unexpressed"].extend(models_catalog_gaps(models_source))
    if frontends_source is not None:
        catalog["unexpressed"].extend(protocol_frontend_gaps(frontends_source))
    # JSON is a YAML subset, with stable key order and explicit string quoting.
    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "registry" / "capability_words.yml")
    parser.add_argument("--output", type=Path, default=root / "registry" / "operations.yml")
    parser.add_argument("--media-source", type=Path, default=root / "registry" / "media_job_intents.yml")
    parser.add_argument("--local-source", type=Path, default=root / "registry" / "local_capability_rows.yml")
    parser.add_argument("--models-source", type=Path, default=root / "registry" / "models_catalog_operations.yml")
    parser.add_argument("--frontends-source", type=Path, default=root / "registry" / "protocol_frontends.yml")
    args = parser.parse_args()
    args.output.write_bytes(generate(args.source, args.media_source, args.local_source, args.models_source, args.frontends_source).encode("utf-8"))


if __name__ == "__main__":
    main()
