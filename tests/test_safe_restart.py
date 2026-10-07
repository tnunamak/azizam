"""scripts/safe-restart logic with systemctl and HTTP mocked."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
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
        self.restarted = False
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
        self.restarted = True
        if self.systemctl_code == 0:
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
        self.assertEqual(code, sr.EXIT_OK, world.text)
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


class WebBuildTests(unittest.TestCase):
    def make_web(self, root, *, fresh=False):
        web = Path(root) / "web"
        (web / "src").mkdir(parents=True)
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
                    self.assertEqual(cmd, ["npm", "--prefix", str(web), "run", "build"])
                    world.commands.append(list(cmd))
                    return 0
                return original_run(cmd, timeout)

            world.run = run
            self.assertEqual(world.invoke(web_dir=web), sr.EXIT_OK)
            self.assertEqual([cmd[0] for cmd in world.commands], ["npm", "systemctl"])
            self.assertFalse(sr.web_build_required(web))

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


if __name__ == "__main__":
    unittest.main()
