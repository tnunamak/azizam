"""Owner-operated long-audio trials. No saved setting changes until configure/apply.

Jobs retain only progress, aggregate scores and a configuration fingerprint in SQLite.
Audio is downloaded on demand into a private job directory and removed on completion.
The injected transcriber calls the gateway route, retaining its policy and scheduler.
"""
from __future__ import annotations

import asyncio
import contextvars
import contextlib
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
import uuid
import wave

import yaml

from gateway.resource_claims import AdmissionRequest, CapacitySnapshot, QueueClass, ResourceClaim, evaluate_admission
from gateway import stt_chunking, stt_measure as measure, stt_samples
from gateway.engine_admin import EngineRouteError, EngineService

POLICY = Path(__file__).resolve().parent.parent / "registry/stt_measurement.yml"
SETTING_KEYS = frozenset({"target_seconds", "search_seconds", "max_segment_seconds", "native_long_audio"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
# This context is created only by the authenticated operator worker, never by an HTTP header.
TRIAL: contextvars.ContextVar[dict | None] = contextvars.ContextVar("stt_measurement_trial", default=None)


def trial_config(service_id: str, config: Mapping[str, Any]) -> Mapping[str, Any]:
    trial = TRIAL.get()
    if trial is None:
        return config
    if service_id != trial["service_id"]:
        raise stt_chunking.SttConfigurationError("A measurement cannot fall back to another service")
    return {**config, "chunking": {**(config.get("chunking") or {}), **trial["settings"]}}


def observe_outcome(outcome: stt_chunking.ChunkingOutcome) -> None:
    trial = TRIAL.get()
    if trial is not None:
        trial["outcome"] = {"mode": outcome.mode, "attempts": outcome.attempts_started}


def observe_reply(status: int, mode: str) -> None:
    trial = TRIAL.get()
    if trial is not None and mode in {"direct", "upstream_error", "stream_refused"}:
        trial["upstream_status"] = status


def gpu_admission(record: Mapping[str, Any], gpus: list[dict] | None, queue_depth: int | None, policy: dict, *, resident_mb: Mapping[int, int] | None = None) -> dict:
    """Conservative admission from declared demand, measured headroom and the shared media queue.

    Honor the service record's GPU device indices. Without a binding, require
    headroom on every reported GPU. This is advice, not an exclusive reservation.
    """
    resources = record.get("resources") or (record.get("config") or {}).get("resources") or {}
    required = resources.get("gpu_required")
    if not isinstance(required, bool):
        raise EngineRouteError(400, "measurement_resources_required", "Declare resources.gpu_required on this engine before measuring it.")
    if not required:
        return {"admitted": True, "reason": "CPU declared; normal gateway admission still applies.", "gpu_required": False}
    estimate = resources.get("vram_gb_estimate")
    if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or not math.isfinite(estimate) or estimate <= 0:
        raise EngineRouteError(400, "measurement_resources_required", "Declare a positive resources.vram_gb_estimate before measuring a GPU engine.")
    reason = "Waiting for current GPU and shared media queue facts."
    if not gpus or queue_depth is None:
        return {"admitted": False, "reason": reason, "gpu_required": True}
    gpu_config = resources.get("gpu") or {}
    devices = gpu_config.get("devices") if isinstance(gpu_config, Mapping) else None
    if devices is not None:
        if not isinstance(devices, list) or not devices or any(isinstance(d, bool) or not isinstance(d, int) or d < 0 for d in devices):
            raise EngineRouteError(400, "measurement_resources_required", "resources.gpu.devices must list non-negative GPU indices.")
        selected = [gpu for gpu in gpus if gpu.get("index") in devices]
        if {gpu.get("index") for gpu in selected} != set(devices):
            return {"admitted": False, "reason": "Waiting for facts for the engine's declared GPU devices.", "gpu_required": True}
        gpus = selected
    for gpu in gpus:
        total, used, utilization = (gpu.get(k) for k in ("memory_total_mb", "memory_used_mb", "instantaneous_util_pct"))
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 for v in (total, used, utilization)):
            return {"admitted": False, "reason": reason, "gpu_required": True}
        if utilization > policy["max_gpu_utilization_pct"]:
            return {"admitted": False, "reason": "Waiting for GPU utilization to fall.", "gpu_required": True}
        resident = (resident_mb or {}).get(gpu.get("index"), 0)
        if isinstance(resident, bool) or not isinstance(resident, int) or not 0 <= resident <= used:
            resident = 0  # Unverified attribution earns no credit.
        # The estimate is total engine demand, not additional demand on top of
        # its already-resident allocation. Other services never earn this credit.
        additional = max(0, math.ceil(estimate * 1024) - resident)
        claim = ResourceClaim(gpu_required=True, vram_mb=additional + policy["gpu_min_free_mb"], queue_class=QueueClass.BACKGROUND)
        decision = evaluate_admission(AdmissionRequest("stt-measurement", claim), CapacitySnapshot(
            total_vram_mb=int(total), used_vram_mb=int(used), observed_at=time.time(), quality="measured",
            active_by_queue={QueueClass.BACKGROUND: queue_depth}, queue_limits={QueueClass.BACKGROUND: 1}))
        if not decision.accepted:
            return {"admitted": False, "reason": "Waiting for GPU memory headroom and the shared media queue to clear.", "gpu_required": True}
    return {"admitted": True, "reason": "Measured GPU headroom available; shared media queue empty.", "gpu_required": True}


def fingerprint(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def settings_patch(body: Any) -> dict:
    if not isinstance(body, dict) or not body or set(body) - (SETTING_KEYS | {"threshold_seconds"}):
        raise EngineRouteError(400, "invalid_stt_settings", "Use only target_seconds, threshold_seconds, search_seconds, max_segment_seconds and native_long_audio.")
    patch = dict(body)
    for key, value in patch.items():
        if key == "native_long_audio":
            if not isinstance(value, bool):
                raise EngineRouteError(400, "invalid_stt_settings", "native_long_audio must be a boolean.")
        elif isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or (value < 0 if key == "search_seconds" else value <= 0):
            raise EngineRouteError(400, "invalid_stt_settings", "Target and maximum must be positive; search width may be zero. All times must be finite seconds.")
    return patch


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


class Measurements:
    def __init__(self, store: Any, transcribe: Callable | None, *, policy_path: Path = POLICY, admit: Callable | None = None, preflight: Callable | None = None, active: Callable | None = None) -> None:
        self.store, self.transcribe, self.policy_path, self.admit, self.preflight, self.active = store, transcribe, policy_path, admit, preflight, active
        self.path = Path(store.path).with_name("stt_measurements.db")
        self.tasks: dict[str, asyncio.Task] = {}
        self.initialized = False
        self._start_lock = asyncio.Lock()
        self._lease_fd: int | None = None

    @contextlib.contextmanager
    def _workspace_lease(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "stt-measurement-work.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EngineRouteError(409, "measurement_busy", "Previous measurement preparation is still stopping. Try again shortly.") from None
            self._lease_fd = lock.fileno()
            try:
                yield
            finally:
                self._lease_fd = None
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _cleanup_abandoned_workspaces(self) -> None:
        if self._lease_fd is not None or any(not task.done() for task in self.tasks.values()):
            return
        try:
            with self._workspace_lease():
                scratch = self.path.parent / "stt-measurement-work"
                if scratch.is_dir():
                    for folder in scratch.iterdir():
                        if folder.is_dir() and not folder.is_symlink():
                            shutil.rmtree(folder)
        except EngineRouteError:
            pass  # A live worker inherited the lease; never remove its files.

    def startup_cleanup(self) -> None:
        """Remove abandoned preparation files before serving owner requests."""
        self._cleanup_abandoned_workspaces()

    @contextlib.contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_abandoned_workspaces()
        db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        db.execute("CREATE TABLE IF NOT EXISTS measurements (id TEXT PRIMARY KEY, service TEXT, payload TEXT NOT NULL)")
        if not self.initialized:
            for jid, raw in db.execute("SELECT id,payload FROM measurements").fetchall():
                job = json.loads(raw)
                if job["status"] not in TERMINAL:
                    job.update(status="interrupted", phase="Gateway restarted; run the measurement again.")
                    db.execute("UPDATE measurements SET payload=? WHERE id=?", (json.dumps(job), jid))
            db.commit()
            self.initialized = True
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _save(self, job: dict) -> None:
        job["updated_at"] = int(time.time())
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO measurements VALUES (?,?,?)", (job["id"], job["service_id"], json.dumps(_finite(job), allow_nan=False)))
            db.execute("DELETE FROM measurements WHERE id IN (SELECT id FROM measurements ORDER BY rowid DESC LIMIT -1 OFFSET 50)")

    def get(self, service_id: str, job_id: str | None = None) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM measurements WHERE service=? AND id=?", (service_id, job_id)).fetchone() if job_id else db.execute(
                "SELECT payload FROM measurements WHERE service=? ORDER BY rowid DESC LIMIT 1", (service_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, service_id: str) -> tuple[dict, int]:
        registry, generation = self.store.get_service_registry_snapshot()
        record = registry.get("services", {}).get(service_id)
        if record is None:
            raise EngineRouteError(404, "service_not_found", "No engine has that id.")
        if not {"transcriptions", "audio.transcriptions"}.intersection(record.get("capabilities", [])):
            raise EngineRouteError(400, "not_transcription_engine", "Choose an engine that offers transcription.")
        return record, generation

    def options(self) -> dict:
        policy = yaml.safe_load(self.policy_path.read_text())
        specs = stt_samples.load_manifest()
        return {"policy": policy, "samples": [{"id": s.id, "title": s.title, "speakers": s.speakers,
                "timing": s.timing, "download_bytes": s.download_bytes(), "licence": s.licence.name} for s in specs]}

    def configure(self, ctx: Any, service_id: str, body: Any, generation: int) -> dict:
        return EngineService(self.store).configure_stt(ctx, service_id, body, expected_generation=generation)

    async def start(self, ctx: Any, service_id: str, body: Any) -> dict:
        async with self._start_lock:
            if self.transcribe is None:
                raise EngineRouteError(503, "measurement_unavailable", "The measurement runner is unavailable.")
            if not isinstance(body, dict) or set(body) - {"sample_ids", "lengths", "search_widths", "model"}:
                raise EngineRouteError(400, "invalid_measurement", "Use sample_ids, lengths, search_widths and model only.")
            # One job at a time across all engines; normal inference still has admission priority.
            if any(not task.done() for task in self.tasks.values()):
                raise EngineRouteError(409, "measurement_busy", "A measurement is already running. Cancel it or wait.")
            record, generation = self.record(service_id)
            options = self.options()
            policy = options["policy"]
            ids = body.get("sample_ids", policy["sample_ids"])
            lengths = body.get("lengths", policy["lengths"])
            widths = body.get("search_widths", policy["search_widths"])
            if not isinstance(ids, list) or not ids or len(ids) > policy["max_samples"] or any(not isinstance(s, str) for s in ids) or len(set(ids)) != len(ids) or any(
                    not isinstance(s, str) or s not in {x["id"] for x in options["samples"]} for s in ids):
                raise EngineRouteError(400, "invalid_measurement", "Choose distinct sample ids from the manifest, within the sample limit.")
            for values, allow_zero in ((lengths, False), (widths, True)):
                if not isinstance(values, list) or not 2 <= len(values) <= 6 or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not (0 if allow_zero else policy["min_trial_seconds"]) <= v <= policy["max_trial_seconds"] for v in values):
                    raise EngineRouteError(400, "invalid_measurement", "Choose two to six finite trial values within the seconds limit; search widths may include zero.")
                if len(set(values)) < 2:
                    raise EngineRouteError(400, "invalid_measurement", "Choose at least two distinct lengths and search widths.")
            projected = len(ids) * (sum(int(policy["audio_seconds_per_sample"] // length) for length in set(lengths)) + 2 * len(set(widths)))
            if projected > policy["max_gateway_requests"]:
                raise EngineRouteError(400, "measurement_budget", "These trials exceed the request budget; choose fewer trial values.")
            if sum(x["download_bytes"] for x in options["samples"] if x["id"] in ids) > policy["max_download_bytes"]:
                raise EngineRouteError(400, "sample_budget", "These samples exceed the download budget; choose fewer samples.")
            model = body.get("model")
            if model is not None and (not isinstance(model, str) or not model or len(model) > 256):
                raise EngineRouteError(400, "invalid_measurement", "Choose a public transcription model name.")
            if self.preflight is not None:
                await self.preflight(ctx, service_id, model)
            self.tasks = {jid: task for jid, task in self.tasks.items() if not task.done()}
            job = {"id": uuid.uuid4().hex, "service_id": service_id, "status": "queued", "phase": "Queued",
                   "completed": 0, "total": 0, "created_at": int(time.time()), "fingerprint": fingerprint(record),
                   "registry_generation": generation, "sample_ids": ids, "lengths": sorted(set(lengths)),
                   "search_widths": sorted(set(widths)), "model": model, "report": None}
            if self.admit is not None:
                job["admission"] = await self.admit(ctx, service_id)
            self._save(job)
            self.tasks[job["id"]] = asyncio.create_task(self._run(ctx, job, policy))
            return job

    async def cancel(self, service_id: str, job_id: str) -> dict:
        job = self.get(service_id, job_id)
        if job is None:
            raise EngineRouteError(404, "measurement_not_found", "No measurement has that id for this engine.")
        task = self.tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # A queued task can be cancelled before its coroutine entered its try/finally.
            job = self.get(service_id, job_id)
            if job["status"] not in TERMINAL:
                job.update(status="cancelled", phase="Cancelled")
                self._save(job)
        return self.get(service_id, job_id)

    def apply(self, ctx: Any, service_id: str, job_id: str, generation: int) -> dict:
        job = self.get(service_id, job_id)
        if not job or job["status"] != "succeeded" or not job["report"].get("recommendation"):
            raise EngineRouteError(409, "no_recommendation", "Complete a measurement with enough evidence before applying settings.")
        record, _ = self.record(service_id)
        if fingerprint(record) != job["fingerprint"]:
            raise EngineRouteError(409, "measurement_stale", "The engine changed since this measurement. Run it again.")
        rec = job["report"]["recommendation"]
        return EngineService(self.store).configure_stt(ctx, service_id, {k: rec[k] for k in SETTING_KEYS if k in rec}, expected_generation=generation, apply_recommendation=True)

    async def _prepare(self, job: dict, spec: stt_samples.SampleSpec, workspace: Path, policy: dict) -> tuple[Path, list[measure.RefWord]]:
        # Separate process makes download, XML parsing and ffmpeg cancellable together.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "gateway.stt_measurement", "prepare", spec.id, str(workspace), str(policy["max_download_bytes"]), str(os.getpid()),
            pass_fds=(self._lease_fd,) if self._lease_fd is not None else (),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        last_phase, last_saved = None, 0.0
        try:
            while line := await process.stdout.readline():
                progress = json.loads(line)
                job.update(phase=progress["phase"], download_completed=progress["done"], download_total=progress["total"])
                now = time.monotonic()
                if progress["phase"] != last_phase or now - last_saved >= 0.25:
                    self._save(job)
                    last_phase, last_saved = progress["phase"], now
                await asyncio.sleep(0)  # Buffered progress must not starve cancellation or other requests.
            if await process.wait() != 0:
                raise EngineRouteError(502, "sample_failed", "The public sample could not be fetched, verified or prepared.")
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The owned group exited between the returncode check and signal.
                finally:
                    await process.wait()
        words = [measure.RefWord(*w) for w in json.loads((workspace / "reference.json").read_text())]
        return workspace / "audio.wav", words

    async def _transcription(self, ctx: Any, job: dict, audio: Path, settings: dict) -> measure.Run:
        record, _ = await asyncio.to_thread(self.record, job["service_id"])
        if fingerprint(record) != job["fingerprint"]:
            raise EngineRouteError(409, "measurement_stale", "The engine changed during measurement. Run it again.")
        if self.admit is not None:
            while True:
                admission = await self.admit(ctx, job["service_id"])
                job.update(admission=admission, phase=admission["reason"])
                self._save(job)
                if admission["admitted"]:
                    break
                await asyncio.sleep(2)
        job.update(phase="Transcribing through gateway admission")
        self._save(job)
        trial = {"service_id": job["service_id"], "settings": settings, "outcome": None}
        token = TRIAL.set(trial)
        try:
            text = await self.transcribe(ctx, job["service_id"], job["model"], audio)
        finally:
            TRIAL.reset(token)
        with wave.open(str(audio), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if settings["enabled"]:
                pcm = audio.with_suffix(".pcm")
                pcm.write_bytes(wav.readframes(wav.getnframes()))
        segments = [(0.0, duration)]
        if settings["enabled"]:
            try:
                segments = stt_chunking.plan_quietest_frame_segments(duration_seconds=duration,
                    quietest_frame=stt_chunking.quietest_frame_reader(pcm), target_seconds=settings["target_seconds"],
                    search_seconds=settings["search_seconds"], max_segment_seconds=settings["max_segment_seconds"], max_segments=512)
            finally:
                pcm.unlink(missing_ok=True)
            outcome = trial["outcome"]
            if len(segments) > 1 and (not outcome or outcome["mode"] != "chunked" or outcome["attempts"] != len(segments)):
                raise EngineRouteError(409, "measurement_plan_mismatch", "The gateway did not execute the trial plan; no seam estimate can be reported.")
        job["completed"] += 1
        self._save(job)
        return measure.Run(measure.normalize(text), segments)

    async def _watch_owner_access(self, ctx: Any, task: asyncio.Task, revoked: asyncio.Event) -> None:
        while True:
            try:
                allowed = await asyncio.to_thread(self.active, ctx)
            except Exception:
                allowed = False
            if not allowed:
                revoked.set()
                task.cancel()
                return
            await asyncio.sleep(1)

    async def _run(self, ctx: Any, job: dict, policy: dict) -> None:
        revoked = asyncio.Event()
        watchdog = asyncio.create_task(self._watch_owner_access(ctx, asyncio.current_task(), revoked)) if self.active else None
        try:
            async with asyncio.timeout(policy["deadline_seconds"]):
                job.update(status="running", phase="Fetching public samples")
                self._save(job)
                scratch = self.path.parent / "stt-measurement-work"
                scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
                with self._workspace_lease():
                    with tempfile.TemporaryDirectory(dir=scratch, prefix="job-") as root:
                        report = await self._measure(ctx, job, policy, Path(root))
                job.update(status="succeeded", phase="Complete; review before applying", report=_finite(report))
        except asyncio.CancelledError:
            job.update(status="cancelled", phase="Owner access ended; measurement stopped." if revoked.is_set() else "Cancelled")
        except TimeoutError:
            job.update(status="failed", phase="Measurement deadline reached; use fewer trials or shorter samples.")
        except EngineRouteError as error:
            job.update(status="failed", phase=error.message, error_code=error.code)
        except Exception:
            # No upstream text, exception detail, local path, transcript or credential in management output.
            job.update(status="failed", phase="Measurement failed. Check engine availability and try again.")
        finally:
            if watchdog is not None:
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
            self._save(job)

    async def _measure(self, ctx: Any, job: dict, policy: dict, root: Path) -> dict:
        specs = {s.id: s for s in stt_samples.load_manifest()}
        selected = [specs[sid] for sid in job["sample_ids"]]
        total_bytes = sum(s.download_bytes() for s in selected)
        if total_bytes > policy["max_download_bytes"]:
            raise EngineRouteError(400, "sample_budget", "These samples exceed the download budget; choose fewer samples.")
        length_pieces = {length: [] for length in job["lengths"]}
        search_pairs = {width: ([], []) for width in job["search_widths"]}
        failed_above = None
        metadata, prepared = [], []
        for spec in selected:
            folder = root / spec.id
            folder.mkdir()
            audio, reference = await self._prepare(job, spec, folder, policy)
            metadata.append({"id": spec.id, "speakers": spec.speakers, "timing": spec.timing, "licence": spec.licence.name})
            prepared.append((folder, audio, reference))
        # Finish the length sweep on all samples before selecting one seam trial plan.
        failed_lengths = set()
        sample_deletions = []
        for folder, audio, reference in prepared:
            with wave.open(str(audio), "rb") as source:
                rate = source.getframerate()
                seconds = min(source.getnframes() / rate, policy["audio_seconds_per_sample"])
                job["total"] += sum(int(seconds // length) for length in job["lengths"]) + 2 * len(job["search_widths"])
                deletions = {length: {} for length in job["lengths"]}
                sample_deletions.append((reference, deletions))
                for length in job["lengths"]:
                    if failed_above is not None and length >= failed_above:
                        continue
                    for index in range(int(seconds // length)):
                        start, end = index * length, (index + 1) * length
                        piece = folder / "piece.wav"
                        _wave_slice(source, piece, start, end)
                        try:
                            run = await self._transcription(ctx, job, piece, {"enabled": False, "native_long_audio": True,
                                "max_segment_seconds": length, "target_seconds": length, "threshold_seconds": length + 1})
                        except EngineRouteError as error:
                            # Admission/cancellation are not evidence that a length is unsafe.
                            if error.code != "measurement_backend_refused":
                                raise
                            failed_above = min(length, failed_above) if failed_above is not None else length
                            failed_lengths.add(length)
                            break
                        words = [measure.RefWord(w.word, w.start - start, w.end - start) for w in reference if start <= w.mid < end]
                        scored = measure.score_run(words, run)
                        length_pieces[length] += measure.piece_counts(scored)
                        indexes = [i for i, w in enumerate(reference) if start <= w.mid < end]
                        deletions[length].update({indexes[i]: deleted for i, deleted in measure.interior_deletions(scored).items()})
        rows = [{"length": length, "failed": length in failed_lengths,
                 **measure.rate_interval(pieces, resamples=policy["resamples"])} for length, pieces in length_pieces.items()]
        recommendation_policy = policy["recommendation"]
        successful = [r for r in rows if not r["failed"] and r["pieces"] > 0
                      and (failed_above is None or r["length"] < failed_above)]
        baseline = min((r["length"] for r in successful), default=None)
        for row in rows:
            blocks = []
            if baseline is not None:
                for reference, deletions in sample_deletions:
                    blocks.extend(measure.deletion_blocks(deletions[baseline], deletions[row["length"]],
                        [w.mid for w in reference], block=recommendation_policy["length_block_seconds"]))
            row.update(baseline_length=baseline, paired_deletion=measure.paired_excess(blocks, resamples=policy["resamples"]).as_dict())
            row.update(measure.length_evidence(row, recommendation_policy))
        safe_rows = [r for r in rows if not r["failed"] and (failed_above is None or r["length"] < failed_above)]
        length_choice = measure.select_length(safe_rows, policy=recommendation_policy, failed_above=failed_above)
        if length_choice:
            maximum, _reasons = length_choice
            target = measure.target_for_maximum(maximum, recommendation_policy)
            # These are requested experimental settings, not values to narrow silently.
            search_pairs = {width: ([], []) for width in job["search_widths"]}
            for folder, audio, reference in prepared:
                for width in job["search_widths"]:
                    settings = {"enabled": True, "native_long_audio": False, "threshold_seconds": maximum,
                        "target_seconds": target, "max_segment_seconds": maximum, "search_seconds": width,
                        "max_segments": 512}
                    runs = []
                    with wave.open(str(audio), "rb") as source:
                        seconds = min(source.getnframes() / source.getframerate(), policy["audio_seconds_per_sample"])
                        for offset in (0.0, target / 2):
                            piece = folder / "seams.wav"
                            _wave_slice(source, piece, offset, seconds)
                            run = await self._transcription(ctx, job, piece, settings)
                            run = measure.Run(run.words, run.segments, offset)
                            runs.append(measure.score_run([w for w in reference if w.mid < seconds], run))
                    pairs, placebo = measure.seam_pairs(*runs, placebo_offset=target / 4)
                    search_pairs[width][0].extend(pairs)
                    search_pairs[width][1].extend(placebo)
        searches = [{"search": width, **measure.paired_excess(pairs, resamples=policy["resamples"]).as_dict(),
                     "noise": measure.paired_excess(noise, resamples=policy["resamples"]).as_dict(),
                     "difference": measure.difference(pairs, noise, resamples=policy["resamples"]).as_dict()} for width, (pairs, noise) in search_pairs.items()]
        for row in searches:
            row.update(measure.seam_evidence(row, recommendation_policy))
        record, _ = self.record(job["service_id"])
        current_search = stt_chunking.SttExecutionConfig.from_service(service_id=job["service_id"],
            service_config=record.get("config") or {}, config_directory=self.path.parent).search_seconds
        recommendation = measure.recommend(safe_rows, searches, policy=recommendation_policy,
                                           failed_above=failed_above, current_search=current_search)
        rec = ({"target_seconds": recommendation.target_seconds, "search_seconds": recommendation.search_seconds,
                "max_segment_seconds": recommendation.max_segment_seconds, "native_long_audio": False, "reasons": recommendation.reasons} if recommendation else None)
        state = "recommended" if rec else "inconclusive"
        evaluated = searches if length_choice else [r for r in rows if r["length"] != baseline]
        if not rec and evaluated and all(row["state"] == "confidently_worse" for row in evaluated):
            state = "confidently_worse"
        recommendation_note = ("Gross-harm screen only; this does not prove no harm. " +
            ("Paired length and seam-minus-noise upper CIs meet the owner margins; review before applying." if rec else
             "No supported recommendation. Inspect per-length and per-width states, reasons, counts and paired intervals."))
        return {"deletions_by_length": rows, "seam_excess": searches, "recommendation": rec,
                "recommendation_state": state, "recommendation_note": recommendation_note,
                "recommendation_policy": dict(recommendation_policy), "samples": metadata,
                "note": "Human transcripts; paired length differences use common interior reference words against the shortest successful length, bootstrapped in recording-local time blocks. Seam-minus-noise uses a difference bootstrap. Search widths are tested as requested; the current width is preferred when supported intervals overlap. Word timing may be interpolated. No settings were applied."}



def _wave_slice(source: wave.Wave_read, destination: Path, start: float, end: float) -> None:
    source.setpos(int(start * source.getframerate()))
    with wave.open(str(destination), "wb") as output:
        output.setparams(source.getparams())
        output.writeframes(source.readframes(int((end - start) * source.getframerate())))


def _arm_parent_death(expected_parent: int) -> None:
    """Linux worker owns this session/group; parent death terminates it and ffmpeg."""
    import ctypes
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Sample preparation requires its own process group")
    def terminate_group(_signum=None, _frame=None):
        os.killpg(os.getpid(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, terminate_group)
    # Set before downloading or spawning ffmpeg. The PPID check closes the
    # race where the gateway died before the kernel notification was armed.
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise RuntimeError("Cannot arm sample worker parent-death notification")
    if os.getppid() != expected_parent:
        terminate_group()


def _prepare_main() -> None:
    _, sample_id, root, max_bytes, parent_pid = sys.argv[1:]
    _arm_parent_death(int(parent_pid))
    folder = Path(root)
    spec = next(s for s in stt_samples.load_manifest() if s.id == sample_id)
    def progress(done, total, phase):
        print(json.dumps({"done": done, "total": total, "phase": phase}), flush=True)
    sample = stt_samples.fetch_sample(spec, folder / "sources", max_bytes=int(max_bytes), progress=progress)
    os.replace(sample.audio_path, folder / "audio.wav")
    (folder / "reference.json").write_text(json.dumps(sample.words))


if __name__ == "__main__":
    _prepare_main()
