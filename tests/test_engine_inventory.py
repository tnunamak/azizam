"""Offline unit tests for the engine inventory behind availability listings."""

import asyncio
import json
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

import httpx

from gateway import engine_inventory as inventory


BODY = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["tiny.safetensors", "spare.safetensors"], {}]}},
    },
}


class Catalog:
    def ids(self):
        return ("other", "engine")

    def definition(self, key):
        if key == "other":
            return {"record": {"protocol": "no-node-list"}, "probes": {
                "lists": [{"offers": "model", "path": "/models"}],
            }}
        return {"record": {"protocol": "fixture"}, "probes": {
            "lists": [
                {"offers": "node", "method": "POST", "path": "/ignored"},
                {"offers": "node", "path": "/object_info", "cap_bytes": 512},
            ],
        }}


class EngineInventoryTest(unittest.TestCase):
    def setUp(self):
        inventory.reset()
        self.addCleanup(inventory.reset)

    def test_from_node_list(self):
        """Extract node and model names and refuse a body with no node list."""
        read = inventory.from_node_list(BODY, fetched_at=10, url="https://engine.invalid/object_info")
        self.assertIsNotNone(read)
        self.assertEqual(read.node_classes, frozenset({"CheckpointLoaderSimple"}))
        self.assertEqual(set(read.model_names), {"tiny.safetensors", "spare.safetensors"})
        self.assertEqual(read.fetched_at, 10)
        self.assertEqual(read.url, "https://engine.invalid/object_info")
        self.assertIsNone(inventory.from_node_list(None, fetched_at=10))

    def test_source_of(self):
        """Use the configured address prefix and probe cap; reject unusable inputs."""
        probe = {"path": "/object_info"}
        expected = inventory.Source("https://engine.invalid/object_info", inventory.DEFAULT_CAP_BYTES)
        self.assertEqual(inventory.source_of("https://engine.invalid", probe), expected)
        self.assertEqual(inventory.source_of("https://engine.invalid/", probe), expected)
        self.assertEqual(
            inventory.source_of("https://engine.invalid/prefix/", {"path": "/object_info", "cap_bytes": 512}),
            inventory.Source("https://engine.invalid/prefix/object_info", 512),
        )
        self.assertIsNone(inventory.source_of("https://engine.invalid", {"path": "object_info"}))
        self.assertIsNone(inventory.source_of("", probe))

    def test_refresh_failure_keeps_same_source(self):
        """Keep the last successful list on transport or invalid-body failures."""
        source = inventory.Source("https://engine.invalid/object_info")
        sources = {"engine": source}
        fetched = []

        def fetch(src):
            fetched.append(src)
            return BODY

        def fail(_):
            raise TimeoutError("fixture")

        self.assertEqual(inventory.refresh_all(sources, fetch=fetch, now=10), {"engine": None})
        self.assertEqual(fetched, [source])
        prior = inventory.fresh("engine", now=10)
        self.assertIsNotNone(prior)
        self.assertEqual(inventory.refresh_all(sources, fetch=fail, now=11), {"engine": "TimeoutError"})
        self.assertIs(inventory.fresh("engine", now=11), prior)
        self.assertEqual(
            inventory.refresh_all(sources, fetch=lambda _: None, now=12),
            {"engine": "ValueError"},
        )
        self.assertIs(inventory.fresh("engine", now=12), prior)

    def test_transport_and_body_failures_keep_original_snapshot_until_expiry(self):
        source = inventory.Source("https://engine.invalid/object_info")
        cases = (
            ("timeout", 200, (), httpx.ReadTimeout("synthetic"), "ReadTimeout"),
            ("not-found", 404, (), None, "ValueError"),
            ("non-json", 200, (b"<html>not JSON</html>",), None, "JSONDecodeError"),
            ("wrong-shape", 200, (json.dumps(["not a node list"]).encode(),), None, "ValueError"),
        )
        for label, status, chunks, error, expected in cases:
            with self.subTest(label=label):
                inventory.reset()
                self.assertEqual(inventory.refresh_all({"engine": source}, fetch=lambda _: BODY, now=10),
                                 {"engine": None})
                prior = inventory.fresh("engine", now=10)

                class FakeClient:
                    @contextmanager
                    def stream(self, method, url, **_kwargs):
                        self_test.assertEqual((method, url), ("GET", source.url))

                        class Response:
                            status_code = status

                            def iter_bytes(self):
                                if error is not None:
                                    raise error
                                yield from chunks

                        yield Response()

                self_test = self
                with patch("gateway.engine_egress.client", return_value=nullcontext(FakeClient())) as client:
                    result = inventory.refresh_all({"engine": source}, now=11)
                    self.assertEqual(result, {"engine": expected})
                    client.assert_called_once()
                    self.assertIs(inventory.fresh("engine", now=11), prior)
                    self.assertEqual(prior.fetched_at, 10)
                    self.assertIsNone(inventory.fresh("engine", now=10 + inventory.MAX_AGE_S))
                    client.assert_called_once()  # memory-only status reads do not fetch

    def test_a_parser_valid_smaller_node_list_is_a_new_snapshot_not_a_partial_state(self):
        source = inventory.Source("https://engine.invalid/object_info")
        smaller = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["tiny.safetensors"], {}]}}}}
        self.assertEqual(inventory.refresh_all({"engine": source}, fetch=lambda _: BODY, now=10),
                         {"engine": None})
        self.assertEqual(inventory.refresh_all({"engine": source}, fetch=lambda _: smaller, now=11),
                         {"engine": None})
        fresh = inventory.fresh("engine", now=11)
        self.assertEqual(fresh.fetched_at, 11)
        self.assertEqual(fresh.model_names, ("tiny.safetensors",))

    def test_removed_and_moved_sources_are_forgotten(self):
        """Remove stale source identities before attempting a changed URL."""
        source = inventory.Source("https://engine.invalid/object_info")
        moved = inventory.Source("https://other.invalid/object_info")
        old = {"removed": source, "moved": source}
        self.assertEqual(inventory.refresh_all(old, fetch=lambda _: BODY, now=10),
                         {"removed": None, "moved": None})
        self.assertEqual(set(inventory.fresh_all(now=10)), {"removed", "moved"})
        observed = []

        def read_changed(src):
            observed.append((src, inventory.fresh_all(now=11)))
            raise ConnectionError("fixture")

        self.assertEqual(inventory.refresh_all({"moved": moved}, fetch=read_changed, now=11),
                         {"moved": "ConnectionError"})
        self.assertEqual(observed, [(moved, {})])
        self.assertEqual(inventory.fresh_all(now=11), {})

    def test_fresh_and_fresh_all(self):
        """Read fresh lists from memory and exclude old or future-dated observations."""
        recent = inventory.NodeInventory(frozenset({"Recent"}), (), 10)
        old = inventory.NodeInventory(frozenset({"Old"}), (), 0)
        future = inventory.NodeInventory(frozenset({"Future"}), (), inventory.MAX_AGE_S + 2)
        for name, value in (("recent", recent), ("old", old), ("future", future)):
            inventory.store(name, value)
        now = inventory.MAX_AGE_S + 1
        self.assertIs(inventory.fresh("recent", now=now), recent)
        self.assertIsNone(inventory.fresh("old", now=now))
        self.assertIsNone(inventory.fresh("missing", now=now))
        self.assertIsNone(inventory.fresh("recent", now=10 + inventory.MAX_AGE_S))
        self.assertEqual(inventory.fresh_all(now=now), {"recent": recent})

    def test_sources_for(self):
        """Keep the first usable service and skip blank fields and unsupported probes."""
        engines = [
            ("fixture", "fixture", "https://first.invalid/prefix/"),
            ("fixture", "fixture", "https://second.invalid"),
            ("", "fixture", "https://blank.invalid"),
            ("blank-protocol", "", "https://blank.invalid"),
            ("blank-address", "fixture", ""),
            ("unsupported", "no-node-list", "https://other.invalid"),
        ]
        self.assertEqual(
            inventory.sources_for(engines, catalog=Catalog()),
            {"fixture": inventory.Source("https://first.invalid/prefix/object_info", 512)},
        )

    def test_run_loop_already_stopped(self):
        """Do not compute sources or fetch after an already-set stop event."""
        async def exercise():
            stop = asyncio.Event()
            stop.set()
            calls = []

            def unexpected(*args):
                calls.append(args)
                return {}

            await asyncio.wait_for(inventory.run_loop(stop, unexpected, fetch=unexpected), timeout=0.75)
            self.assertEqual(calls, [])

        asyncio.run(exercise())

    def test_run_loop_reads_once_and_stops(self):
        """Start immediately with an injected fetch, then stop without a second read."""
        async def exercise():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            source = inventory.Source("https://engine.invalid/object_info")
            fetched = []

            def fetch(src):
                fetched.append(src)
                loop.call_soon_threadsafe(stop.set)
                return BODY

            await asyncio.wait_for(
                inventory.run_loop(stop, lambda: {"engine": source}, fetch=fetch,
                                   first_after_s=0, every_s=0.1),
                timeout=0.75,
            )
            self.assertEqual(fetched, [source])
            self.assertIsNotNone(inventory.fresh("engine"))

        asyncio.run(exercise())
