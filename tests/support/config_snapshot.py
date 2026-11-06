"""Publish a config the way a reload does, for a test that swaps ``proxy.config`` and reads ``proxy.runtime()``.

``runtime()`` answers the published snapshot only, so ``monkeypatch.setattr(proxy, "config", cfg)`` alone is not seen by
code that reads the snapshot (the decision runtime, the mesh frontend records). The autouse fixture in
``tests/conftest.py`` puts the holder back after the test.
"""
from __future__ import annotations

from typing import Any, Mapping

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


class section_published:
    """Publish the current config with one top-level ``section`` replaced, until ``stop`` (or the end of a ``with`` block).

    For a test that patched a module global a reader used to read (``patch.object(proxy, "ROLES", ...)``): the reader now
    reads ``proxy.runtime()``, which answers the published snapshot. ``start``/``stop`` suit a list of patchers that the
    test starts itself; ``with`` suits a block. The previous config and snapshot are restored on stop.
    """

    def __init__(self, section: str, value: Any) -> None:
        self.section = section
        self.value = value
        self._saved: tuple[Any, Any] | None = None

    def start(self) -> None:
        self._saved = (proxy.config, proxy.CONFIG_HOME.holder.current())
        proxy.config = {**proxy.config, self.section: self.value}
        proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(proxy.config))

    def stop(self) -> None:
        if self._saved is not None:
            proxy.config, previous = self._saved
            proxy.CONFIG_HOME.holder.publish(previous)
            self._saved = None

    def __enter__(self) -> "section_published":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


class section_key_published(section_published):
    """``section_published`` of the current section with one key replaced, for a test that patched one module global
    of a section (``patch.object(proxy, "VIDEO_MAX_TOTAL_SECONDS", 120.0)``): the other keys stay as they are."""

    def __init__(self, section: str, key: str, value: Any) -> None:
        current = proxy.config.get(section)
        super().__init__(section, {**(current if isinstance(current, Mapping) else {}), key: value})
