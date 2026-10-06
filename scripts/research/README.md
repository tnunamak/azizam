# Research Scripts

Small, repeatable research utilities used to keep product simulations grounded in
real user discussions.

## Reddit Corpus

Default output is `/tmp/azizam-reddit-corpus`.

Collect target-subreddit threads with the committed query plan:

```bash
python3 scripts/research/reddit_corpus.py collect
```

Use Arctic Shift only, which avoids Reddit JSON rate limits:

```bash
python3 scripts/research/reddit_corpus.py collect --source arctic --sleep 1
```

Normalize already-downloaded raw thread JSON into Markdown:

```bash
python3 scripts/research/reddit_corpus.py normalize --allow-failures
```

Print corpus counts:

```bash
python3 scripts/research/reddit_corpus.py summarize
```

The default query plan lives in `scripts/research/reddit_corpus_queries.json`.

## Live Reddit Simulation Trials

Run persona-based live probes against the gateway and write trial transcripts:

```bash
python3 scripts/research/reddit_simulation.py
```

The default output is a timestamped directory under
`/tmp/azizam-simulations/live` containing:

- `live-probes.json`
- `persona-trials.md`
- `reddit-thread-brief.md`

By default the harness only runs low-cost discovery/status probes. To include
small chat/API generation probes, opt in explicitly:

```bash
python3 scripts/research/reddit_simulation.py --include-generative
```

The default request timeout is 60 seconds because local LLM cold starts and
queued responses can legitimately exceed typical web-service timeouts.

Runtime keys are read from `AZIZAM_API_KEY`. Admin keys are read from
`AZIZAM_ADMIN_KEY`. Both accept the migration env fallbacks. Missing keys are recorded
as trial findings rather than guessed.

Persona definitions live in `scripts/research/reddit_simulation_personas.json`.

## 100x Product Trial Lab

Run task-level user journeys that combine local setup evidence, live gateway
probes, protocol compatibility checks, media workflow discovery, policy
boundaries, and admin observability:

```bash
python3 scripts/research/reddit_trial_lab.py --include-costly
```

The default output is a timestamped directory under
`/tmp/azizam-simulations/100x` containing:

- `100x-trial-lab.json`
- `100x-journey-transcripts.md`
- `100x-product-gap-ledger.md`
- `100x-reddit-thread-brief.md`

Scenario definitions live in
`scripts/research/reddit_trial_lab_scenarios.json`. Runtime and admin keys use
the same environment variables as the live Reddit simulation harness. Missing
admin keys are recorded as untested evidence, not guessed.
