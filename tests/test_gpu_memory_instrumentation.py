"""GPU memory instrumentation (GPU strategy step 0).

Covers: per-service VRAM reaching metrics.db, attribution derived from the
config's backends (no fixed engine-name list), and the NVKMS desktop
allocation-failure metric next to GPU free memory.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


class SamplerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self._orig_path = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)
        metrics_sampler.close_writer()
        self.writer = metrics_sampler.start_writer()
        metrics_sampler._state["nvkms_seen"] = None

    def tearDown(self) -> None:
        metrics_sampler.close_writer()
        metrics_store.DB_PATH = self._orig_path
        self.tmp.cleanup()

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


class TestNvkmsFailureMetric(SamplerCase):
    def tick(self, journal_lines, *, journal_rc=0, gpus=GPUS) -> None:
        def run(cmd, timeout=None):
            if cmd[0] == "journalctl":
                return journal_rc, "\n".join(journal_lines) + "\n", ""
            if cmd[0] == "nvidia-smi":
                return 0, "0, 00000000:01:00.0\n1, 00000000:03:00.0\n", ""
            return 1, "", ""

        with (
            patch.object(status_endpoint, "_run", run),
            patch.object(status_endpoint, "_gpus_raw", return_value=gpus),
            patch.object(status_endpoint, "_read_sensors", return_value={}),
            patch.object(status_endpoint, "_load_average", return_value=(0.0, 0.0, 0.0)),
            patch.object(status_endpoint, "_ram_attribution", return_value={}),
        ):
            metrics_sampler._emit_fast(self.writer, int(time.time()))

    def failures(self) -> list[tuple[dict, float]]:
        return self.samples("gpu_desktop_alloc_failures")

    def test_counts_only_new_failures_and_tags_the_gpu(self) -> None:
        old = FAIL.format(ts="1791503000.000001")
        new = FAIL.format(ts="1791503480.000002")
        self.tick([NOISE, old])  # baseline: history is not counted
        self.tick([NOISE, old, new])
        self.tick([NOISE, old, new])  # same window again: nothing new
        by_tick = [v for t, v in self.failures() if t == {"gpu": 0}]
        self.assertEqual(by_tick, [0.0, 1.0, 0.0])
        self.assertEqual([v for t, v in self.failures() if t == {"gpu": 1}], [0.0, 0.0, 0.0])

    def test_two_failures_in_the_same_microsecond_both_count(self) -> None:
        line = FAIL.format(ts="1791503480.000002")
        self.tick([])
        self.tick([line, line])
        self.assertEqual([v for t, v in self.failures() if t == {"gpu": 0}], [0.0, 2.0])

    def test_unreadable_journal_records_no_failure_rows_and_does_not_raise(self) -> None:
        self.tick([], journal_rc=1)
        self.tick([FAIL.format(ts="1791503480.000002")], journal_rc=1)
        self.assertEqual(self.failures(), [])
        self.assertEqual(len(self.samples("vram_used_mb")), 4)  # rest of the tick still ran

    def test_failure_from_unknown_gpu_id_is_counted_without_a_gpu_tag(self) -> None:
        self.tick([])
        self.tick([FAIL.format(ts="1791503480.000002").replace("0x00000100", "0x00000900")])
        self.assertIn(({}, 1.0), self.failures())


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
