"""Small deterministic guards for the authenticated P1B fixture seam."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
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


_FIXTURE_STORES = {
    "AZIZAM_ADMIN_DB": "admin.db",
    "AZIZAM_METRICS_DB": "metrics.db",
    "AZIZAM_RESPONSES_DB": "responses.db",
    "AZIZAM_RECEIPTS_DB": "receipts.db",
    "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db",
    "AZIZAM_KEY_ACTIVITY_LOG": "key_activity.jsonl",
    "AZIZAM_IMAGE_CACHE_DIR": "images",
    "AZIZAM_VRAM_ESTIMATES_PATH": "vram_estimates.json",
    "AZIZAM_DRIFT_REPORT_PATH": "drift_status.json",
}

# Runs in a child process: start the launcher, wait for its ready file, GET
# /health on the advertised port (unless it is protected), then stop it.
_LAUNCHER_DRIVER = """
import json, os, subprocess, sys, time, urllib.error, urllib.request
helper, state = sys.argv[1], sys.argv[2]
log = open(os.path.join(state, "launcher.log"), "w")
proc = subprocess.Popen([sys.executable, helper, "--serve", "--state-root", state, "--scenario", "all_clear"],
                        stdout=log, stderr=subprocess.STDOUT)
result = {}
try:
    ready = os.path.join(state, "fixture-ready.json")
    deadline = time.monotonic() + 120
    while "port" not in result:
        if proc.poll() is not None or time.monotonic() > deadline:
            log.flush()
            result = {"error": "launcher never became ready", "exit": proc.poll(),
                      "log": open(log.name).read()[-3000:]}
            break
        try:
            result = {"port": json.load(open(ready))["port"], "health": None}
        except (OSError, ValueError, KeyError):
            time.sleep(0.1)
    if result.get("port") not in (None, 5000, 15000):
        while time.monotonic() < deadline and result["health"] is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{result['port']}/health", timeout=2) as response:
                    result["health"] = response.status
            except urllib.error.HTTPError as error:
                result["health"] = error.code
            except OSError:
                time.sleep(0.1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
print(json.dumps(result))
"""


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

    def test_history_dense_scenario_reports_a_real_backendhistory_shape(self) -> None:
        # history_dense is declared fixture data (see _HISTORY_DENSE_SAMPLES),
        # not a live metrics_store computation — but it must still shape
        # exactly like the real BackendHistory contract (web/src/lib/api.ts)
        # and its aggregates must be internally consistent with the raw
        # samples, so a future edit to one without the other fails here
        # rather than silently drifting from what the UI actually renders.
        observed = asyncio.run(fixture._status("history_dense")())
        by_key = {row["key"]: row for row in observed["backends"]}
        history = by_key["fixture-required"]["history"]

        samples = fixture._HISTORY_DENSE_SAMPLES
        expected_uptime_pct = round(100.0 * sum(up for _, up, _, _ in samples) / len(samples), 3)
        expected_avg_latency = round(sum(latency for _, _, _, latency in samples) / len(samples))
        self.assertEqual(history["uptime_pct_24h"], expected_uptime_pct)
        self.assertEqual(history["avg_latency_ms_24h"], expected_avg_latency)
        self.assertGreaterEqual(history["sample_count_24h"], 3)

        for series_key in ("latency_series", "status_series"):
            series = history[series_key]
            self.assertGreaterEqual(len(series), 3)
            for point in series:
                self.assertEqual(len(point), 2)
                self.assertIsInstance(point[0], int)
                self.assertIsInstance(point[1], float)

        self.assertEqual(
            sorted(point[1] for point in history["latency_series"]),
            sorted(latency for _, _, _, latency in samples),
        )
        self.assertEqual(
            sorted(point[1] for point in history["status_series"]),
            sorted(status_code for _, _, status_code, _ in samples),
        )
        # No other scenario fabricates history — the chart-forward Status
        # page must show an honest no-data state for them instead.
        for scenario in fixture.SCENARIOS - {"history_dense", "status_unavailable"}:
            other = asyncio.run(fixture._status(scenario)())
            other_required = next(row for row in other["backends"] if row["key"] == "fixture-required")
            self.assertNotIn("history", other_required)

    def test_launcher_serves_on_an_ephemeral_port_never_a_protected_one(self) -> None:
        # The hermetic guard forbids signalling and connecting to another
        # process's port from this process, so a driver child starts the real
        # launcher, probes the port it advertises and stops it.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            env = {
                **os.environ,
                **{name: str(state / filename) for name, filename in _FIXTURE_STORES.items()},
                "AZIZAM_ADMIN_SESSION_KEY": "launcher-test-session-key-32-bytes-min",
                "AZIZAM_LITELLM_BASE_URL": "http://127.0.0.1:9",
                "AZIZAM_HOST_TELEMETRY_DISABLED": "1",
                "TTS_ENABLED": "false",
                "PYTHONUNBUFFERED": "1",
            }
            driver = subprocess.run(
                [sys.executable, "-c", _LAUNCHER_DRIVER, str(HELPER_PATH), str(state)],
                env=env, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(driver.returncode, 0, driver.stderr[-4000:])
            result = json.loads(driver.stdout.strip().splitlines()[-1])
        self.assertIsNone(result.get("error"), result)
        port = result["port"]
        self.assertNotIn(port, {5000, 15000})
        low, high = map(int, Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
        self.assertTrue(low <= port <= high, f"port {port} is not OS-assigned ({low}-{high})")
        # The advertised port is the live listener, not a stale number.
        self.assertEqual(result["health"], 200, result)

    def test_fixture_child_environment_and_owned_paths_are_explicit(self) -> None:
        source = HARNESS_PATH.read_text(encoding="utf-8")
        self.assertIn('env -i PATH="$PATH" HOME="$TMPDIR_ROOT/home"', source)
        for name in ("AZIZAM_ADMIN_DB", "AZIZAM_METRICS_DB", "AZIZAM_RESPONSES_DB", "AZIZAM_RECEIPTS_DB", "AZIZAM_MEDIA_JOBS_DB", "AZIZAM_CONFIG_PATH", "AZIZAM_KEY_ACTIVITY_LOG", "PYTHONPYCACHEPREFIX"):
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
