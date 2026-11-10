"""Closed, versioned compatibility declarations; no recurring migration policy."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gateway.strict_yaml import safe_load_strict

REGISTRY = Path(__file__).resolve().parent.parent / "registry"
Word = Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, pattern=r"^\S+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MigrationSelector(Contract):
    include_capabilities: Annotated[list[Word], Field(min_length=1)]
    exclude_capabilities: Annotated[list[Word], Field(min_length=1)]
    config_mapping: Word

    @model_validator(mode="after")
    def unique_selectors(self):
        for values in (self.include_capabilities, self.exclude_capabilities):
            if len(set(values)) != len(values) or any(value != value.lower() for value in values):
                raise ValueError("selectors must be unique lowercase words")
        if set(self.include_capabilities) & set(self.exclude_capabilities):
            raise ValueError("include and exclude selectors must be disjoint")
        return self


class CompatibilityContract(Contract):
    schema_version: Literal[1]
    migration: MigrationSelector
    legacy_alias_tables: dict[Word, Word]
    limit_key_aliases: dict[Word, Word]


def load_compatibility(path: Path = REGISTRY / "compatibility.yml") -> CompatibilityContract:
    return CompatibilityContract.model_validate(safe_load_strict(path.read_text(encoding="utf-8")))


COMPATIBILITY = load_compatibility()
