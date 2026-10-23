# Status page stalls the event loop (2026-10-23)

## What the journal showed

`journalctl --user -u azizam --since "-24h"` has seven `[loop-watchdog] event loop blocked` events,
1.3 to 3.5 s each. Six happened between 11:27 and 11:32. Each has the loop thread inside
`status_endpoint._lifecycle_probe` → `lifecycle_drivers.py` `SystemdDriver.status` →
`asyncio/subprocess.py` `create_subprocess_exec`. The seventh (08:46, 3.48 s) is in prisma's engine
spawn during its first import. That is another path.

## Cause

Production runs uvicorn's default loop, which is uvloop (0.21.0 in the venv). On uvloop,
`asyncio.create_subprocess_exec` starts the child through libuv. The cost grows with resident
memory, as a fork's does, and the loop thread is blocked for all of it.

Measured on this host, with a bare process and a `/usr/bin/true` child (median of 15 spawns):

| Resident memory | Spawn on the loop (median) | Loop blocked (max) |
|---|---|---|
| 0 MB | 2.6 ms | 2.7 ms |
| 600 MB | 32.5 ms | 33.6 ms |
| 2 GB | 92.6 ms | 97 ms |

The stdlib `subprocess` module uses a vfork, which copies no page tables. Run in a worker thread,
the spawn keeps the fork away from the loop.

## Measured after the change

Two measurements, both run on this host (1-minute load 23 to 26 on 24 cores).

### Each spawn site, and the status build

`scripts/perf/spawn_lag.py` runs each site under uvloop at 600 MB of touched memory (resident
622 to 769 MB). Each site is the real gateway function with a stand-in binary on PATH, so the spawn
is real. "Loop lag" is the largest wake-up delay of a 2 ms probe during one call, median of 8 calls.

| Site | Loop lag before | Loop lag after | Call before | Call after |
|---|---|---|---|---|
| `_run_systemctl` (swap path) | 37.5 ms | 1.0 ms | 38.2 ms | 1.6 ms |
| `speech_stream.transcode_pcm_stream` (ffmpeg per reply) | 37.5 ms | 1.0 ms | 41.0 ms | 4.9 ms |
| `stt_chunking.run_media_command` (per STT chunk) | 39.5 ms | 1.0 ms | 41.9 ms | 1.6 ms |
| `stt_measurement._prepare` (sample process) | 39.5 ms | 1.0 ms | 42.9 ms | 4.7 ms |
| `proxy._run_workflow_drift_once` (drift refresh) | 37.0 ms | 1.0 ms | 46.2 ms | 10.1 ms |
| `/health` (credential count read) | 1.0 ms | 1.0 ms | 1.2 ms | 1.4 ms |
| Status snapshot build (new; background) | not measured | 14.5 ms (worst 35 ms) | not measured | 591 ms |

"Before" is commit `a4415f3ae`, the parent of the spawn change. "After" is the branch head.
The `/health` row shows no difference at idle: its SQLite read takes about 1 ms. Moving it off the
loop removes a block that every request makes, but this probe cannot show a difference at idle.

The snapshot row is the background build's own cost, measured in-process against a seeded admin
database. It runs every 15 s while something reads the snapshot, and not at all after the five-minute idle
window. The figure above is from before that change.

### The cold reads, against the ordinary budget

`scripts/perf/api_bench.py` runs each cold row three times after an idle wait, with a `/health` probe
beside it. Both gateways were served from the same seeded data and measured in the same window. "Before" is
commit `16155ad39`, the commit before the snapshot change (the spawn change is in it). "After" is the
branch head. The rows are judged against the defaults in
`scripts/perf/budgets.toml` `[api]`: p95 100 ms, 100 statements, 20 connections, 0 processes. The loop
limit is `[loop] max_stall_ms = 100`. p95 is the slowest of the three reads, as `api_bench` computes it.

| Row | Before (p95, statements, connections, processes, stall) | After | Limit |
|---|---|---|---|
| `/me/api/home (after idle)` | 150.7 ms, 84, 10, 1, 54.4 ms | 74.2 ms, 57, 5, 0, 34.3 ms | 100 ms, 100, 20, 0, 100 ms |
| `/admin/api/status (after idle)` | 960.5 ms, 717, 71, 26, 158.9 ms | 15.7 ms, 27, 3, 0, 1.5 ms | 100 ms, 100, 20, 0, 100 ms |

Before, the owner status row broke every limit and Home broke two (p95 and processes). After, both
rows meet every limit. The exceptions that `[api.routes]` carried for these two rows are gone, and
the table is empty. `test_perf_harness.py` fails if a route is added to it.

## Change

