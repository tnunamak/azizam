"""Closed, versioned compatibility declarations; no recurring migration policy."""
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

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


Operation = Annotated[str, StringConstraints(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]


class AliasRelation(Contract):
    """One workflow intent an alias section may name, and the prefix older alias targets carry for it."""
    intent: Word
    target_prefix: Word


class CompatibilityContract(Contract):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    migration: MigrationSelector
    legacy_alias_tables: dict[Word, Word]
    limit_key_aliases: dict[Word, Word]
    legacy_audio_result_keys: Annotated[tuple[Word, ...], Field(min_length=1)]
    alias_operation_intents: dict[Operation, Annotated[tuple[AliasRelation, ...], Field(min_length=1)]]

    @field_validator("alias_operation_intents", mode="before")
    @classmethod
    def owned_relations(cls, value):
        if not isinstance(value, dict):
            return value
        return {key: tuple(rows) if isinstance(rows, list) else rows for key, rows in value.items()}

    @model_validator(mode="after")
    def consistent_alias_relations(self):
        prefixes: dict[str, str] = {}
        for operation, rows in self.alias_operation_intents.items():
            if len({row.intent for row in rows}) != len(rows):
                raise ValueError(f"alias_operation_intents.{operation}: an intent appears twice")
            for row in rows:
                if prefixes.setdefault(row.intent, row.target_prefix) != row.target_prefix:
                    raise ValueError(f"alias_operation_intents: intent {row.intent} has two target prefixes")
        return self

    @field_validator("legacy_audio_result_keys", mode="before")
    @classmethod
    def owned_key_order(cls, value):
        # YAML gives a list; keep an immutable copy so the order cannot change after validation.
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def unique_legacy_result_keys(self):
        keys = self.legacy_audio_result_keys
        if len(set(keys)) != len(keys) or any(any(char.isspace() for char in key) for key in keys):
            raise ValueError("legacy result keys must be unique non-whitespace words")
        return self


def load_compatibility(path: Path = REGISTRY / "compatibility.yml") -> CompatibilityContract:
    return CompatibilityContract.model_validate(safe_load_strict(path.read_text(encoding="utf-8")))


COMPATIBILITY = load_compatibility()
