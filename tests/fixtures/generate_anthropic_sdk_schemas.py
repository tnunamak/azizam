"""Regenerate anthropic-sdk-schemas.json from the official anthropic SDK.

Run with the SDK installed, for example:

    uv run --with anthropic==1.11.0 python tests/fixtures/generate_anthropic_sdk_schemas.py

Anthropic does not publish a stable OpenAPI URL. Its Python SDK is generated
from the same spec, so its response models are the pinned source of truth.
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from pydantic import TypeAdapter

from anthropic.types import (
    Message,
    MessageTokensCount,
    ModelInfo,
    RawMessageStreamEvent,
)
from anthropic.types.shared import ErrorResponse

TYPES = {
    "Message": Message,
    "MessageTokensCount": MessageTokensCount,
    "ModelInfo": ModelInfo,
    "RawMessageStreamEvent": RawMessageStreamEvent,
    "ErrorResponse": ErrorResponse,
}


def main() -> None:
    schemas = {
        name: TypeAdapter(tp).json_schema(
            mode="validation", ref_template="#/$defs/{model}"
        )
        for name, tp in TYPES.items()
    }
    document = {
        "source": "anthropic (PyPI) python SDK response models",
        "package": "anthropic",
        "version": version("anthropic"),
        "repository": "https://github.com/anthropics/anthropic-sdk-python",
        "note": (
            "Generated with pydantic TypeAdapter(...).json_schema() from "
            "anthropic.types. Do not edit by hand."
        ),
        "schemas": schemas,
    }
    out = Path(__file__).with_name("anthropic-sdk-schemas.json")
    out.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
