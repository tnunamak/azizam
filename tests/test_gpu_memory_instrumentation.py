"""GPU memory instrumentation (GPU strategy step 0).

Covers: per-service VRAM reaching metrics.db, attribution derived from the
config's backends (no fixed engine-name list), and the NVKMS desktop
allocation-failure metric next to GPU free memory.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import firehose_render  # noqa: E402
import metrics_sampler  # noqa: E402
import metrics_store  # noqa: E402
import status_endpoint  # noqa: E402

U0, U1 = "GPU-uuid-0", "GPU-uuid-1"
CG = "/user.slice/user@1000.service/app.slice"

CONFIG = {
    "backends": {
        "kobold": {
            "host": "127.0.0.1", "port": 5051, "vram_budget_key": "GPU1_LLM",
            "lifecycle": {"driver": "systemd", "driver_config": {"scope": "user", "unit": "llama-bee.service"}},
        },
        "tts-supertonic": {
            "host": "127.0.0.1", "port": 8010,
            "lifecycle": {"driver": "systemd", "driver_config": {"scope": "user", "unit": "supertonic-tts.service"}},
        },
        # No lifecycle unit: found by its listening port.
        "comfyui": {"host": "127.0.0.1", "port": 7890, "vram_budget_key": "GPU0_COMFYUI"},
        # Remote host: its port number must never claim a local process.
        "remote": {"host": "10.0.0.5", "port": 9999},
    }
}

GPUS = [
    {"index": 0, "uuid": U0, "memory_used_mb": 16000, "memory_total_mb": 24576},
    {"index": 1, "uuid": U1, "memory_used_mb": 22865, "memory_total_mb": 24576},
]
APPS = [
    {"pid": 100, "gpu_uuid": U0, "used_mb": 11886, "process_name": "python"},
    {"pid": 200, "gpu_uuid": U0, "used_mb": 338, "process_name": "audiocpp_server"},
    {"pid": 400, "gpu_uuid": U0, "used_mb": 262, "process_name": "sunshine"},
    {"pid": 300, "gpu_uuid": U1, "used_mb": 22854, "process_name": "llama-server"},
]
BUDGET = {"raw": {"GPU0_COMFYUI": 15, "GPU1_LLM": 24, "GPU0_DESKTOP": 3}}


class FakeHost:
    """Fake cgroup tree plus `systemctl show` / `ss` output for the attribution probes."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp
        self.units = {
            "llama-bee.service": [300],
            "supertonic-tts.service": [200, 201],
            "comfyui.service": [100, 101],
        }
        for unit, pids in self.units.items():
            d = tmp / (CG + "/" + unit).lstrip("/")
            d.mkdir(parents=True)
            (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))

    def run(self, cmd, timeout=None):
        if cmd[0] == "systemctl":
            blocks = []
            for unit in [a for a in cmd if a.endswith(".service")]:
                blocks.append(f"ControlGroup={CG}/{unit}" if unit in self.units else "ControlGroup=")
            return 0, "\n\n".join(blocks) + "\n", ""
        if cmd[0] == "ss":
            return 0, (
                'LISTEN 0 512 127.0.0.1:5051 0.0.0.0:* users:(("llama-server",pid=300,fd=12))\n'
                'LISTEN 0 128 0.0.0.0:7890 0.0.0.0:* users:(("python",pid=100,fd=53))\n'
                'LISTEN 0 128 0.0.0.0:9999 0.0.0.0:* users:(("sunshine",pid=400,fd=3))\n'
            ), ""
        return 1, "", "unexpected"

    def proc_service_cgroup(self, pid):
        return {100: f"{CG}/comfyui.service"}.get(pid)


class TestAttributionFromConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        host = FakeHost(Path(self.tmp.name))
        self.patches = [
            patch.object(status_endpoint, "_CGROUP_ROOT", Path(self.tmp.name)),
            patch.object(status_endpoint, "_run", host.run),
            patch.object(status_endpoint, "_proc_service_cgroup", host.proc_service_cgroup),
            patch.object(status_endpoint, "_get_compute_apps", return_value=APPS),
            patch.object(status_endpoint, "_read_gpu_budget", return_value=BUDGET),
            patch.object(status_endpoint, "_read_proc_comm", return_value="proc"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def rows(self, gpu: int) -> dict[str, dict]:
        attr = status_endpoint._vram_attribution(GPUS, CONFIG)
        entry = next(g for g in attr["per_gpu"] if g["index"] == gpu)
        return {r["service"]: r for r in entry["services"]}

    def test_services_are_the_config_backends_not_a_fixed_list(self) -> None:
        gpu0, gpu1 = self.rows(0), self.rows(1)
        self.assertEqual(gpu0["comfyui"]["used_mb"], 11886)  # found by port 7890
        self.assertEqual(gpu0["tts-supertonic"]["used_mb"], 338)  # found by unit cgroup
        self.assertEqual(gpu1["kobold"]["used_mb"], 22854)
        self.assertNotIn("llama-turbo", gpu1)

    def test_unowned_process_stays_in_desktop_bucket(self) -> None:
        # pid 400 listens on 9999 but backend "remote" is a remote host: not claimed.
        self.assertEqual(self.rows(0)["desktop"]["used_mb"], 262)

    def test_unattributed_is_device_used_minus_process_rows(self) -> None:
        self.assertEqual(self.rows(0)["__unattributed"]["used_mb"], 16000 - (11886 + 338 + 262))
        self.assertEqual(self.rows(1)["__unattributed"]["used_mb"], 22865 - 22854)

    def test_budget_comes_from_backend_data(self) -> None:
        self.assertEqual(self.rows(0)["comfyui"]["budget_gb"], 15)
        self.assertEqual(self.rows(1)["kobold"]["budget_gb"], 24)
        self.assertIsNone(self.rows(0)["tts-supertonic"]["budget_gb"])
        self.assertEqual(self.rows(0)["desktop"]["budget_gb"], 3)

    def test_a_two_gpu_pid_is_compared_with_its_budget_on_the_primary_gpu_only(self) -> None:
        """Red-team-3 A2: llama-server (pid 300) holds 20 GiB on GPU 1 and 8 GiB
        on GPU 0. GPU1_LLM (24 GiB) is a GPU 1 budget: the GPU 1 row is
        compared with it using the GPU 1 bytes only; the GPU 0 row is reported
        as spillover with no budget verdict. 28 GiB in total is not flagged."""
        split_apps = [app for app in APPS if app["pid"] != 300] + [
            {"pid": 300, "gpu_uuid": U1, "used_mb": 20 * 1024, "process_name": "llama-server"},
            {"pid": 300, "gpu_uuid": U0, "used_mb": 8 * 1024, "process_name": "llama-server"},
        ]
        with patch.object(status_endpoint, "_get_compute_apps", return_value=split_apps):
            primary, spill = self.rows(1)["kobold"], self.rows(0)["kobold"]
        self.assertEqual(
            (primary["used_mb"], primary["budget_gb"], primary["pct_of_budget"], primary["over_budget"], primary["spillover"]),
            (20 * 1024, 24, 83.3, False, False),
        )
        self.assertEqual(
            (spill["used_mb"], spill["budget_gb"], spill["pct_of_budget"], spill["over_budget"], spill["spillover"]),
            (8 * 1024, None, None, False, True),
        )
        self.assertEqual(spill["spillover_from_gpu"], 1)
        label = firehose_render._vram_row_label(spill)
        self.assertIn("spillover from GPU1", label)
        self.assertIn("no budget verdict", label)


class SamplerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self._orig_path = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)
        metrics_sampler.close_writer()
        self.writer = metrics_sampler.start_writer()
        self._reset_nvkms_state()

    def tearDown(self) -> None:
        metrics_sampler.close_writer()
        metrics_store.DB_PATH = self._orig_path
        self._reset_nvkms_state()
        self.tmp.cleanup()

    @staticmethod
    def _reset_nvkms_state() -> None:
        """The cursor in memory belongs to one journal; each test has its own."""
        metrics_sampler._state["nvkms_cursor"] = None
        metrics_sampler._state["nvkms_journal_unreadable"] = False

    def samples(self, metric: str) -> list[tuple[dict, float]]:
        self.writer.flush()
        with metrics_store.reader(self.db) as c:
            rows = c.execute("SELECT tags, value FROM samples WHERE metric=? ORDER BY ts, rowid", (metric,)).fetchall()
        return [(json.loads(t) if t else {}, v) for t, v in rows]


class TestServiceVramSampled(SamplerCase):
    def test_sampler_reads_the_services_key_that_vram_attribution_returns(self) -> None:
        attribution = {"per_gpu": [{"index": 0, "services": [
            {"service": "comfyui", "used_mb": 11886},
            {"service": "__unattributed", "used_mb": 100},
        ]}]}
        with (
            patch.object(status_endpoint, "_gpus_raw", return_value=GPUS),
            patch.object(status_endpoint, "_vram_attribution", return_value=attribution),
            patch.object(status_endpoint, "_disk_partitions", return_value=[]),
            patch.object(status_endpoint, "_ram_attribution", return_value={}),
            patch.object(status_endpoint, "_journal_xids", return_value={}),
        ):
            metrics_sampler._emit_slow(self.writer, int(time.time()))
        self.assertEqual(self.samples("service_vram_mb"), [({"gpu": 0, "service": "comfyui"}, 11886.0)])

    def test_service_vram_mb_rows_carry_service_and_gpu_tags(self) -> None:
        with tempfile.TemporaryDirectory() as cg_root:
            host = FakeHost(Path(cg_root))
            with (
                patch.object(status_endpoint, "_CGROUP_ROOT", Path(cg_root)),
                patch.object(status_endpoint, "_run", host.run),
                patch.object(status_endpoint, "_proc_service_cgroup", host.proc_service_cgroup),
                patch.object(status_endpoint, "_get_compute_apps", return_value=APPS),
                patch.object(status_endpoint, "_read_gpu_budget", return_value=None),
                patch.object(status_endpoint, "_gpus_raw", return_value=GPUS),
                patch.object(status_endpoint, "_disk_partitions", return_value=[]),
                patch.object(status_endpoint, "_ram_attribution", return_value={}),
                patch.object(status_endpoint, "_journal_xids", return_value={}),
            ):
                metrics_sampler._emit_slow(self.writer, int(time.time()), CONFIG)
        got = {(t["gpu"], t["service"]): v for t, v in self.samples("service_vram_mb")}
        self.assertEqual(got[(0, "comfyui")], 11886.0)
        self.assertEqual(got[(0, "tts-supertonic")], 338.0)
        self.assertEqual(got[(0, "desktop")], 262.0)
        self.assertEqual(got[(1, "kobold")], 22854.0)
        self.assertFalse([k for k in got if k[1].startswith("__")])


FAIL = (
    "{ts} peregrine kernel: [drm:nv_drm_gem_alloc_nvkms_memory_ioctl [nvidia_drm]] "
    "*ERROR* [nvidia-drm] [GPU ID 0x00000100] Failed to allocate NVKMS memory for GEM object"
)
NOISE = "1791503490.100000 peregrine kernel: usb 9-2.2.1.4: USB disconnect, device number 86"


class FakeKernelJournal:
    """The kernel journal as journalctl shows it: entries with cursors.

    Answers ``--since @T`` (time window), ``--after-cursor=C`` (read on from a
    saved position) and ``-n 0`` (no entries, only the end position);
    ``--show-cursor`` adds the ``-- cursor:`` line journalctl prints.
    """

    EMPTY = f"s={'a' * 32};i=0;b={'b' * 32};m=0;t=0;x=0"  # the end of an empty journal

    def __init__(self) -> None:
        self.now = 1791503000.0
        self.entries: list[tuple[float, str, str]] = []  # (ts, line, cursor)
        self.readable = True

    def log(self, template: str, *, after_s: float = 0.0) -> None:
        self.now += after_s
        cursor = f"s={'a' * 32};i={len(self.entries) + 1:x};b={'b' * 32};m=1;t=1;x=1"
        self.entries.append((self.now, template.format(ts=f"{self.now:.6f}"), cursor))

    def run(self, cmd, timeout=None):
        if cmd[0] == "nvidia-smi":
            return 0, "0, 00000000:01:00.0\n1, 00000000:03:00.0\n", ""
        if cmd[0] != "journalctl":
            return 1, "", ""
        if not self.readable:
            return 1, "", "Failed to get journal access"
        args = list(cmd[1:])
        shown = list(self.entries)
        position = self.entries[-1][2] if self.entries else self.EMPTY
        for arg in args:
            if arg.startswith("--after-cursor="):
                saved = arg.removeprefix("--after-cursor=")
                cursors = [self.EMPTY] + [cursor for _ts, _line, cursor in self.entries]
                shown, position = self.entries[cursors.index(saved):], saved
        if "--since" in args:
            since = float(args[args.index("--since") + 1].lstrip("@"))
            shown = [entry for entry in shown if entry[0] >= since]
        if "-n" in args and args[args.index("-n") + 1] == "0":
            shown = []
        out = [line for _ts, line, _cursor in shown]
        if "--show-cursor" in args:
            out.append(f"-- cursor: {shown[-1][2] if shown else position}")
        return 0, "\n".join(out) + "\n", ""


class TestNvkmsFailureMetric(SamplerCase):
    def setUp(self) -> None:
        super().setUp()
        self.journal = FakeKernelJournal()

    def tick(self, *, after_s: float = 10.0, gpus=GPUS) -> None:
        self.journal.now += after_s
        with (
            patch.object(metrics_sampler.time, "time", lambda: self.journal.now),
            patch.object(status_endpoint, "_run", self.journal.run),
            patch.object(status_endpoint, "_gpus_raw", return_value=gpus),
            patch.object(status_endpoint, "_read_sensors", return_value={}),
            patch.object(status_endpoint, "_load_average", return_value=(0.0, 0.0, 0.0)),
            patch.object(status_endpoint, "_ram_attribution", return_value={}),
        ):
            metrics_sampler._emit_fast(self.writer, int(self.journal.now))

    def failures(self, gpu: int | None = 0) -> list[float]:
        tags = {} if gpu is None else {"gpu": gpu}
        return [v for t, v in self.samples("gpu_desktop_alloc_failures") if t == tags]

    def test_first_run_starts_at_the_journal_end_and_counts_no_history(self) -> None:
        self.journal.log(NOISE)
        self.journal.log(FAIL)  # before the deploy: history
        self.tick()
        self.journal.log(FAIL, after_s=1)
        self.tick()
        self.tick()
        self.assertEqual(self.failures(0), [0.0, 1.0, 0.0])
        self.assertEqual(self.failures(1), [0.0, 0.0, 0.0])

    def test_the_cursor_is_kept_next_to_metrics_db(self) -> None:
        self.tick()
        cursor_file = self.db.parent / "nvkms-journal.cursor"
        self.assertTrue(cursor_file.read_text().startswith("s="))

    def test_failures_during_a_gap_longer_than_the_window_count_on_recovery(self) -> None:
        self.tick()
        self.journal.readable = False
        self.tick()
        self.journal.log(FAIL, after_s=5)
        self.journal.log(FAIL, after_s=20)
        self.tick(after_s=300)  # still unreadable, five minutes later
        self.journal.readable = True
        self.tick()
        self.assertEqual(self.failures(0), [0.0, 2.0], "no rows while unreadable; both counted on recovery")

    def test_a_sampler_restart_resumes_from_the_saved_cursor(self) -> None:
        self.tick()
        self.journal.log(FAIL, after_s=1)
        importlib.reload(metrics_sampler)  # the gateway restarts: module state is gone
        self.tick(after_s=120)
        self.assertEqual(self.failures(0), [0.0, 1.0])

    def test_a_failed_cursor_save_does_not_count_the_same_failure_twice(self) -> None:
        self.tick()
        self.journal.log(FAIL, after_s=1)
        with patch.object(metrics_sampler.os, "replace", side_effect=OSError(28, "No space left on device")):
            self.tick()
            self.tick()
        self.assertEqual(self.failures(0), [0.0, 1.0, 0.0])

    def test_identical_repeated_lines_each_count_once(self) -> None:
        self.tick()
        self.journal.log(FAIL, after_s=1)
        self.journal.log(FAIL)  # same text, same microsecond
        self.tick()
        self.tick()
        self.assertEqual(self.failures(0), [0.0, 2.0, 0.0])

    def test_unreadable_journal_records_no_rows_keeps_the_cursor_and_warns_once(self) -> None:
        self.tick()
        cursor_file = self.db.parent / "nvkms-journal.cursor"
        saved = cursor_file.read_text()
        self.journal.readable = False
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.tick()
            self.tick()
        self.assertEqual(self.failures(0), [0.0])
        self.assertEqual(cursor_file.read_text(), saved)
        self.assertEqual(out.getvalue().count("kernel journal unreadable"), 1)
        self.assertEqual(len(self.samples("vram_used_mb")), 6)  # the rest of each tick still ran

    def test_failure_from_unknown_gpu_id_is_counted_without_a_gpu_tag(self) -> None:
        self.tick()
        self.journal.log(FAIL.replace("0x00000100", "0x00000900"), after_s=1)
        self.tick()
        self.assertEqual(self.failures(None), [1.0])


class TestGpuFreeMemory(SamplerCase):
    def test_vram_free_mb_is_recorded_per_gpu_each_tick(self) -> None:
        with (
            patch.object(status_endpoint, "_gpus_raw", return_value=GPUS),
            patch.object(status_endpoint, "_run", lambda cmd, timeout=None: (1, "", "")),
            patch.object(status_endpoint, "_read_sensors", return_value={}),
            patch.object(status_endpoint, "_load_average", return_value=(0.0, 0.0, 0.0)),
            patch.object(status_endpoint, "_ram_attribution", return_value={}),
        ):
            metrics_sampler._emit_fast(self.writer, int(time.time()))
        got = {t["gpu"]: v for t, v in self.samples("vram_free_mb")}
        self.assertEqual(got, {0: 24576.0 - 16000.0, 1: 24576.0 - 22865.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
