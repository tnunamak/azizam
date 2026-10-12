"""scripts/safe-restart logic with systemctl and HTTP mocked."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import time
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("safe_restart", str(ROOT / "scripts" / "safe-restart"))
_spec = importlib.util.spec_from_loader("safe_restart", _loader)
sr = importlib.util.module_from_spec(_spec)
_loader.exec_module(sr)

TOKEN = "test-admin-token-123"


def snap(*, draining=False, requests=0, streams=0, chat=0, jobs=None, inflight=None, quiet="unset"):
    jobs = jobs or {}
    extra = {} if quiet == "unset" else {"seconds_since_chat_activity": quiet}
    return {
        **extra,
        "draining": draining,
        "active": {
            "requests": requests,
            "streams": streams,
            "chat": chat,
            "media": 0,
            "async_media_jobs": jobs,
            "async_media_jobs_total": sum(jobs.values()),
            "total": requests + sum(jobs.values()),
        },
        "inflight": inflight or [],
    }


class World:
    """A scripted host: a gateway drain API, health probes, llama slots, systemctl."""

    def __init__(self, snapshots=None):
        self.now = 0.0
        self.snapshots = list(snapshots or [snap()])  # consumed by GET drain; last one repeats
        self.draining = False
        self.calls: list[tuple] = []
        self.commands: list[list[str]] = []
        self.output: list[str] = []
        self.drain_status = 200
        self.unreachable = False
        self.health_after_restart: list = [200]
        self.public_health: list = [200]
        self.restarted = False
        self.running = True
        self.systemctl_code = 0
        self.llama_slots: list = [[{"is_processing": False}]]
        self.post_restart_snapshot = snap()
        self.sleeps = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds

    def http(self, method, url, *, token=None, body=None, timeout=10.0):
        self.calls.append((method, url, token, body))
        if self.unreachable and not (self.restarted and url.endswith("/health")):
            raise sr.Unreachable("connection refused")
        if url.endswith("/admin/api/drain"):
            if token != TOKEN:
                return 401, {"error": {"code": "admin_auth_required"}}
            if self.drain_status != 200:
                return self.drain_status, {"error": {"code": "x"}}
            if method == "POST":
                self.draining = bool(body["draining"])
                return 200, {**snap(draining=self.draining), "active": snap()["active"]}
            if self.restarted:
                return 200, {**self.post_restart_snapshot, "draining": self.draining}
            current = self.snapshots[0] if len(self.snapshots) == 1 else self.snapshots.pop(0)
            return 200, {**current, "draining": self.draining}
        if url.endswith("/slots"):
            current = self.llama_slots[0] if len(self.llama_slots) == 1 else self.llama_slots.pop(0)
            return 200, current
        if url.startswith("https://") and url.endswith("/health"):
            code = self.public_health[0] if len(self.public_health) == 1 else self.public_health.pop(0)
            return code, {"status": "healthy"}
        if url.endswith("/health"):
            if not self.restarted:
                return 200, {"status": "healthy"}
            code = self.health_after_restart[0] if len(self.health_after_restart) == 1 else self.health_after_restart.pop(0)
            if code == "down":
                raise sr.Unreachable("connection refused")
            return code, {"status": "healthy"}
        raise AssertionError(f"unexpected request {method} {url}")

    def run(self, cmd, timeout):
        self.commands.append(list(cmd))
        if cmd[2] == "stop":
            if self.systemctl_code == 0:
                self.running = False
            return self.systemctl_code
        self.restarted = True
        if self.systemctl_code == 0:
            self.running = True
            self.draining = False if cmd[-1] == "azizam" else self.draining
        return self.systemctl_code

    def invoke(self, *argv, token=TOKEN, web_dir=None):
        lock = tempfile.NamedTemporaryFile(suffix=".lock", delete=False)
        lock.close()
        args = sr.build_parser().parse_args([
            "--lock-file", lock.name, "--poll", "1",
            *([] if web_dir is not None else ["--skip-web-build"]), *argv,
        ])
        restarter = sr.Restarter(
            args,
            token=token,
            http=self.http,
            run=self.run,
            sleep=self.sleep,
            clock=self.clock,
            out=self.output.append,
            web_dir=web_dir,
        )
        self.restarter = restarter
        return restarter.execute()

    def posts(self):
        return [c[3] for c in self.calls if c[0] == "POST"]

    @property
    def text(self):
        return "\n".join(self.output)


class SafeRestartTests(unittest.TestCase):
    def test_idle_gateway_drains_restarts_and_leaves_drain_off(self) -> None:
        world = World()
        code = world.invoke()
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertEqual(world.commands, [["systemctl", "--user", "restart", "azizam"]])
        self.assertEqual(world.posts()[0]["draining"], True)
        self.assertEqual(world.posts()[0]["reason"], "safe-restart")
        self.assertFalse(world.draining)
        self.assertIn("draining is off", world.text)

    def test_waits_for_stream_to_finish_before_restarting(self) -> None:
        busy = snap(requests=1, streams=1, chat=1, inflight=[
            {"method": "POST", "path": "/v1/chat/completions", "kind": "chat", "stream": True, "age_s": 12}])
        world = World([busy, busy, busy, snap()])
        code = world.invoke()
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertGreaterEqual(world.sleeps, 2)
        order = [("POST" if c[0] == "POST" else "GET") for c in world.calls]
        self.assertEqual(order[0], "GET")
        self.assertEqual(len(world.commands), 1)
        self.assertIn("waiting: 1 in flight", world.text)

    def test_deadline_prints_leftovers_does_not_restart_and_undrains(self) -> None:
        busy = snap(requests=1, streams=1, chat=1, inflight=[
            {"method": "POST", "path": "/v1/chat/completions", "kind": "chat", "stream": True, "age_s": 700}])
        world = World([busy])
        code = world.invoke("--timeout", "5", "--quiet-seconds", "0")
        self.assertEqual(code, sr.EXIT_BUSY)
        self.assertEqual(world.commands, [])
        self.assertIn("/v1/chat/completions", world.text)
        self.assertIn("NOT restarting", world.text)
        self.assertEqual([p["draining"] for p in world.posts()], [True, False])
        self.assertFalse(world.draining)

    def test_async_media_jobs_block_until_ignored(self) -> None:
        busy = snap(jobs={"video": 1})
        world = World([busy])
        self.assertEqual(world.invoke("--timeout", "3"), sr.EXIT_BUSY)
        self.assertIn("async video media job", world.text)
        world = World([busy])
        self.assertEqual(world.invoke("--timeout", "3", "--ignore-async-jobs"), sr.EXIT_OK, world.text)

    def test_unreachable_drain_endpoint_refuses_without_force(self) -> None:
        world = World()
        world.unreachable = True
        code = world.invoke()
        self.assertEqual(code, sr.EXIT_REFUSED)
        self.assertEqual(world.commands, [])
        self.assertIn("REFUSING", world.text)

    def test_missing_drain_endpoint_404_refuses(self) -> None:
        world = World()
        world.drain_status = 404
        self.assertEqual(world.invoke(), sr.EXIT_REFUSED)
        self.assertEqual(world.commands, [])
        self.assertIn("no drain endpoint", world.text)

    def test_missing_admin_key_refuses(self) -> None:
        world = World()
        self.assertEqual(world.invoke(token=None), sr.EXIT_REFUSED)
        self.assertEqual(world.commands, [])
        self.assertIn("no admin key", world.text)

    def test_wrong_admin_key_refuses(self) -> None:
        world = World()
        self.assertEqual(world.invoke(token="wrong"), sr.EXIT_REFUSED)
        self.assertEqual(world.commands, [])
        self.assertNotIn("wrong", world.text)

    def test_force_restarts_without_endpoint_and_warns(self) -> None:
        world = World()
        world.drain_status = 404
        code = world.invoke("--force")
        self.assertEqual(code, sr.EXIT_UNCONFIRMED, world.text)
        self.assertEqual(len(world.commands), 1)
        self.assertIn("WITHOUT draining", world.text)
        self.assertIn("will be cut", world.text)

    def test_force_skips_the_wait_and_lists_what_is_cut(self) -> None:
        busy = snap(requests=2, streams=1, chat=1, inflight=[
            {"method": "POST", "path": "/v1/chat/completions", "kind": "chat", "stream": True, "age_s": 3}])
        world = World([busy])
        code = world.invoke("--force")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertIn("cutting 2 in-flight", world.text)
        self.assertIn("/v1/chat/completions", world.text)
        self.assertEqual(len(world.commands), 1)

    def test_dry_run_changes_nothing_and_reports_busy(self) -> None:
        busy = snap(requests=1, chat=1, inflight=[
            {"method": "POST", "path": "/v1/chat/completions", "kind": "chat", "stream": True, "age_s": 9}])
        world = World([busy])
        self.assertEqual(world.invoke("--dry-run"), sr.EXIT_BUSY)
        self.assertEqual(world.commands, [])
        self.assertEqual(world.posts(), [])
        idle = World()
        self.assertEqual(idle.invoke("--dry-run"), sr.EXIT_OK)
        self.assertEqual(idle.posts(), [])
        self.assertEqual(idle.commands, [])

    def test_systemctl_failure_exits_5_and_undrains(self) -> None:
        world = World()
        world.systemctl_code = 1
        self.assertEqual(world.invoke(), sr.EXIT_SYSTEMCTL)
        self.assertEqual(world.posts()[-1]["draining"], False)

    def test_unhealthy_after_restart_exits_1(self) -> None:
        world = World()
        world.health_after_restart = [503]
        code = world.invoke("--health-timeout", "4")
        self.assertEqual(code, sr.EXIT_UNCONFIRMED)
        self.assertIn("did not become healthy", world.text)

    def test_waits_for_the_public_proxy_to_mark_the_gateway_up(self) -> None:
        world = World()
        world.public_health = [503, 503, 200]
        self.assertEqual(world.invoke("--public-url", "https://ai.example.test"), sr.EXIT_OK, world.text)
        public = [c for c in world.calls if c[1] == "https://ai.example.test/health"]
        self.assertEqual(len(public), 3)
        self.assertIn("public health is OK", world.text)

    def test_public_health_timeout_is_unconfirmed(self) -> None:
        world = World()
        world.public_health = [503]
        self.assertEqual(world.invoke("--public-url", "https://ai.example.test", "--public-timeout", "4"), sr.EXIT_UNCONFIRMED, world.text)
        self.assertIn("did not return 200", world.text)
        empty = World()
        self.assertEqual(empty.invoke("--public-url", ""), sr.EXIT_OK)
        self.assertFalse([c for c in empty.calls if c[1].startswith("https://")])

    def test_public_check_is_off_by_default_and_says_how_to_enable_it(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZIZAM_PUBLIC_URL", None)
            world = World()
            self.assertEqual(world.invoke(), sr.EXIT_OK, world.text)
        self.assertFalse([c for c in world.calls if c[1].startswith("https://")])
        self.assertIn("public health check skipped: set --public-url or AZIZAM_PUBLIC_URL", world.text)

    def test_waits_through_connection_refused_while_starting(self) -> None:
        world = World()
        world.health_after_restart = ["down", "down", 200]
        self.assertEqual(world.invoke(), sr.EXIT_OK, world.text)

    def test_drain_flag_found_on_after_restart_is_cleared(self) -> None:
        world = World()
        world.post_restart_snapshot = snap(draining=True)
        orig_run = world.run

        def run(cmd, timeout):
            code = orig_run(cmd, timeout)
            world.draining = True  # simulate a flag that survived
            return code

        world.run = run
        self.assertEqual(world.invoke(), sr.EXIT_OK, world.text)
        self.assertEqual(world.posts()[-1]["draining"], False)
        self.assertFalse(world.draining)

    def test_ttl_follows_timeout_and_is_capped(self) -> None:
        world = World()
        world.invoke("--timeout", "100")
        self.assertEqual(world.posts()[0]["ttl_s"], 700)
        world = World()
        world.invoke("--timeout", "99999", "--ignore-async-jobs")
        self.assertEqual(world.posts()[0]["ttl_s"], 7200)

    def test_token_never_appears_in_output(self) -> None:
        world = World([snap(requests=1, chat=1)])
        world.invoke("--timeout", "2")
        self.assertNotIn(TOKEN, world.text)

    def test_keyboard_interrupt_while_waiting_undrains(self) -> None:
        world = World([snap(requests=1, chat=1)])

        def boom(seconds):
            raise KeyboardInterrupt

        world.sleep = boom
        code = world.invoke("--quiet-seconds", "0")
        self.assertEqual(code, sr.EXIT_INTERRUPTED)
        self.assertEqual(world.commands, [])
        self.assertEqual(world.posts()[-1]["draining"], False)

    def test_keyboard_interrupt_during_quiet_wait_never_drained(self) -> None:
        world = World([snap(requests=1, chat=1)])

        def boom(seconds):
            raise KeyboardInterrupt

        world.sleep = boom
        code = world.invoke()
        self.assertEqual(code, sr.EXIT_INTERRUPTED)
        self.assertEqual(world.commands, [])
        self.assertEqual(world.posts(), [])

    def test_failed_observation_never_reuses_idle_snapshot(self):
        for failure in ("timeout", "auth", "malformed", "http"):
            with self.subTest(failure=failure):
                world = World()
                http = world.http

                def unavailable(method, url, **kwargs):
                    if method == "GET" and url.endswith("/admin/api/drain") and world.draining:
                        if failure == "timeout":
                            raise sr.Unreachable("timeout")
                        return {"auth": (401, {}), "malformed": (200, {"draining": True}),
                                "http": (503, {})}[failure]
                    return http(method, url, **kwargs)

                world.http = unavailable
                self.assertEqual(world.invoke("--quiet-seconds", "0", "--timeout", "1"), sr.EXIT_BUSY)
                self.assertEqual(world.commands, [])
                self.assertFalse(world.draining)
                self.assertIn("cannot prove idle", world.text)

    def test_transient_observation_failure_retries_busy_work(self):
        world = World()
        http = world.http
        reads = 0

        def transient(method, url, **kwargs):
            nonlocal reads
            if method == "GET" and url.endswith("/admin/api/drain") and world.draining:
                reads += 1
                if reads == 1:
                    return 200, snap(draining=True, requests=1)
                if reads == 2:
                    raise sr.Unreachable("timeout")
            return http(method, url, **kwargs)

        world.http = transient
        self.assertEqual(world.invoke("--quiet-seconds", "0"), sr.EXIT_OK)
        self.assertGreaterEqual(world.sleeps, 2)
        self.assertEqual(len(world.commands), 1)

    def test_lost_drain_flag_cannot_authorize_restart(self):
        world = World()
        http = world.http

        def expired(method, url, **kwargs):
            if method == "GET" and url.endswith("/admin/api/drain") and world.draining:
                return 200, snap(draining=False)
            return http(method, url, **kwargs)

        world.http = expired
        self.assertEqual(world.invoke("--quiet-seconds", "0", "--timeout", "1"), sr.EXIT_BUSY)
        self.assertEqual(world.commands, [])


class QuietWindowTests(unittest.TestCase):
    def test_waits_for_quiet_before_draining(self) -> None:
        # Idle, but the last chat call was 3 s ago: an agent may be between tool steps.
        world = World([snap(quiet=3.0), snap(quiet=8.0), snap(quiet=21.0)])
        code = world.invoke("--quiet-seconds", "20")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertIn("without chat activity", world.text)
        self.assertIn("idle and quiet", world.text)
        # No drain request may precede the quiet confirmation.
        first_post = next(i for i, c in enumerate(world.calls) if c[0] == "POST")
        gets_before = [c for c in world.calls[:first_post] if c[0] == "GET" and c[1].endswith("/admin/api/drain")]
        self.assertGreaterEqual(len(gets_before), 3)
        self.assertEqual(world.posts()[0]["draining"], True)
        self.assertEqual(len(world.commands), 1)

    def test_new_chat_during_quiet_wait_restarts_the_clock(self) -> None:
        world = World([snap(quiet=15.0), snap(requests=1, chat=1, quiet=0.0), snap(quiet=2.0), snap(quiet=25.0)])
        code = world.invoke("--quiet-seconds", "20")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertIn("1 in flight", world.text)
        self.assertEqual(world.posts()[0]["draining"], True)
        self.assertEqual(len(world.commands), 1)

    def test_deadline_in_quiet_wait_leaves_gateway_undrained(self) -> None:
        world = World([snap(quiet=1.0)])
        code = world.invoke("--quiet-seconds", "20", "--timeout", "10")
        self.assertEqual(code, sr.EXIT_BUSY, world.text)
        self.assertEqual(world.commands, [])
        self.assertEqual(world.posts(), [])  # never drained: agents were never refused
        self.assertIn("last chat activity", world.text)
        self.assertIn("Nothing was drained", world.text)

    def test_zero_disables_the_quiet_check(self) -> None:
        world = World([snap(quiet=0.5)])
        code = world.invoke("--quiet-seconds", "0")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertEqual(world.sleeps, 0)

    def test_force_skips_the_quiet_check(self) -> None:
        world = World([snap(quiet=0.5)])
        self.assertEqual(world.invoke("--force"), sr.EXIT_OK, world.text)
        self.assertEqual(world.sleeps, 0)

    def test_older_gateway_without_the_field_warns_and_proceeds(self) -> None:
        world = World([snap()])
        code = world.invoke()
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertIn("does not report chat activity", world.text)

    def test_never_chatted_counts_as_quiet(self) -> None:
        world = World([snap(quiet=None)])
        self.assertEqual(world.invoke(), sr.EXIT_OK, world.text)
        self.assertEqual(world.sleeps, 0)

    def test_request_slipping_in_after_quiet_is_waited_for_after_drain(self) -> None:
        # Quiet at the check, then a chat appears before the drain flag lands.
        busy = snap(requests=1, chat=1, quiet=0.0)
        world = World([snap(quiet=30.0), snap(quiet=30.0), busy, snap(quiet=0.5)])
        code = world.invoke("--quiet-seconds", "20")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertEqual(world.posts()[0]["draining"], True)

    def test_media_in_flight_does_not_hold_phase_one(self) -> None:
        # Chat is quiet; a slow image edit runs. Drain at once so new media stops,
        # then wait for the edit in phase 2.
        media = snap(requests=1, quiet=40.0)
        world = World([media, media, snap(quiet=45.0)])
        code = world.invoke("--quiet-seconds", "20")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertEqual(world.posts()[0]["draining"], True)
        self.assertLess(world.sleeps, 3)

    def test_negative_quiet_is_a_usage_error(self) -> None:
        args = sr.build_parser().parse_args(["--quiet-seconds", "-1"])
        self.assertIn("quiet", sr.validate(args))


class LlamaModeTests(unittest.TestCase):
    def test_llama_drains_chat_restarts_llama_and_undrains_gateway(self) -> None:
        busy = snap(requests=1, chat=1, streams=1)
        world = World([busy, snap()])
        code = world.invoke("--llama")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertEqual(world.commands, [["systemctl", "--user", "restart", "llama-bee"]])
        # gateway stayed up, so the flag must have been switched off explicitly
        self.assertEqual([p["draining"] for p in world.posts()], [True, False])
        self.assertFalse(world.draining)
        health_urls = [c[1] for c in world.calls if c[1].endswith("/health")]
        self.assertTrue(any(":5051" in u for u in health_urls))

    def test_llama_waits_for_busy_slot_even_if_gateway_shows_no_chat(self) -> None:
        world = World([snap()])
        world.llama_slots = [[{"is_processing": True}], [{"is_processing": True}], [{"is_processing": False}]]
        code = world.invoke("--llama")
        self.assertEqual(code, sr.EXIT_OK, world.text)
        self.assertGreaterEqual(world.sleeps, 2)
        self.assertIn("llama busy slots: 1", world.text)

    def test_llama_ignores_media_requests_and_jobs(self) -> None:
        world = World([snap(requests=3, chat=0, jobs={"video": 2})])
        self.assertEqual(world.invoke("--llama"), sr.EXIT_OK, world.text)

    def test_llama_deadline_does_not_restart(self) -> None:
        world = World([snap(requests=1, chat=1)])
        self.assertEqual(world.invoke("--llama", "--timeout", "3"), sr.EXIT_BUSY)
        self.assertEqual(world.commands, [])
        self.assertFalse(world.draining)

    def test_llama_refuses_when_gateway_drain_unreachable(self) -> None:
        world = World()
        world.unreachable = True
        self.assertEqual(world.invoke("--llama"), sr.EXIT_REFUSED)
        self.assertEqual(world.commands, [])


class MainTests(unittest.TestCase):
    def test_rejects_non_loopback_url_and_bad_timeout(self) -> None:
        self.assertEqual(sr.main(["--url", "http://10.0.0.5:5000"], env={}), sr.EXIT_USAGE)
        self.assertEqual(sr.main(["--timeout", "0"], env={}), sr.EXIT_USAGE)
        self.assertEqual(sr.main(["--bogus"], env={}), sr.EXIT_USAGE)

    def test_resolve_token_prefers_env_then_file(self) -> None:
        self.assertEqual(sr.resolve_token({"AZIZAM_ADMIN_KEY": " abc "}), "abc")
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "k"
            key_file.write_text("from-file\n")
            self.assertEqual(sr.resolve_token({"AZIZAM_ADMIN_KEY_FILE": str(key_file)}), "from-file")
            self.assertIsNone(sr.resolve_token({"AZIZAM_ADMIN_KEY_FILE": str(Path(tmp) / "none")}))

    def test_second_instance_is_locked_out(self) -> None:
        import fcntl

        with tempfile.NamedTemporaryFile() as lock:
            handle = open(lock.name, "w")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                code = sr.main(["--lock-file", lock.name], env={})
            finally:
                handle.close()
        self.assertEqual(code, sr.EXIT_LOCKED)


def _process_alive(pid):
    # Read /proc rather than os.kill(pid, 0): the hermetic runner forbids os.kill.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        # The process can exit after /proc lookup but before open completes.
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


class RunCommandTests(unittest.TestCase):
    def test_timeout_kills_grandchildren(self):
        # Like `npm run build` -> sh -> `next build`: the grandchild must die too.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            script = f"sleep 60 & echo $! > {pid_file}; wait"

            class ReadyPopen(subprocess.Popen):
                """Starts the timeout clock only once the grandchild exists."""

                def wait(self, timeout=None):
                    ready_by = time.monotonic() + 30  # watchdog; the pid file is the event
                    while not (pid_file.exists() and pid_file.read_text().endswith("\n")):
                        if time.monotonic() > ready_by:
                            raise AssertionError("grandchild never started")
                        time.sleep(0.01)
                    return super().wait(timeout)

            with patch.object(sr.subprocess, "Popen", ReadyPopen):
                code = sr.run_command(["sh", "-c", f"sh -c '{script}'"], timeout=1)
            self.assertEqual(code, 124)
            grandchild = int(pid_file.read_text())
            deadline = time.time() + 5
            while time.time() < deadline and _process_alive(grandchild):
                time.sleep(0.05)
            if _process_alive(grandchild):
                os.killpg(os.getpgid(grandchild), 9)
                self.fail("grandchild survived the timeout")

    def test_missing_command(self):
        self.assertEqual(sr.run_command(["/nonexistent/azizam-cmd"], timeout=1), 127)


class WebBuildTests(unittest.TestCase):
    def make_web(self, root, *, fresh=False):
        web = Path(root) / "web"
        (web / "src").mkdir(parents=True)
        identity = Path(root) / "gateway" / "product_identity.json"
        identity.parent.mkdir()
        identity.write_text('{"name": "Azizam"}\n')
        os.utime(identity, (100, 100))
        pages = Path(root) / "gateway" / "ui_pages.json"
        pages.write_text('{"pages": {"": "public"}}\n')
        os.utime(pages, (100, 100))
        source = web / "src" / "page.tsx"
        source.write_text("export default function Page() {}\n")
        (web / "out").mkdir()
        output = web / "out" / "index.html"
        output.write_text("<html></html>\n")
        os.utime(source, (100, 100))
        os.utime(web / "src", (100, 100))
        os.utime(output, (200 if fresh else 50, 200 if fresh else 50))
        if fresh:
            stamp = web / "out" / sr.WEB_BUILD_STAMP
            stamp.write_text(json.dumps(["src/page.tsx"]))
            os.utime(stamp, (200, 200))
        return web

    def test_stale_sources_build_before_first_drain_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            original_run = world.run

            def run(cmd, timeout):
                if cmd[0] == "npm":
                    self.assertEqual(world.calls, [])
                    self.assertFalse(world.draining)
                    self.assertEqual(cmd[:2], ["npm", "--prefix"])
                    self.assertEqual(cmd[3:], ["run", "build"])
                    build_web = Path(cmd[2])
                    self.assertNotEqual(build_web, web)
                    (build_web / "out").mkdir()
                    (build_web / "out" / "index.html").write_text("built")
                    world.commands.append(list(cmd))
                    return 0
                return original_run(cmd, timeout)

            world.run = run
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual([cmd[0] for cmd in world.commands], ["npm", "systemctl", "systemctl"])
            self.assertFalse(sr.web_build_required(web))

    def test_staged_build_sees_inputs_outside_web(self):
        # web/src/lib/product.ts imports ../../../gateway/product_identity.json.
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            original_run = world.run
            seen = {}

            def run(cmd, timeout):
                if cmd[0] == "npm":
                    build_web = Path(cmd[2])
                    seen["identity"] = (build_web.parent / "gateway" / "product_identity.json").read_text()
                    (build_web / "out").mkdir()
                    (build_web / "out" / "index.html").write_text("built")
                    return 0
                return original_run(cmd, timeout)

            world.run = run
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual(seen["identity"], '{"name": "Azizam"}\n')

    def test_shared_input_edit_requires_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp, fresh=True)
            self.assertFalse(sr.web_build_required(web))
            os.utime(Path(tmp) / "gateway" / "product_identity.json", (300, 300))
            self.assertTrue(sr.web_build_required(web))

    def test_export_failure_preserves_served_output(self):
        for outcome in (1, 124, KeyboardInterrupt):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                web = self.make_web(tmp)
                original = (web / "out" / "index.html").read_bytes()
                world = World()

                def export(cmd, timeout):
                    build_web = Path(cmd[2])
                    shutil.rmtree(build_web / "out", ignore_errors=True)
                    (build_web / "out").mkdir()
                    (build_web / "out" / "partial.js").write_text("partial")
                    if outcome is KeyboardInterrupt:
                        raise KeyboardInterrupt
                    return outcome

                world.run = export
                if outcome is KeyboardInterrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        world.invoke(web_dir=web)
                else:
                    self.assertEqual(world.invoke(web_dir=web), sr.EXIT_WEB_BUILD)
                self.assertTrue((web / "out" / "index.html").is_file())
                self.assertEqual((web / "out" / "index.html").read_bytes(), original)
                self.assertFalse((web / "out" / "partial.js").exists())
                self.assertEqual(world.calls, [])

    def test_successful_export_publishes_complete_output_after_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            original_run = world.run

            def export(cmd, timeout):
                if cmd[0] != "npm":
                    return original_run(cmd, timeout)
                build_web = Path(cmd[2])
                shutil.rmtree(build_web / "out", ignore_errors=True)
                (build_web / "out").mkdir()
                (build_web / "out" / "index.html").write_text("new page")
                self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
                (build_web / "out" / "app.js").write_text("complete bundle")
                return 0

            world.run = export
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual((web / "out" / "index.html").read_text(), "new page")
            self.assertEqual((web / "out" / "app.js").read_text(), "complete bundle")
            self.assertFalse(sr.web_build_required(web))

    def test_publication_failure_preserves_served_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            original_run = world.run

            def export(cmd, timeout):
                if cmd[0] != "npm":
                    return original_run(cmd, timeout)
                build_web = Path(cmd[2])
                (build_web / "out").mkdir()
                (build_web / "out" / "index.html").write_text("new page")
                return 0

            world.run = export
            with patch.object(sr, "publish_web_export", side_effect=OSError("exchange refused")):
                self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), sr.EXIT_WEB_BUILD)
            self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
            self.assertEqual([cmd[2] for cmd in world.commands], ["stop", "stop"])
            self.assertEqual([p["draining"] for p in world.posts()], [True])
            self.assertFalse(world.running)
            self.assertIn("Recovery:", world.text)
            self.assertIn("exchange refused", world.text)
            self.assertEqual(self.staged(tmp), [])

    @staticmethod
    def staged(root):
        return sorted(path.name for path in Path(root).glob(".azizam-web-*"))

    def build_world(self, web, events):
        world = World()
        original_run = world.run
        original_http = world.http

        def run(cmd, timeout):
            if cmd[0] == "npm":
                build_web = Path(cmd[2])
                (build_web / "out").mkdir()
                (build_web / "out" / "index.html").write_text("new page")
                return 0
            events.append((cmd[2], (web / "out" / "index.html").read_text()))
            return original_run(cmd, timeout)

        def http(method, url, **kwargs):
            if method == "POST" and url.endswith("/admin/api/drain"):
                events.append(("drain" if kwargs["body"]["draining"] else "undrain",
                               (web / "out" / "index.html").read_text()))
            return original_http(method, url, **kwargs)

        world.run = run
        world.http = http
        return world

    def test_export_is_published_between_stop_and_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            events = []
            world = self.build_world(web, events)
            self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), sr.EXIT_OK)
            self.assertEqual(events, [("drain", "<html></html>\n"), ("stop", "<html></html>\n"), ("start", "new page")])
            self.assertLess(world.output.index("published the new web export"),
                            world.output.index("starting azizam"))
            self.assertEqual(self.staged(tmp), [])

    def test_drain_timeout_leaves_served_export_byte_identical(self):
        busy = snap(requests=1, inflight=[{"method": "POST", "path": "/v1/images", "age_s": 9}])
        for argv in (("--timeout", "5", "--quiet-seconds", "0"), ("--timeout", "5", "--quiet-seconds", "60")):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                web = self.make_web(tmp)
                before = {p.name: p.read_bytes() for p in (web / "out").iterdir()}
                events = []
                world = self.build_world(web, events)
                world.snapshots = [snap(requests=1, chat=1, quiet=0.0, inflight=busy["inflight"])]
                self.assertEqual(world.invoke(*argv, web_dir=web), sr.EXIT_BUSY)
                self.assertEqual({p.name: p.read_bytes() for p in (web / "out").iterdir()}, before)
                self.assertEqual(world.commands, [])
                self.assertEqual(self.staged(tmp), [])

    def test_refused_drain_discards_the_staged_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = self.build_world(web, [])
            world.drain_status = 404
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_REFUSED)
            self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
            self.assertEqual(self.staged(tmp), [])

    def test_failed_stop_leaves_old_process_and_old_export_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = self.build_world(web, [])
            world.systemctl_code = 1
            self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), sr.EXIT_SYSTEMCTL)
            self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
            self.assertTrue(world.running)
            self.assertTrue(world.draining)
            self.assertEqual([cmd[2] for cmd in world.commands], ["stop"])
            self.assertEqual(self.staged(tmp), [])
            self.assertIn("Recovery: systemctl --user stop azizam && systemctl --user start azizam", world.text)

    def test_failed_or_interrupted_start_rolls_back_only_while_stopped(self):
        for failure in (1, KeyboardInterrupt):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                web = self.make_web(tmp)
                world = self.build_world(web, [])
                run = world.run

                def fail_start(cmd, timeout):
                    if cmd[2] == "start":
                        self.assertFalse(world.running)
                        self.assertEqual((web / "out" / "index.html").read_text(), "new page")
                        if failure is KeyboardInterrupt:
                            raise KeyboardInterrupt
                        return failure
                    return run(cmd, timeout)

                world.run = fail_start
                expected = sr.EXIT_INTERRUPTED if failure is KeyboardInterrupt else sr.EXIT_SYSTEMCTL
                self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), expected)
                self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
                self.assertFalse(world.running)
                self.assertNotIn(False, [p["draining"] for p in world.posts()])
                [holder] = self.staged(tmp)
                self.assertEqual((Path(tmp) / holder / "out" / "index.html").read_text(), "new page")
                self.assertIn("Recovery:", world.text)
                self.assertNotIn("rm -rf", world.text)

    def test_ambiguous_start_and_failed_cleanup_keep_both_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = self.build_world(web, [])
            run = world.run
            stops = 0

            def ambiguous(cmd, timeout):
                nonlocal stops
                if cmd[2] == "start":
                    world.running = True  # new process started, but controller failed
                    return 1
                if cmd[2] == "stop":
                    stops += 1
                    if stops > 1:
                        return 1
                return run(cmd, timeout)

            world.run = ambiguous
            self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), sr.EXIT_SYSTEMCTL)
            self.assertEqual((web / "out" / "index.html").read_text(), "new page")
            [holder] = self.staged(tmp)
            self.assertEqual((Path(tmp) / holder / "out" / "index.html").read_text(), "<html></html>\n")
            self.assertNotIn(False, [p["draining"] for p in world.posts()])
            self.assertIn("publish_web_export", world.text)
            self.assertIn("systemctl --user stop azizam && python3 -c", world.text)
            self.assertNotIn("rm -rf", world.text)

    def test_interrupt_after_exchange_retains_recovery_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = self.build_world(web, [])
            publish = sr.Restarter._publish_staged_export

            def interrupt(restarter):
                publish(restarter)
                raise KeyboardInterrupt

            with patch.object(sr.Restarter, "_publish_staged_export", interrupt):
                self.assertEqual(world.invoke("--quiet-seconds", "0", web_dir=web), sr.EXIT_INTERRUPTED)
            self.assertEqual((web / "out" / "index.html").read_text(), "<html></html>\n")
            self.assertFalse(world.running)
            self.assertEqual(len(self.staged(tmp)), 1)

    def test_unconfirmed_check_keeps_new_process_and_previous_export(self):
        for failure in ("public", "get", "clear"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                web = self.make_web(tmp)
                world = self.build_world(web, [])
                http = world.http
                if failure == "public":
                    world.public_health = [503]
                    extra = ("--public-url", "https://ai.example.test")
                else:
                    extra = ()

                def fail_verification(method, url, **kwargs):
                    if world.restarted and url.endswith("/admin/api/drain"):
                        if failure == "get":
                            return 503, {}
                        if failure == "clear":
                            return 200, snap(draining=True)
                    return http(method, url, **kwargs)

                world.http = fail_verification
                self.assertEqual(world.invoke("--quiet-seconds", "0", "--public-timeout", "1", *extra, web_dir=web),
                                 sr.EXIT_UNCONFIRMED)
                # Left running: the new process serves the new export.
                self.assertTrue(world.running)
                self.assertEqual([cmd[2] for cmd in world.commands], ["stop", "start"])
                self.assertEqual((web / "out" / "index.html").read_text(), "new page")
                [holder] = self.staged(tmp)
                self.assertEqual((Path(tmp) / holder / "out" / "index.html").read_text(), "<html></html>\n")
                self.assertIn("Recovery:", world.text)

    def test_failed_build_never_drains_or_restarts_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            world.run = lambda cmd, timeout: 1
            self.assertEqual(world.invoke("--force", web_dir=web), sr.EXIT_WEB_BUILD)
            self.assertEqual(world.calls, [])
            self.assertFalse((web / "out" / sr.WEB_BUILD_STAMP).exists())

    def test_missing_export_builds_and_refuses_incomplete_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            (web / "out" / "index.html").unlink()
            self.assertTrue(sr.web_build_required(web))
            world = World()
            world.run = lambda cmd, timeout: 0
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_WEB_BUILD)
            self.assertEqual(world.calls, [])

    def test_fresh_export_ignores_dependency_and_build_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp, fresh=True)
            for directory in ("node_modules", ".next"):
                (web / directory).mkdir()
                (web / directory / "cache").write_text("cache")
            (web / "next-env.d.ts").write_text("generated")
            (web / "tsconfig.tsbuildinfo").write_text("generated")
            self.assertFalse(sr.web_build_required(web))
            world = World()
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual(len(world.commands), 1)

    def test_skip_flag_dry_run_and_llama_never_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            for args in (("--skip-web-build",), ("--dry-run",), ("--llama",)):
                world = World()
                self.assertEqual(world.invoke(*args, web_dir=web), sr.EXIT_OK)
                self.assertTrue(all(cmd[0] == "systemctl" for cmd in world.commands))

    def test_removing_root_config_requires_build_without_directory_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp, fresh=True)
            config = web / "next.config.ts"
            config.write_text("export default {}\n")
            os.utime(config, (100, 100))
            stamp = web / "out" / sr.WEB_BUILD_STAMP
            stamp.write_text(json.dumps(["next.config.ts", "src/page.tsx"]))
            os.utime(stamp, (200, 200))
            self.assertFalse(sr.web_build_required(web))
            config.unlink()
            self.assertTrue(sr.web_build_required(web))

    def test_source_newer_than_completion_marker_requires_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp, fresh=True)
            os.utime(web / "src" / "page.tsx", (300, 300))
            self.assertTrue(sr.web_build_required(web))

    def test_source_edit_during_build_stays_newer_than_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp)
            world = World()
            original_run = world.run

            def run(cmd, timeout):
                if cmd[0] == "npm":
                    build_web = Path(cmd[2])
                    (build_web / "out").mkdir()
                    (build_web / "out" / "index.html").write_text("built")
                    source = web / "src" / "page.tsx"
                    source.write_text("export default function UpdatedPage() {}\n")
                    os.utime(source, (400, 400))
                    return 0
                return original_run(cmd, timeout)

            world.run = run
            with patch.object(sr.time, "time", return_value=300):
                self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual((web / "out" / sr.WEB_BUILD_STAMP).stat().st_mtime, 300)
            self.assertTrue(sr.web_build_required(web))

    def test_export_without_source_manifest_is_rebuilt_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = self.make_web(tmp, fresh=True)
            (web / "out" / sr.WEB_BUILD_STAMP).unlink()
            self.assertTrue(sr.web_build_required(web))


class SmokeAuthModeTests(unittest.TestCase):
    def smoke(self, *, mode=None, status=200, body=None):
        # No network and no model calls: every curl invocation is a local fake.
        fake = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
path = url.split("://", 1)[1].partition("/")[2]
code, content_type, redirect = "200", "application/json", ""
headers = ""
if path == "v1/chat/completions":
    body = '{"choices":[{"message":{"content":"OK"}}]}'
elif path == "v1/audio/speech":
    body = "x" * 1200
elif path == "sdapi/v1/sd-models":
    body = "[]"
elif path == "":
    content_type, body = "text/html", '<script src="/_next/static/a123456789abcdef.js"></script>'
elif path == "library":
    code, redirect, body = "308", "/library/", ""
elif path == "showcase/":
    content_type, body = "text/html", "<title>Azizam: your own AI gateway</title>"
elif path.startswith("_next/static/"):
    headers, body = "HTTP/1.1 200 OK\nCache-Control: public, immutable\n", ""
elif path == "v1/models":
    if "-I" in args:
        code, body = "405", ""
    else:
        code, body = os.environ["FAKE_MODELS_STATUS"], os.environ["FAKE_MODELS_BODY"]
elif path == "mcp":
    headers, body = "HTTP/1.1 405 Method Not Allowed\nAllow: POST\n", ""
elif path == "ui/home/":
    code, content_type, body = "404", "text/html", "<html>not found</html>"
else:
    raise AssertionError(url)
if "-D" in args:
    sys.stdout.write(headers)
if "-o" in args:
    pathlib.Path(args[args.index("-o") + 1]).write_text(body)
else:
    sys.stdout.write(body)
if "-w" in args:
    fmt = args[args.index("-w") + 1].replace("\\n", "\n")
    sys.stdout.write(fmt.replace("%{http_code}", code).replace("%{content_type}", content_type).replace("%{redirect_url}", redirect))
"""
        with tempfile.TemporaryDirectory() as tmp:
            curl = Path(tmp) / "curl"
            curl.write_text(fake)
            curl.chmod(0o755)
            env = {**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"],
                   "FAKE_MODELS_STATUS": str(status),
                   "FAKE_MODELS_BODY": body if body is not None else json.dumps(
                       {"object": "list", "data": [{"id": "fast"}, {"id": "default"}]})}
            env.pop("AZIZAM_SMOKE_AUTH_MODE", None)
            env.pop("AZIZAM_SMOKE_KEY", None)
            if mode is not None:
                env["AZIZAM_SMOKE_AUTH_MODE"] = mode
            return subprocess.run(["bash", str(ROOT / "scripts" / "post-deploy-smoke")],
                                  env=env, capture_output=True, text=True, timeout=10)

    def test_default_lan_bypass_requires_json_model_list(self):
        result = self.smoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS models without key (bypass)", result.stdout)

    def test_explicit_required_auth_checks_401_json_error(self):
        result = self.smoke(mode="required", status=401, body='{"error":{"code":"auth_required"}}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS models without key (required)", result.stdout)

    def test_mode_mismatch_or_malformed_json_fails(self):
        for kwargs in ({"mode": "required"}, {"status": 401, "body": '{"error":{}}'},
                       {"body": "not json"}, {"body": '{"object":"list","data":[]}'},
                       {"mode": "required", "status": 401, "body": "{}"}):
            with self.subTest(kwargs=kwargs):
                result = self.smoke(**kwargs)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL models without key", result.stdout)

    def test_unknown_auth_mode_is_usage_error(self):
        result = self.smoke(mode="auto")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
