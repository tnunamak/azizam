"""Bundled starter categories, read from ``manifests/categories/*.yml``.

A preset is only a shortcut for filling in the Add category form: an id, a
label, a one-line meaning and who may see it. Adding one writes the same rows
the page writes for a custom category, so the owner can edit or delete it
afterwards. The gateway never reads a preset at request time and knows no
category id or label; this module only lists and validates the files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRESETS_DIR = Path(__file__).resolve().parents[1] / "manifests" / "categories"
# The one value of a plain category: "this item is in it". The page and the
# seed script write the same value, so both read it from here.
MEMBER_VALUE = "included"
VISIBILITIES = ("owner", "everyone")
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FIELDS = {"schema_version", "id", "label", "description", "visibility"}


class PresetError(ValueError):
    """A bundled preset file is malformed. The message names the file."""


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    visibility: str  # "owner": only the owner; "everyone": no restriction.

    def as_json(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "description": self.description, "visibility": self.visibility}

    def category_row(self) -> dict[str, Any]:
        """The category row the Categories page writes for this preset."""
        return {"id": self.id, "label": self.label, "kind": "set", "values": [MEMBER_VALUE],
                "applies_to": ["release", "workflow"], "description": self.description,
                "visible_to": [] if self.visibility == "owner" else None}


def _text(source: Path, data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PresetError(f"{source.name}: {key} must be non-empty text without edge spaces")
    return value


def _read(source: Path) -> Preset:
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PresetError(f"{source.name}: cannot read ({exc})") from exc
    if not isinstance(data, dict) or set(data) - _FIELDS or data.get("schema_version") != 1:
        raise PresetError(f"{source.name}: expected schema_version 1 and only {sorted(_FIELDS)}")
    preset = Preset(_text(source, data, "id"), _text(source, data, "label"),
                    _text(source, data, "description"), _text(source, data, "visibility"))
    if not _ID.match(preset.id) or source.stem != preset.id:
        raise PresetError(f"{source.name}: id must be lowercase words joined by hyphens and match the file name")
    if preset.visibility not in VISIBILITIES:
        raise PresetError(f"{source.name}: visibility must be one of {list(VISIBILITIES)}")
    return preset


def load_presets(directory: Path = PRESETS_DIR) -> list[Preset]:
    """Every bundled preset, by id. A malformed file raises ``PresetError``."""
    return [_read(path) for path in sorted(Path(directory).glob("*.yml"))]
