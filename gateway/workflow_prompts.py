"""The generation prompts the gateway sends or injects itself, read from ``registry/workflow_prompts.yml``.

A prompt is reviewed text, not code: each one is declared once there with a ``version`` and the named ``slots`` its text
fills. ``render`` returns the text with the slots filled by ``str.format_map``; a slot value is inserted as written (it is
never read as a template). The file is checked against ``registry/workflow_prompts.schema.json``, and each text must use
exactly the slots it declares, so a typo fails at import, not on the first request that needs the prompt.
"""

from __future__ import annotations

import json
import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "workflow_prompts.yml"
SCHEMA_PATH = _REGISTRY_DIR / "workflow_prompts.schema.json"


@dataclass(frozen=True)
class Prompt:
    version: int
    slots: tuple[str, ...]
    text: str


def load_prompts(path: Path = BUNDLED_PATH) -> dict[str, Prompt]:
    """Read and validate the prompt file; raise ``ValueError`` naming the file and the first problem."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).validate(document)
        prompts: dict[str, Prompt] = {}
        for name, row in document["prompts"].items():
            used = {field for _, field, _, _ in string.Formatter().parse(row["text"]) if field is not None}
            if used != set(row["slots"]):
                raise ValueError(f"prompts.{name}: the text uses slots {sorted(used)} but declares {sorted(row['slots'])}")
            prompts[name] = Prompt(version=row["version"], slots=tuple(row["slots"]), text=row["text"])
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError, jsonschema.ValidationError) as error:
        raise ValueError(f"{path}: invalid workflow prompts: {error}") from error
    return prompts


_PROMPTS = load_prompts()


def render(name: str, **slots: Any) -> str:
    """The text of the prompt ``name`` with its slots filled; the slots given must be exactly the ones it declares."""
    prompt = _PROMPTS[name]
    if set(slots) != set(prompt.slots):
        raise ValueError(f"workflow prompt {name!r} takes slots {sorted(prompt.slots)}, got {sorted(slots)}")
    return prompt.text.format_map(slots)


def version(name: str) -> int:
    return _PROMPTS[name].version
