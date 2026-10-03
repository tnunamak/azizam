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
