"""Small deterministic guards for the authenticated P1B fixture seam."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "tests" / "compat" / "python" / "macro_ux_fixture.py"
HARNESS_PATH = ROOT / "scripts" / "fresh_instance_harness.sh"
SPEC = importlib.util.spec_from_file_location("macro_ux_fixture", HELPER_PATH)
fixture = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(fixture)


class TestMacroUxFixture(unittest.TestCase):
    def test_scenarios_project_exact_service_facts(self) -> None:
        for scenario, (state, tone, available, services, _attention) in fixture.SCENARIO_EXPECTATIONS.items():
            if scenario == "status_unavailable":
                with self.assertRaises(RuntimeError):
                    asyncio.run(fixture._status(scenario)())
                continue
            observed = asyncio.run(fixture._status(scenario)())
            self.assertTrue(available)
            self.assertIn(state, {"ok", "warn", "bad"})
            self.assertIn(tone, {"ok", "warn", "bad"})
            by_id = {row["key"]: row["status"] for row in observed["backends"]}
            for service_id, (observation, _service_tone) in services.items():
                if observation == "disabled":
                    continue
                expected = "down" if observation == "standby_stopped" else observation
                self.assertEqual(by_id[service_id], expected)

    def test_fixture_listener_is_ephemeral_and_protected_ports_are_not_used(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            self.assertNotIn(listener.getsockname()[1], {5000, 15000})
        finally:
            listener.close()

    def test_fixture_child_environment_and_owned_paths_are_explicit(self) -> None:
        source = HARNESS_PATH.read_text(encoding="utf-8")
        self.assertIn("env -i PATH=\"$PATH\" HOME=\"$HOME\"", source)
        for name in ("AI_GATEWAY_ADMIN_DB", "AI_GATEWAY_METRICS_DB", "AI_GATEWAY_RESPONSES_DB", "AI_GATEWAY_RECEIPTS_DB", "AI_GATEWAY_MEDIA_JOBS_DB", "AI_GATEWAY_CONFIG_PATH", "OPENAI_PROXY_KEY_ACTIVITY_LOG", "PYTHONPYCACHEPREFIX"):
            self.assertIn(f'{name}=\"$TMPDIR_ROOT/', source)
        self.assertIn('TMPDIR="$TMPDIR_ROOT"', source)
        self.assertIn('strace -f -qq -o "$TMPDIR_ROOT/file-trace.log" -e trace=%file', source)
        self.assertIn('--assert-file-trace "$TMPDIR_ROOT/file-trace.log" --state-root "$TMPDIR_ROOT"', source)
        self.assertIn('rm -rf "$TMPDIR_ROOT"', source)
        self.assertIn('kill -TERM -- "-$pgid"', source)

    def test_file_trace_rejects_write_outside_fixture_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.log"
            trace.write_text('openat(AT_FDCWD, "/outside/cache.jsonl", O_WRONLY|O_CREAT, 0666) = 3\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "outside its state root"):
                fixture.assert_file_writes_are_rooted(trace, root)


if __name__ == "__main__":
    unittest.main()
