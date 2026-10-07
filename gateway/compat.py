"""Environment reads with empty values treated as unset."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def get_env(name: str, default: Any = None, *, environ: Mapping[str, str] | None = None) -> Any:
    """Read the configured value; empty strings are unset."""
    env = os.environ if environ is None else environ
    return env.get(name) or default
