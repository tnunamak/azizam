"""SDK compatibility harness shared utilities.

Pure-Python, stdlib-only. Heavy SDK dependencies live in optional probe
modules under ``tests/compat/python/`` and ``tests/compat/node/`` and are
imported lazily so the main test suite never requires them.
"""

from .config import HarnessConfig, load_config, ConfigError
from .matrix import (
    ProbeResult,
    ProtocolMatrix,
    Result,
    build_matrix,
    matrix_to_json,
)

__all__ = [
    "ConfigError",
    "HarnessConfig",
    "ProbeResult",
    "ProtocolMatrix",
    "Result",
    "build_matrix",
    "load_config",
    "matrix_to_json",
]
