"""Publish a config the way a reload does, for a test that swaps ``proxy.config`` and reads ``proxy.runtime()``.

``runtime()`` answers the published snapshot only, so ``monkeypatch.setattr(proxy, "config", cfg)`` alone is not seen by
code that reads the snapshot (the decision runtime, the mesh frontend records). The autouse fixture in
``tests/conftest.py`` puts the holder back after the test.
"""
from __future__ import annotations

from typing import Any, Mapping
from unittest import mock

import pytest

import proxy
from tests.support import graph_service


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
    """``publish_config`` of the current config with one top-level ``section`` replaced by ``value``.

    A section other than ``backends`` does not change what the registry derives, so the running snapshot's derived values
    (a test may have patched one, ``derived_patch``) stay in the new snapshot. ``backends`` derives them afresh.
    """
    config = {**proxy.config, section: value}
    monkeypatch.setattr(proxy, "config", config)
    carried = dict(proxy.runtime().derived) if section != "backends" else None
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(config, derived=carried))


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


class derived_patch:
    """Patch one value of the published snapshot's ``derived`` mapping (``proxy.DERIVED.<name>``), for a ``with`` block or
    ``start``/``stop``.

    A test that patched a module constant a reader used to read (``patch.object(proxy, "COMFY_ENABLED", True)``) patches the
    snapshot the reader now takes it from. The snapshot is the one published when ``start`` runs, so a list of patchers that
    starts a ``section_published`` first patches that snapshot. A test that publishes another snapshot afterwards (a reload,
    ``publish_config``) must patch after that.
    """

    _ABSENT = object()

    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self.value = value
        self._patch: Any = None
        self._patched: Any = None
        self._original: dict[str, Any] = {}
        self._set: dict[str, Any] = {}

    def _values(self, derived: Mapping[str, Any]) -> dict[str, Any]:
        """What the patch sets: the value, and for one of the engine's (``GRAPH_MIRROR``) the family views that carry it."""
        values = {self.name: self.value}
        if self.name in graph_service.GRAPH_MIRROR:
            values["GRAPH_SERVICES"] = graph_service.mirror(derived["GRAPH_SERVICES"], self.name, self.value)
        return values

    def start(self) -> None:
        self._patched = proxy.runtime().derived
        self._original = {name: self._patched.get(name, self._ABSENT) for name in self._values(self._patched)}
        self._set = self._values(self._patched)
        self._patch = mock.patch.dict(self._patched, self._set)
        self._patch.start()

    def stop(self) -> None:
        if self._patch is None:
            return
        self._patch.stop()
        self._patch = None
        # publish_section carries the running derived values into the snapshot it publishes, so a snapshot published
        # inside this block holds the patched value too. Put the value back there as well.
        current = proxy.runtime().derived
        if current is not self._patched:
            for name, value in self._set.items():
                if current.get(name, self._ABSENT) == value:
                    if self._original[name] is self._ABSENT:
                        current.pop(name, None)
                    else:
                        current[name] = self._original[name]
        self._patched = None

    def __enter__(self) -> "derived_patch":
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
