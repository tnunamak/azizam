# Hermetic gateway benchmark

This harness measures gateway overhead with real Azizam request handlers and a fixed local fake backend. It uses private Unix sockets and temporary state; it does not call live backends or the running gateway.

## Run

Use Python 3.12 and install only the benchmark lock, offline:

```sh
uv venv --python 3.12 .venv
uv pip sync --offline --no-build --require-hashes --python .venv/bin/python tests/requirements-hermetic.lock
```

Run either frozen request mix, or both:

```sh
.venv/bin/python -m bench.run --mix train --reps 32 --json bench/results/train.json
.venv/bin/python -m bench.run --mix test --reps 32 --json bench/results/test.json
.venv/bin/python -m bench.run --mix all --reps 32 --json bench/results/all.json
```

`--reps` is a target per case and concurrency. The runner rounds it up to full concurrency batches: at 32, the sample counts are 32 at every concurrency. It warms both paths with one single-client request per shape before measurement (`--warmup 1`), then measures concurrency 1, 8, and 32 in that order. The default 32 is a quick diagnostic, but it is too small for reliable tail estimates. Results and profiles under `bench/results/` are git-ignored.

For optional Linux namespace isolation, install `bwrap` and run the same arguments through the wrapper:

```sh
.venv/bin/python -m bench.sandbox -- --mix train --reps 32 --json bench/results/train.json
```

The wrapper creates user, network, PID, and other namespaces; exposes the repository and Python runtime read-only; and binds only temporary home/output locations writable. The child audit hook blocks host commands, DNS and non-fixture sockets. Outside the namespace wrapper it also checks file writes, mutations and databases; under the wrapper the read-only mounts enforce that boundary without repeated path-resolution work. It is not a substitute for the OS namespace boundary.

For CPU-pinned runs, pass three distinct Linux CPU IDs in client, gateway, engine order. For example:

```sh
.venv/bin/python -m bench.run --mix train --reps 32 --cpu 2,4,6 --json bench/results/train-cpu.json
```

The runner pins itself and assigns the second and third CPUs to the gateway and fake engine processes. Choose CPUs deliberately; affinity alone does not isolate the machine from other workloads.

## Measurement and limits

Each run measures paired gateway and direct-to-fake requests for chat, streaming chat, models, Responses, streaming Responses, images, TTS, STT, status, and health. Pair order alternates between repeated batches. At concurrency 32 with `--reps 32` there is one batch, always direct then gateway, so that condition retains a fixed order effect. The reported overhead is each gateway latency percentile minus the same direct-fixture percentile. Whole paired batches remain together when resampling; individual requests are not treated as independent under concurrency. JSON retains both series as `samples`/`sample_blocks` and `reference_samples`/`reference_blocks`. Streaming records one-way timestamps for each observed chunk; TTFB is time to first SSE body byte, TTFT is time to first text token, and chunk relay overhead compares backend emission with client arrival. `chunk_relay_overhead` pools all four content chunks per request while retaining whole-burst blocks; `mean_chunk_relay_overhead` separately summarizes each request’s four-chunk mean. CPU time comes from a separate gateway burst across all its threads with the lag probe disabled, subtracting an empty control-window calibration. JSON retains the raw value and calibration floor. Control requests sit outside production middleware. The real metrics writer is flushed at each boundary, so CPU includes the completed persistence work even when production would batch it later. cProfile runs continuously in the gateway child after fixture initialization; on this Python 3.12 interpreter it also captures calls in worker threads. Its output includes warm-up and control work. Profile durations are wall time, and overlapping asynchronous or threaded cumulative times must not be summed or interpreted as process CPU. Gateway latency batches include the requested 1 ms loop probe; direct batches do not. Thus the latency delta includes probe cost. Throughput is per concurrent batch, not a sustained-load rate.

The fixture reuses the UI fake engine. Status uses the real builder and renderer, fixed host observations, and its single-flight path; the metrics writer is started manually. Full application startup and periodic maintenance, the production loop watchdog and firehose replay are not measured. TTS returns fixed WAV bytes and substitutes a native ffmpeg metadata fixture, so native remux cost is excluded. Images are always fixed 32×32 pixels regardless of requested size. Private database/media state defaults to `/dev/shm`, a small tmpfs allocation on Linux; the namespace wrapper supplies a private tmpfs there. This controls physical disk latency while retaining SQLite and file operations. Use `--state-dir` for a storage diagnostic and compare only matching filesystem kinds. STT deliberately rejects `/tmp` spool paths. There is no GPU work or live service traffic.

Negative auth controls run before timing. Response grading, persisted Responses retrieval, backend call counts, metrics, receipts, and status errors and sections are checked after each timed batch. A failed check leaves correctness score zero, and compare rejects such reports. Keep the train and test mix files frozen: use train for candidate work and run test once only after the candidate is frozen. Do not optimize the fixtures or grader, or weaken correctness checks. Choose one primary metric and the minimum worthwhile gain before changing code.

## Repeat and compare

Use at least three fresh processes for each mix and condition. Keep each report as a separate process block, then aggregate and compare:

```sh
for i in 1 2 3; do
  .venv/bin/python -m bench.run --mix train --reps 32 --json "bench/results/train-$i.json"
done
.venv/bin/python -m bench.report bench/results/train-1.json bench/results/train-2.json bench/results/train-3.json --json bench/results/train-envelope.json
.venv/bin/python -m bench.report bench/results/all-1.json bench/results/all-2.json bench/results/all-3.json --json bench/results/all-envelope.json
.venv/bin/python -m bench.compare bench/results/baseline.json bench/results/candidate.json
```

Aggregate train and test separately to retain per-mix envelopes. `--mix all` reports both mixes; comparison also separates their metrics. The report uses hierarchical bootstrap intervals over process and batch blocks. Its noise floor is the maximum of the observed pairwise process-run differences and a model-based bootstrap A/A delta interval; the latter is not a real independent A/A trial. Run an actual unchanged-code A/A comparison before trusting a candidate result. Interleave baseline and candidate runs in ABBA order to expose time drift. Comparison reports estimated 95% delta intervals and flags fewer than three process blocks, exploratory intervals with fewer than five blocks, and undersampled tails. These are per-metric noise flags, with no global winner verdict. Use the A/A noise floor to judge the minimum worthwhile gain: if noise exceeds that gain, collect more runs or make no gain claim. Do not cherry-pick across the many emitted metrics. Per-run p95 below 100 samples and p99 below 1,000 samples are flagged as undersampled; those tail estimates are weak evidence.

Compare only runs with matching benchmark settings, dependencies, lock and harness hashes, kernel, machine, CPU model, state filesystem kind, and network namespace. Installed packages must exactly match the lock and the interpreter must be Python 3.12; startup fails otherwise. The production source may differ between baseline and candidate; pooling differing sources on one side is rejected. The harness fingerprint includes the reused UI fake engine. Profiled reports are invalid for comparison. Use `--profile path.pstats` only for diagnosis, then rerun without profiling for measurement.

Validate the statistics, malformed response rejection and boundary tripwires with:

```sh
.venv/bin/python -m unittest bench.test_stats bench.test_contract -q
```
