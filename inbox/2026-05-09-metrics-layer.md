# 2026-05-09 — SQLite metrics persistence layer

## Why this exists

Until today, every status-page widget was computed from instantaneous probes
(nvidia-smi, sensors, /proc, ComfyUI /queue). That's fine for the "right now"
columns but makes it impossible to answer questions the dashboard *needs* to
answer:

- "Did GPU0 hit 90 C in the last hour?"
- "Is the root partition filling? When will it run out?"
- "Did this key's request rate just collapse, or has it been quiet all day?"
- "How many Xid 109 events have we taken today?"
- "Did VRAM% climb steadily across the morning, or spike once and recover?"

We were eyeballing `journalctl` and the systemd logs for these. They live in a
pile of unstructured text and disappear into the journal's vacuum policy. The
fix is a small, dedicated time-series store the proxy owns.

## Design principles (preserve these on future rewrites)

1. **Single tag-based table.** No metric-specific schemas. `(ts, metric, tags_json, value)` covers
   every dimension we need (GPU index, mount point, service name, API key,
   path, status). Adding a new metric is one `record_sample(...)` call — no
   migrations.
2. **Pre-rolled aggregates.** Three tables (raw / 1min / 1hour) with hard
   retention windows (7 d / 30 d / 365 d). Disk budget < 500 MB at steady
   state. We never query "avg of the last year of raw samples"; we query the
   1-hour rollup.
3. **WAL + autocommit writer + many short-lived readers.** Stdlib sqlite3.
   No ORM. No `aiosqlite`. The writer is one connection in autocommit mode
   (`isolation_level=None`); each query in `status_endpoint._metrics_section`
   opens a fresh read connection. WAL keeps the read path lock-free.
4. **The sampler must not crash the lifespan.** Every cycle is wrapped in
   try/except at three levels (per-probe, per-tier, top-level). If
   `nvidia-smi` disappears or `journalctl` permission-denies, we log and keep
   going. The dashboard is allowed to render `(no data)`; the lifespan is
   never allowed to fall over because of a metric write.
5. **Disk-pressure awareness.** When `data/`'s partition drops below 1 GB
   free we pause writes (logging once per 10 min). The sampler still runs;
   it just stops appending. We'd rather the dashboard go stale than fill the
   disk we're warning about.
6. **Retention is enforced by the sampler, not by a cron.** `prune_old()`
   runs once per day from inside `sampler_loop()`. One process owns the data;
   nothing else schedules against it.
7. **Brand-token sparklines, no JS.** Inline SVG. Each ~200 bytes. The
   status page already uses `var(--dive-biolum)` etc.; sparklines reuse those
   tokens so charts look native to the page rather than bolted on.

## File layout

- `metrics_store.py` — pure storage layer. Schema, WAL, integrity check,
  insert/query/aggregate/series/downsample/prune, disk-avail probe.
- `metrics_sampler.py` — async sampler. Reuses `status_endpoint` probe
  helpers (`_gpus_raw`, `_read_sensors`, `_disk_partitions`,
  `_ram_attribution`, `_vram_attribution`, `_comfy_queue_and_stats`,
  `_llama_props_and_slots`, `_journal_xids`) — single source of truth for
  what counts as a GPU temp.
- `status_endpoint._metrics_section()` — payload builder; produces the
  `metrics` block with `gpus[].temp_max_5min_c / _1h / _24h`,
  `cpu.temp_max_*`, `disk[mount].avail_change_24h_gb` and
  `time_to_full_estimate_hours`, `trends.per_key_rpm_5min`,
  `trends.xid_109_hourly_24h`, plus 24h sparkline series.
- `firehose_render._render_trends()` — renders the new "Trends" section
  using `_sparkline()` + `_bar_sparkline()`.
- `data/metrics.db` — runtime DB (gitignored). `data/.gitkeep` is committed
  so the directory exists on fresh clones.

## Why no ORM / no migrations

The schema is fixed. The sampler is the only writer. The status endpoint is
the only reader. Adding columns will mean adding tables (or extra rollup
columns), not altering this one. SQLAlchemy / Alembic / aiosqlite would all
add weight without buying anything.

## What this enables (future work)

- Anomaly badges in the banner ("VRAM% jumped 15 pts in 5 min").
- Operator-facing CLI: `python3 -m metrics_store query "gpu_temp_c
  --gpu=0 --since=1h --max"`.
- Export to Prometheus via a thin `/v1/metrics/prometheus` endpoint that
  reads the same DB.
- Alert hooks (e.g. Telegram poke when `time_to_full_estimate_hours < 24`).

These all read from the SQLite store; none of them require touching the
sampler.

## Verification at commit time

Run from the repo root with the proxy's venv:

```bash
./venv/bin/python tests/test_metrics_store.py
./venv/bin/python tests/test_metrics_sampler.py
```

Both suites cover insert + query, tag filtering, downsample, prune, sampler
emit functions tolerating missing upstream data, and request-tag round-trip.
