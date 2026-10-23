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
memory, as a fork's does, and the loop thread is blocked for all of it. The measurements below show
the scaling.

Measured on this host, with a bare process and a `/usr/bin/true` child (median of 15 spawns):

| Resident memory | Spawn on the loop (median) | Loop blocked (max) |
|---|---|---|
| 0 MB | 2.6 ms | 2.7 ms |
| 600 MB | 32.5 ms | 33.6 ms |
| 2 GB | 92.6 ms | 97 ms |

The same spawn made by the stdlib `subprocess` module uses a vfork, which copies no page tables.
Run in a worker thread, the systemctl call took about 5 ms, and the loop lag was at most 3.7 ms at
600 MB and 2.1 ms at 2 GB. The worker thread keeps the fork away from the loop, and the stdlib spawn
is cheap at any size.

Each systemd engine costs one spawn per status build, and one per Home read after the 12 s cache.
This host has 13 such engines. Under host memory or I/O pressure, the same spawn takes far longer.
The six journal events of 1.3 to 1.9 s are that: the same path, slowed by the host. I could not
reproduce the host pressure on demand. The stack and the scaling with resident memory do reproduce.

## Fixture evidence

The perf fixture ran uvicorn's default loop with a 512 MB ballast, so resident memory was 683 MB
(production: 613 MB). Each cold read was measured with a `/health` probe running alongside it.
The stall is the probe's largest latency during the read, less the idle median. Two runs, each with
two rounds, before and after in turn, at host load 23 to 65:

| Cold read (four reads each, two rounds in two runs) | Spawns, before → after | Wall, median, before → after | Loop stall, median, before → after |
|---|---|---|---|
| Home, `/me/api/home` | 13–15 → 1 | 1.37 s → 0.13 s | 1.08 s → 21 ms |
| Owner status, `/admin/api/status` | 38–41 → 26–29 | 2.79 s → 0.73 s | 0.92 s → 64 ms |

Per read, the stall was 0.52 to 1.20 s for Home and 0.58 to 1.08 s for status before, and 18 to 22 ms
and 35 to 86 ms after. The "after" status reads spawn 26 to 29 processes: one systemctl call plus the
section probes, which run in worker threads as before. One status read in the second run spawned 29
processes, three more than the others. I did not trace them.

The loop watchdog (30 ms threshold) logged 9 and 16 stalls before, up to 1.09 s. After the change
it logged 3 and 5, none over 0.11 s. The before stalls show the same frames as the journal. The
largest events after the change (0.10 s and 0.11 s) were in `/health`'s own SQLite open, which runs
on the loop (see below), not in the status build.
The idle `/health` maximum during these runs was 71 to 286 ms, so stalls under about 100 ms are
inside the host's noise.

## Change

1. Systemd probes run on a two-thread pool with the stdlib spawn (`lifecycle_drivers._PROBE_POOL`).
   The loop no longer forks for them.
2. One `systemctl is-active -- <units>` call answers every systemd engine in a scope. It used to
   be one call per engine. The grid and Home share it.
3. The status sections run at once on a six-thread pool (`status_endpoint._SECTION_POOL`). Each has
   a budget: 3 s, and 6 s for backends. A section past its budget shows its last answer, is named
   in `stale_sections`, and gets `errors[name] = "stale: …"`. The pool keeps a status build from
   taking every worker that requests use for SQLite and file work.
4. `_status_payload_with_availability` logs one line with the exception type when it fails. The
   message is not logged, because it can carry a path or a secret.

Not chosen: D-Bus. No D-Bus client is a dependency here, and adding one is outside this change.
Not chosen: a helper process. A worker thread with the vfork spawn already keeps the loop clear.

## Left as is, same shape

- `SystemdDriver._run_systemctl` (`gateway/lifecycle_drivers.py`): start, stop and daemon-reload on
  the swap path. Rare, but each call is a fork on the loop.
- `gateway/stt_chunking.py` (a child process per STT chunk), `gateway/speech_stream.py` (ffmpeg for
  each streamed speech reply), `gateway/stt_measurement.py`, and `proxy.py` near line 12792 (the
  workflow drift refresh, a background task). Each is `asyncio.create_subprocess_exec` on the loop, so
  each one pays the spawn cost on the loop thread.
- `/health` opens a SQLite connection on the loop for each request (`proxy.py` `health` →
  `active_app_credential_count`). In the fixture this showed as 30 to 110 ms loop events under host
  noise. Moving it to a thread is a separate change.

## Where the checks are

- `tests/test_status_probes_off_loop.py`: no probe starts on the loop thread; one systemctl call
  per scope; independent sections run at the same time; a section past its budget is stale and does
  not hold the others; a stale section shows its last answer; the availability failure is logged
  with its type only.
- `scripts/perf/api_bench.py`: each cold Home and cold status read reports its loop stall.
  `scripts/perf/check.py` judges it against `[loop] max_stall_ms` in `scripts/perf/budgets.toml`.
- `scripts/perf/gateway_main.py`: the fixture runs uvicorn's default loop, as production does, and
  counts asyncio spawns, which uvloop does not report to the audit hook.
