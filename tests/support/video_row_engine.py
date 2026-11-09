"""The engine a video create needs under test, now that a plain create is a run-core job (builder V5e).

The run core finds its engine in a service record that names its protocol, and a create holds for the engine to take the job.
A test that drives a create against its own fake engine (``tests/_fake_comfy_video.FakeComfy``, which patches the HTTP client)
declares the engine here, so the core reaches it, and cuts the hold to a moment. Nothing here is a hook of the old handler."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Any, Callable
from unittest.mock import patch

import proxy

ENGINE_URL = "http://engine.test:1"


def declare_graph_engine(registry: dict[str, Any]) -> dict[str, Any]:
    """Make the graph engine's record in ``registry`` one the run core finds. Returns the record's config."""
    config = registry["services"][proxy._GRAPH_BACKEND_ID]["config"]
    config["connection"] = {"protocol": "node-graph", "base_url": config["base_url"]}
    config["enabled"] = True
    return config


def use_graph_engine(stack: ExitStack) -> dict[str, Any]:
    """Serve a registry with only the graph engine, declared for the run core. Returns the registry."""
    registry = {"schema_version": 2, "services": {
        proxy._GRAPH_BACKEND_ID: {"id": proxy._GRAPH_BACKEND_ID, "config": {"base_url": ENGINE_URL}}}}
    declare_graph_engine(registry)
    stack.enter_context(patch.object(proxy.ADMIN_STORE, "get_service_registry", lambda: registry))
    return registry


@contextmanager
def edited_workflows(edit: Callable[[Any], Any]):
    """The workflow snapshot served with ``edit(entry)`` in place of each entry, and the run core rebuilt over it. This is how
    a test gives a workflow another contract or binding: the core pins what the snapshot serves, as it does in production."""
    live = proxy.WORKFLOW_SNAPSHOTS

    class Edited:
        def current(self):
            snap = live.current()

            class Snap:
                def get(self, workflow_id):
                    entry = snap.get(workflow_id)
                    return None if entry is None else edit(entry)

                def __getattr__(self, name):
                    return getattr(snap, name)
            return Snap()

        def __getattr__(self, name):
            return getattr(live, name)

    with patch.object(proxy, "WORKFLOW_SNAPSHOTS", Edited()), patch.object(proxy, "_workflow_core_cache", None):
        yield
