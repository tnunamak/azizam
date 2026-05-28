# Node-side compatibility probes

Isolated SDK probes that must run under Node (OpenAI Node, Anthropic TS,
Ollama JS, Vercel AI SDK).

These are intentionally isolated from the Python repo's install path:
the core `requirements.txt` and the unittest discovery in
`tests/test_*.py` never touch this directory.

## Install

```bash
cd tests/compat/node
npm install
```

This produces a local `node_modules/` scoped to this directory only.
`package-lock.json` is committed so the optional SDK probes remain
reproducible.

## Run

```bash
export OPENAI_PROXY_BASE_URL=http://127.0.0.1:8080
export OPENAI_PROXY_API_KEY=sk-your-proxy-key
npm run probe:all
```

Set `OPENAI_PROXY_SMOKE_LIVE_LLM=1` to run chat/generation round-trips
and `OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS=1` to run embedding round-trips.
Without those flags, live-cost probes skip while cheap discovery/envelope
checks still run.

## Adding new probes

1. Add a `<sdk>.test.mjs` file using `node:test`.
2. Add a `probe:<sdk>` script in `package.json`.
3. Have the probe skip cleanly when `OPENAI_PROXY_BASE_URL` is unset so
   CI without a running gateway stays green.
