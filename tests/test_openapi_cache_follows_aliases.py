"""The cached /openapi.json follows the alias tables: an alias edit while the gateway runs shows up without a restart."""
from __future__ import annotations

from unittest.mock import patch

import proxy
from tests.test_workflow_openapi import ROOT_EXTENSION


def _listed_aliases() -> set[str]:
    return {alias for workflow in proxy.app.openapi()[ROOT_EXTENSION] for alias in workflow["aliases"]}


def test_the_schema_is_rebuilt_when_the_alias_tables_change() -> None:
    real = proxy._gateway_aliases_provider
    try:
        proxy.app.openapi_schema = None
        with patch.object(proxy, "_gateway_aliases_provider", lambda operation=None: {}):
            assert _listed_aliases() == set(), "with no alias tables no workflow lists an alias"
        listed = _listed_aliases()
        assert "sfx" in listed and "image" in listed, "the schema built under other tables is not served again"
        first = proxy.app.openapi()
        assert proxy.app.openapi() is first, "with the same inputs the cached schema is served"
    finally:
        assert proxy._gateway_aliases_provider is real
        proxy.app.openapi_schema = None
