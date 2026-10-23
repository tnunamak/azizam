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
database. It runs every 15 s, whether or not anyone reads it.

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
4. **The status page and Home read one background snapshot** (`status_endpoint.py`). One full build
   runs every `SNAPSHOT_INTERVAL_S` (15 s), with each section on its own budget. Each reader gets the
   latest snapshot and never probes. `snapshot.section_age_s` gives the age of each section, and
   `snapshot.age_s` gives the age of the whole build. Before the first build finishes, readers get a
   `checking` answer at once. A failed build keeps the last snapshot and logs the error type only.
   `/admin/api/status`, `/me/api/home`, `/v1/status` and the owner's HTML status page all read it.
   `_engine_rows_payload`, the Home refresh tasks and their cache are removed.
5. **Shutdown cancels and joins the build.** The lifespan calls `status_endpoint.stop_snapshot()` where
   it used to call `_stop_home_refreshes()`.
6. **Earlier round, kept:** the systemd probes run in one batch per scope, each status section has a
   budget, `_backend_grid` gives each engine its own deadline, and a refused connection is classified
   only on `ECONNREFUSED`. The lifecycle batch is shielded, so one row that hits its deadline does not
   cancel the batch the other rows wait for.

## Costs and limits

- **The background build runs whether or not anyone reads.** About 0.6 s of work every 15 s, and
  a loop lag of 14.5 ms (median of worst) and 35 ms (worst) during it. Watch the `[loop]` rows. If the
  cost matters, lengthen the interval when no one has read for several minutes.
- **A reader that arrives before the first build sees `checking`.** The first build starts at startup and
  took about 0.6 s of work in the fixture, so this lasts about that long after a restart.
- **Data can be up to one interval plus one build old.** The page shows each section's age, so a reader
  can see it.
- **`run()` does not kill its child when the caller is cancelled**, as asyncio's subprocess did not.
  Only `start()` kills, and only for a spawn that was still starting.
- **Not changed:** the engine grid's own deadlines and classification, and the health evidence cache.

## Where the checks are

- `tests/test_spawns_off_loop.py`: the helper (threads, pipes, timeouts, a cancelled start kills its
  child), and one test per site, and `/health`. Each test records every `subprocess.Popen` call and
  fails if one ran on the loop thread. The hermetic sandbox refuses `os.kill` and host-control command
  names, so the kill tests record the signal and use stand-ins.
- `tests/test_status_snapshot.py`: a reader is answered at once while the first build is blocked; a
  reader never starts a probe; each section reports its age; the build repeats on the schedule;
  shutdown cancels a running build and waits for it; a failed build keeps the last snapshot and logs
  only its type; the lifespan starts the build and joins it at shutdown.
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
