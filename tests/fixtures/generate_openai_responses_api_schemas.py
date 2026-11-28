"""Regenerate openai-openapi-responses-api.json from the pinned OpenAI spec.

    uv run python tests/fixtures/generate_openai_responses_api_schemas.py

Keeps the closure of the ``Response`` and ``ResponseStreamEvent`` schemas from
openai/openai-openapi at the commit below. Prose keys (description, title,
example) are dropped to keep the file small; schema meaning is unchanged.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

COMMIT = "145fd3b2e30e3ac87a4ed915913f6fa1217974fa"
URL = f"https://raw.githubusercontent.com/openai/openai-openapi/{COMMIT}/openapi.json"
ROOTS = ["Response", "ResponseStreamEvent"]
PROSE = {"description", "title", "example", "examples"}


def refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                found.add(value.rsplit("/", 1)[1])
            else:
                refs(value, found)
    elif isinstance(node, list):
        for item in node:
            refs(item, found)


def strip(node):
    if isinstance(node, dict):
        return {
            k: strip(v)
            for k, v in node.items()
            if not (k in PROSE and not isinstance(v, (dict, list)))
        }
    if isinstance(node, list):
        return [strip(v) for v in node]
    return node


def main() -> None:
    spec = json.load(urllib.request.urlopen(URL))
    schemas = spec["components"]["schemas"]
    seen: set[str] = set()
    todo = list(ROOTS)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        found: set[str] = set()
        refs(schemas[name], found)
        todo.extend(found - seen)
    out = {
        "commit": COMMIT,
        "source": "https://github.com/openai/openai-openapi",
        "retrieved_from": URL,
        "roots": ROOTS,
        "description": "Closure of the Response and ResponseStreamEvent schemas. Prose keys dropped.",
        "components": {"schemas": {n: strip(schemas[n]) for n in sorted(seen)}},
    }
    path = Path(__file__).with_name("openai-openapi-responses-api.json")
    path.write_text(json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
