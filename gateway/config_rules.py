"""Which reviewed rule judges which config section, and the one place that builds the gateway's ``ConfigHome``.

A section needs no code to be stored, exported or checked for structure. A section with a rule across its entries
registers it here by id, so ``config_layers`` and ``config_runtime`` stay free of what any section holds.
"""
from __future__ import annotations

from typing import Mapping

from gateway import config_layers as layers
from gateway import config_runtime, stable_aliases

RULES: Mapping[str, layers.SemanticValidator] = {
    "model_aliases": stable_aliases.alias_problems,
}


def config_home() -> config_runtime.ConfigHome:
    """The release's sections, each with its rule, as the gateway and the dry-run command load them."""
    return config_runtime.ConfigHome(config_runtime.load_sections(validators=RULES))
