"""Publish a config the way a reload does, for a test that swaps ``proxy.config`` and reads ``proxy.runtime()``.

``runtime()`` answers the published snapshot only, so ``monkeypatch.setattr(proxy, "config", cfg)`` alone is not seen by
code that reads the snapshot (the decision runtime, the mesh frontend records). The autouse fixture in
``tests/conftest.py`` puts the holder back after the test.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import pytest

import proxy


def publish_config(monkeypatch: pytest.MonkeyPatch, config: Mapping[str, Any]) -> None:
    """Set ``proxy.config`` to ``config`` and publish a snapshot of it."""
    monkeypatch.setattr(proxy, "config", config)
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(config))


def pin_inner_hop_hosts(monkeypatch: pytest.MonkeyPatch, hosts: Mapping[str, str], *, only: bool = False) -> None:
    """Make the LiteLLM lifecycle hop proxy to ``hosts`` (``{backend id: "http://host:port"}``), beside the hosts the
    published config derives unless ``only`` is set. For a test that needs an address no config entry carries."""
    derived = proxy.inner_hop_backend_hosts
    monkeypatch.setattr(proxy, "inner_hop_backend_hosts", lambda: {**({} if only else derived()), **hosts})


def publish_section(monkeypatch: pytest.MonkeyPatch, section: str, value: Any) -> None:
    """``publish_config`` of the current config with one top-level ``section`` replaced by ``value``."""
    publish_config(monkeypatch, {**proxy.config, section: value})


@contextmanager
def section_published(section: str, value: Any) -> Iterator[None]:
    """Publish the current config with one top-level ``section`` replaced, for the length of a ``with`` block.

    For a test that patched a module global a reader used to read (``patch.object(proxy, "ROLES", ...)``): the reader now
    reads ``proxy.runtime()``, which answers the published snapshot. The previous config and snapshot are restored on exit.
    """
    saved_config = proxy.config
    saved = proxy.CONFIG_HOME.holder.current()
    proxy.config = {**saved_config, section: value}
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(proxy.config))
    try:
        yield
    finally:
        proxy.config = saved_config
        proxy.CONFIG_HOME.holder.publish(saved)
