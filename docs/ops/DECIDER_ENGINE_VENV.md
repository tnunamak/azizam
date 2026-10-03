# Decision engine venv

The decision engine wrapper uses the repo-local `venv/` interpreter. Do not add
these proof/runtime packages to the global gateway requirements until they are
reviewed, pinned, and hash-locked.

Observed install used for this lane:

```bash
uv venv --python python3.12 venv
uv pip sync --offline --python venv/bin/python tests/requirements-hermetic.lock
uv pip install --python venv/bin/python -r requirements-decisions.txt --no-deps
```

Observed package versions:

- `decider-ai==1.8.1`
- `numpy==2.5.3`
- `tokenizers==0.23.1`

The wrapper imports only the pure prompt helpers from `decider-ai` and loads
`tokenizer.json` through `tokenizers`; it does not import `decider.infer` or
`torch`.

## Memory sizing

Live CPU proof for `decider-2b-v11-Q8_0` measured about 2.9 GiB whole-process RSS for the wrapper plus child llama-server on ordinary narrow readouts, and an earlier run with full-vocabulary readouts sampled 5979 MiB (5.84 GiB) at `-c 4096`. Keep the 2B unit above the observed full-vocabulary value; the seed unit uses `MemoryMax=8G`. The 4B quality unit uses `MemoryMax=10G` as an unmeasured starting budget and should be remeasured on the target GGUF before tightening.
