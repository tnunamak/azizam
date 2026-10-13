# Real-data performance harness

Measures the gateway on a **copy** of the operator's data, so slowness that only shows with real
history (hundreds of jobs and receipts, a metrics database of 400 MB, long audio files) fails a
test before the owner sees it. Nothing here writes the live databases: they are read with
SQLite's online backup API from a read-only connection (`scripts/ui/live_data_copy.py`), media
files are copied, and the copy's owner and first member get a known password in the copy only.
Engines point at the fake engine. Ports 18000-18998 only.

| Script | What it does |
|---|---|
| `run.py` | Everything below, then `check.py`. The nightly `perf` task (`scripts/test-scheduled`) runs this and opens or updates the `perf-red` issue. |
| `serve.py` | The instrumented fixture gateway on a copy (`--port`, `--clone`). Its copy of the data is a scratch directory (`scripts/scratch_owner.py`), deleted when it ends, and a killed run's copy is removed at the next start; the path is the `state` field of the ready line. Responses carry `x-perf-sql`, `x-perf-connects`, `x-perf-subproc`, `x-perf-ms`. |
| `api_bench.py` | Every GET route (route table, real ids from list responses): p50/p95, bytes, statements, connections, processes. |
| `home_bench.mjs` | Home alone, in a real browser: desktop and a throttled phone, cold and warm cache; breaks the time into HTML, JavaScript download, compile and run, and each API call. Judged by `[home]` in budgets.toml. |
| `ui_bench.mjs` | Every page, signed in as owner and member: time until `<main>` stops changing, requests, bytes, long tasks. Run from `web/` (needs a built `web/out`). |
| `check.py` | Judges both against `budgets.toml`; ranks what is over. `--counts-only` ignores times. |
| `snapshot.py` | Saves response bodies and compares two saves: the byte-identical proof for a behavior-preserving change. Run the old and the new code on copies of the same data at the same time, then `diff`. |
| `attribute.py` | Needs `PERF_ATTRIBUTE=1` on `serve.py`: which code issued each statement and process of one request. |
| `profile.py` | Needs `PERF_PROFILE=1`: where one request spends its time (sampled stacks). |

Counts (statements, connections, processes) are exact. Times depend on the host: use them only
when the load is low (the nightly skips otherwise), and read `uptime` beside any number you quote.
The gate holds the counts at fixed data sizes: `tests/test_route_work_budget.py` (every GET route,
4 apps against 24) and `tests/test_work_budget.py`.

```sh
PYTHONPATH=. venv/bin/python scripts/perf/run.py --counts-only --no-ui   # a few minutes
```
