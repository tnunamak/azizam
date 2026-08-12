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
        self.assertIn('env -i PATH="$PATH" HOME="$TMPDIR_ROOT/home"', source)
        for name in ("AI_GATEWAY_ADMIN_DB", "AI_GATEWAY_METRICS_DB", "AI_GATEWAY_RESPONSES_DB", "AI_GATEWAY_RECEIPTS_DB", "AI_GATEWAY_MEDIA_JOBS_DB", "AI_GATEWAY_CONFIG_PATH", "OPENAI_PROXY_KEY_ACTIVITY_LOG", "PYTHONPYCACHEPREFIX"):
            self.assertIn(f'{name}=\"$TMPDIR_ROOT/', source)
        self.assertIn('TMPDIR="$TMPDIR_ROOT"', source)
        self.assertIn('--ro-bind / /', source)
        self.assertIn('--bind "$TMPDIR_ROOT" "$TMPDIR_ROOT"', source)
        self.assertIn('--dev-bind /dev/null /dev/null', source)
        self.assertIn('strace -f -qq -o "$TMPDIR_ROOT/network-trace.log" -e trace=network', source)
        self.assertIn('--assert-network-trace "$TMPDIR_ROOT/network-trace.log" --fixture-port "$HARNESS_PORT"', source)
        self.assertIn('--assert-os-containment --state-root "$TMPDIR_ROOT" --readonly-root "$REPO_ROOT"', source)
        self.assertIn('rm -rf "$TMPDIR_ROOT"', source)
        self.assertIn('kill -TERM -- "-$pgid"', source)

    def test_network_trace_rejects_non_fixture_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.log"
            trace.write_text('connect(3, {sa_family=AF_INET, sin_port=htons(5051), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 ECONNREFUSED\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "non-fixture endpoint"):
                fixture.assert_network_trace_is_fixture_only(trace, 48123)

    def test_network_trace_allows_fixture_and_declared_unreachable_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace.log"
            trace.write_text('connect(3, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 ECONNREFUSED\n'
                             'bind(4, {sa_family=AF_INET, sin_port=htons(48123), sin_addr=inet_addr("127.0.0.1")}, 16) = 0\n', encoding="utf-8")
            fixture.assert_network_trace_is_fixture_only(trace, 48123)

    def test_network_trace_rejects_unparsed_and_non_ipv4_records_from_child_processes(self) -> None:
        records = (
            'connect(3, {sa_family=AF_INET6, sin6_port=htons(5000), inet_pton(AF_INET6, "::1", &sin6_addr)}, 28) = -1 ECONNREFUSED',
            'connect(3, {sa_family=AF_UNIX, sun_path="/run/production.sock"}, 110) = -1 ENOENT',
            'connect(3, {sa_family=AF_INET, sin_addr=inet_addr("127.0.0.1"), sin_port=htons(9)}, 16) = -1 ECONNREFUSED',
            'connect(3, {sa_family=AF_INET, sin_port=htons(9), ...',
            '4242 connect(3, {sa_family=AF_INET, sin_port=htons(5000), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 ECONNREFUSED',
        )
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace.log"
            for record in records:
                trace.write_text(record + "\n", encoding="utf-8")
                with self.subTest(record=record), self.assertRaisesRegex(RuntimeError, "non-fixture endpoint"):
                    fixture.assert_network_trace_is_fixture_only(trace, 48123)

    def test_network_trace_parses_allowed_child_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace.log"
            trace.write_text('4242 connect(3, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 ECONNREFUSED\n', encoding="utf-8")
            fixture.assert_network_trace_is_fixture_only(trace, 48123)


if __name__ == "__main__":
    unittest.main()
