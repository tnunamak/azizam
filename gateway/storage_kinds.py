"""Internal storage accounting vocabulary shared by schema and projection."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gateway.strict_yaml import safe_load_strict

STORAGE_KINDS_PATH = Path(__file__).resolve().parent.parent / "registry" / "storage_kinds.yml"


class StorageKinds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    kinds: Annotated[list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_kinds(self):
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("storage kinds must be unique")
        return self


def load_storage_kinds(path: Path = STORAGE_KINDS_PATH) -> tuple[str, ...]:
    document = safe_load_strict(path.read_text(encoding="utf-8"))
    return tuple(StorageKinds.model_validate(document).kinds)