1. **Child processes start off the loop** (`gateway/off_loop_spawn.py`). `start()` runs
   `subprocess.Popen` on a small worker pool. The child's pipes stay on the loop, as asyncio's own
   pipe transports do, so streaming order is unchanged. One daemon thread per child waits for its exit,
   as the stdlib's `ThreadedChildWatcher` does. `run()` is `subprocess.run` on a daemon thread, so a
   run still going at shutdown does not hold the process open. A `start()` cancelled while its spawn
   is still running kills the child that spawn makes.
2. **The five spawn sites use the helper:** `SystemdDriver._run_systemctl` (swap path),
   `speech_stream.transcode_pcm_stream`, `stt_chunking.run_media_command`,
   `stt_measurement.Measurements._prepare`, and `proxy._run_workflow_drift_once`. Exit codes,
   timeouts, kills and the log lines are unchanged.
3. **`/health` reads the app credential count on a worker thread** (`proxy.health`).
4. **The status page and Home read one snapshot, refreshed only while it is read** (`status_endpoint.py`).
   A read keeps the snapshot wanted for `SNAPSHOT_IDLE_WINDOW_S` (300 s). While it is wanted, one full build
   runs every `SNAPSHOT_INTERVAL_S` (15 s), with each section on its own budget. When the window passes with
   no read, the refresher stops. The next read starts it again and gets the stored snapshot at once, with its
   age. Before the first build finishes, that read gets a `checking` answer. `snapshot.section_age_s` gives
   the age of each section, and `snapshot.age_s` gives the age of the whole build. A failed build keeps the
   last snapshot and logs the error type only. `/admin/api/status`, `/me/api/home`, `/v1/status` and the
   owner's HTML status page all read it. `_engine_rows_payload`, the Home refresh tasks and their cache are
   removed.
5. **Availability trusts backend rows only while the backend section is fresh.** `latest_payload()` returns `{}`
   when the backend section's last answer is older than `SNAPSHOT_FRESH_S` (60 s), or when it has never answered.
   The build's own time does not count: a section that times out keeps its last answer inside a fresh build. The
   capability and model-catalog answers then probe the services they need, with the 12 s row cache. The availability
   summary that each build stores (Home and the admin status read it) uses the same rule. It leaves out a stale
   section's rows, so its services read as unknown until the section answers again. The status page still shows the
   stale rows, with their age. Without this rule, the first read after a long idle would trust rows that were hours old.
   **Shutdown cancels and joins both background tasks.** The lifespan calls `status_endpoint.stop_snapshot()`
   where it used to call `_stop_home_refreshes()`, and `stop_backend_samples()` after it. After
   `stop_snapshot()`, no read starts a refresher.
6. **Earlier round, kept:** the systemd probes run in one batch per scope, each status section has a
   budget, `_backend_grid` gives each engine its own deadline, and a refused connection is classified
   only on `ECONNREFUSED`. The lifecycle batch is shielded, so one row that hits its deadline does not
   cancel the batch the other rows wait for.
7. **Backend history has its own light sampler** (`start_backend_samples()`). It probes each HTTP health URL
   once at startup and then every `BACKEND_SAMPLE_INTERVAL_S` (60 s), and writes the four `service_*` samples
   from those rows. It asks no lifecycle driver and starts no process. Shutdown cancels it and waits up to
   `BACKEND_SAMPLE_JOIN_S` (5 s) for a write that has already started on the status pool. A write still running after
   that is logged, and the sampler drops its handle. The snapshot build no longer writes samples, so the samples come
   at a fixed interval and not at the times a build happens to run.

## Costs and limits

- **An idle gateway starts no snapshot build.** The refresher stops after the window. The build costs about
  0.6 s of work each time it runs, and a loop lag of 14.5 ms (median of worst) and 35 ms (worst) during it.
  Watch the `[loop]` rows.
- **The first read after idle is stale.** It gets the stored snapshot with its age, and the next read, one
  interval later, gets a fresh one. A read after a restart gets `checking` until the first build finishes.
- **A read after idle starts a background build.** The build's process starts happen after the read has
  returned. If one starts while a request is in flight, the perf harness counts it against that request
  (`x-perf-subproc`).
- **The backend sampler runs all the time.** It makes one HTTP GET per backend with a health URL each minute,
  and it starts no process. A backend with a lifecycle driver and no health URL has no sample now. Its recorded
  uptime was always zero before this change, because a lifecycle state alone never reads as up.
- **The sampler's HTTP-only probe can differ from the grid.** A stopped systemd unit reads as down (0) rather
  than stopped (0), and only the error-kind label differs. A unit that systemd reports as starting or draining,
  while its health URL answers, reads as 0 in the grid and as the health URL says in the sampler. Nothing in the
  gateway reads the error kind.
- **The metrics sampler still runs on its own clock.** It samples the GPU, RAM, disk, Xid and queue series
  every 10 s and every 60 s. Its GPU probe starts `nvidia-smi` on every 10 s tick, and its kernel-journal read
  starts `journalctl` on every tick unless a commit is pending. Neither is part of the idle window, so the
  gateway still starts processes while idle. The snapshot does not.
- **Data can be up to one interval plus one build old.** The page shows each section's age, so a reader
  can see it.
- **`run()` does not kill its child when the caller is cancelled**, as asyncio's subprocess did not.
  Only `start()` kills, and only for a spawn that was still starting.
- **Not changed:** the engine grid's own deadlines and classification, and the health evidence cache.

## What depends on periodic samples

The brief asked what needs samples when nobody reads. The status-sample writer is
`_record_backend_health_samples`. It ran once per build, so the build wrote the backend samples. The other
time series are written by `metrics_sampler.py`, which is not part of the snapshot.

| Consumer | Needs samples with no reader? | How it is handled |
|---|---|---|
| Backend uptime (24 h and 7 d), backend latency, the 24 h status strip and each row's `history` block (`_metrics_section`) | Yes. An outage at night must count in the 24 h uptime. | The backend sampler, once a minute, HTTP only |
| `service_error_kind` | Not read anywhere in the gateway. Written by the sampler and by `scripts/ui/household_seed.py`. | Written as before |
| GPU, RAM, disk, Xid, queue, slots, per-service RSS and VRAM (`metrics_sampler.py`) | Yes | Unchanged. The sampler runs on its own clock, every 10 s and every 60 s |
| Loop-lag watchdog (`gateway/loop_watchdog.py`) | No. It watches the event loop, not the snapshot. | Unchanged |
| Alerting | No alerting code in this repo reads the snapshot or the samples. External alerting (Grafana, ntfy) is not in this repo, and I did not check it. | Unchanged. Check the external rules before relying on this change |

The sampler's probe is HTTP only, so it does not record the lifecycle states. Those states are the only
difference between its samples and the build's. A stopped systemd unit reads as down (0) rather than
stopped (0), and a backend with no health URL has no sample.

## Where the checks are

- `tests/test_spawns_off_loop.py`: the helper (threads, pipes, timeouts, a cancelled start kills its
  child), and one test per site, and `/health`. Each test records every `subprocess.Popen` call and
  fails if one ran on the loop thread. The hermetic sandbox refuses `os.kill` and host-control command
  names, so the kill tests record the signal and use stand-ins.
- `tests/test_status_snapshot.py`, on a fake clock: no build runs without a reader; a read starts the
  refresher and returns before the build runs; the refresher builds each interval and stops after the idle
  window; a read inside the window keeps it going; a read after the stop gets the stale snapshot and its age
  at once; concurrent reads start one refresher; an idle gateway starts no status process; shutdown cancels
  and joins; a backend answer older than the freshness bound is not trusted for availability, even inside a fresh
  build, nor in the summary a build stores; the lifespan opens the snapshot and the sampler before serving and joins
  both after.
- `tests/test_backend_health_sampler.py`: the sampler probes the HTTP health URLs and starts no process; it
  samples once at startup and then once a minute; shutdown joins the sampler and waits, up to its bound, for a write
  that has started; the status build writes no samples.
- `tests/test_status_probes_off_loop.py`: no probe starts on the loop; one systemctl call per scope;
  sections run together; a section past its budget is stale and does not hold the others; a
  shielded lifecycle batch survives one row's deadline.
- `tests/test_home_engine_rows.py`: the grid's per-engine deadlines.
- `tests/test_status_build.py`: the builder keeps the loop responsive (blocking sections, disk
  pressure stat, detached config).
- `tests/test_perf_harness.py`: the budget table has no route exceptions.
- `scripts/perf/spawn_lag.py`: the loop-lag probe above.

## Reproduce the measurements

1. Run `scripts/perf/spawn_lag.py --repo <checkout> --out lag.json --admin-seed <seeded admin.db>`
   under the hermetic venv. Use `--sites` to run one site.
2. For the cold rows, seed a fixture with `scripts/ui/live_data_seed.py --state-root <dir> --port
   <port>`. The seed script takes ports 18000 to 18998 only. Copy its `admin.db`, `receipts.db` and
   `media_jobs.db` into `<src>/data/`, and copy `proxy_config.yml` into `<src>`.
3. Start `scripts/perf/serve.py --port <port> --source <src>` from the checkout you measure. Pass
   `--source` every time: its default is the live checkout. Check the listener's PID with `ss -ltnp`
   against the ready line's `gateway_pid`. Serve the fake engine on `port+1` too, so leave that port
   free. Stop each server by SIGTERM to its `serve_pid`, then check the ports are closed.
