# SDK Compatibility Harness

Scaffold for proving that real SDKs work against this gateway. See
[`docs/SDK_COMPATIBILITY_HARNESS.md`](../../docs/SDK_COMPATIBILITY_HARNESS.md)
for principles, the priority list, and the per-protocol case grid.

This directory is a **scaffold**, not exhaustive coverage. The first
slice gives us:

- a shared `harness/` package (env validation, matrix builder, fake
  backend response shapes)
- no-network unit tests under `tests/test_compat_harness.py` that the
  core repo test suite runs by default
- optional Python SDK probes under `python/` that import real SDKs only
  when they are installed
- a raw-HTTP gateway contract probe that gates audit-critical OpenAI and
  Anthropic response shapes without SDK dependencies
- optional Node SDK probes under `node/` that are completely isolated
  from the Python install path
- a runnable CLI at `scripts/compat/run_matrix.py` that aggregates
  whatever probes are available and writes a matrix JSON

## Layout

```
tests/compat/
  README.md
  harness/
    __init__.py
    config.py            # HarnessConfig, load_config, ConfigError
    matrix.py            # ProbeResult, ProtocolMatrix, build_matrix
    fake_backends/
      openai_chat.py     # canonical OpenAI Chat response shapes
      ollama.py          # canonical Ollama native shapes
  python/
    probe_gateway_contract.py # optional: live raw-HTTP audit gates
    probe_openai.py      # optional: real openai SDK
    probe_anthropic.py   # optional: real anthropic SDK
    probe_ollama.py      # optional: real ollama SDK
    requirements-*.txt
  node/
    package.json
    openai.test.mjs
    README.md
  fixtures/              # request/response fixtures (populated per probe)
  matrix/
    compatibility.schema.json
```

## Environment

The harness reads two variables. Both are gateway-side; SDK probes
re-map them onto each SDK's own knobs.

| Variable | Required | Notes |
|---|---|---|
| `AZIZAM_BASE_URL` | yes | Gateway root, e.g. `http://127.0.0.1:8080`. Must **not** include `/v1` or `/api`. |
| `AZIZAM_API_KEY` | yes (default) | Bearer token. Optional when the probe declares `allow_anonymous=True`. |
| `AZIZAM_TIMEOUT_S` | no | Per-request timeout in seconds. Default `30`. |
| `AZIZAM_SMOKE_LIVE_EMBEDDINGS` | no | Raw HTTP smoke opt-in for embedding backend round-trips. Default off. |
| `AZIZAM_SMOKE_EMBEDDINGS_MODEL` | no | OpenAI embeddings probe model. Default `nomic-embed-text-v1.5`. |
| `AZIZAM_SMOKE_OLLAMA_EMBED_MODEL` | no | Ollama embed/embeddings probe model. Default `nomic-embed-text-v1.5`. |
| `AZIZAM_SMOKE_LIVE_VISION` | no | Opt-in for the chat image-input-block SDK probe. Default off — text-only local backends 4xx an image content block, which would be a false negative for gateway shape conformance. |
| `AZIZAM_SMOKE_LIVE_TOOLS` | no | Opt-in for the Anthropic `messages.create.tool_use` SDK probe. Default off — local backends without tools-capable models would 4xx the request and that isn't a gateway-shape failure. |
| `AZIZAM_SMOKE_AUDIO_TRANSCRIPTION` | no | Opt-in for the OpenAI Python `audio.transcriptions.create` SDK probe. Uses a stdlib-generated tiny PCM16 WAV (no committed binary fixture). |
| `AZIZAM_SMOKE_AUDIO_TRANSLATION` | no | Opt-in for the OpenAI Python `audio.translations.create` SDK probe. Auto-skips when the gateway's STT backend does not advertise translation; the shape contract is still gated by `probe_gateway_contract.py`. |

## Running the core unit tests

The harness's own correctness tests run with the existing venv and need
no SDKs:

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
$PROXY_PYTHON -m unittest discover -s tests -p 'test_*.py' -v
```

## Running Python SDK probes (optional)

The raw-HTTP audit probe needs no SDK install, only a reachable gateway
and an app API key:

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
export AZIZAM_BASE_URL=http://127.0.0.1:8080
export AZIZAM_API_KEY=sk-your-proxy-key
$PROXY_PYTHON -m unittest tests.compat.python.probe_gateway_contract -v
```

It covers the critical compatibility failures from `/tmp/api-audit.md`:
blank OpenAI `message.content`, streamed text arriving only as
`reasoning_content`, malformed JSON returning the wrong class of error,
missing/unknown model silently routing to default, empty Anthropic
`content`, and non-OpenAI-shaped transcription errors.

Install only the SDK you want, into the existing venv. The repo's
default `requirements.txt` is untouched.

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
PROXY_PIP=${PROXY_PIP:-venv/bin/pip}

$PROXY_PIP install -r tests/compat/python/requirements-openai.txt
export AZIZAM_BASE_URL=http://127.0.0.1:8080
export AZIZAM_API_KEY=sk-your-proxy-key
$PROXY_PYTHON -m unittest tests.compat.python.probe_openai -v
```

Replace `requirements-openai.txt` with `requirements-anthropic.txt` or
`requirements-ollama.txt` as needed. Probes auto-skip when their SDK
is missing or when `AZIZAM_BASE_URL` is unset. The OpenAI probe
covers `/v1/embeddings`; the Ollama probe covers both current
`/api/embed` and legacy `/api/embeddings`.

## Running Node SDK probes (optional)

```bash
cd tests/compat/node
npm install
AZIZAM_BASE_URL=http://127.0.0.1:8080 \
AZIZAM_API_KEY=sk-your-proxy-key \
npm run probe:all
```

Node deps stay inside `tests/compat/node/` and are not part of the
Python repo install. The Node lockfile is committed so SDK compatibility
checks are reproducible; `node_modules/` remains ignored.

## Emitting the compatibility matrix

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
$PROXY_PYTHON scripts/compat/run_matrix.py --dry-run
$PROXY_PYTHON scripts/compat/run_matrix.py --out tests/compat/matrix/compatibility.json
```

When the SDK + env preconditions hold for a probe, the runner loads
its unittest TestCase and executes the test method backing each
declared case, mapping outcomes to `pass` / `fail` / `skipped`. When
the SDK or env are missing, every case is recorded as `skipped` with
the reason — the case grid is always documented in full.

The output is validated against
`tests/compat/matrix/compatibility.schema.json`.

## Adding a new SDK or app probe

1. Pick the protocol it speaks (OpenAI, Anthropic, Ollama, etc.).
2. Add a Python module under `tests/compat/python/probe_<sdk>.py` or a
   Node test under `tests/compat/node/<sdk>.test.mjs`.
3. Auto-skip when the SDK is not installed and when
   `AZIZAM_BASE_URL` is unset.
4. For Python probes, emit `ProbeResult` values for each case in the
   relevant per-protocol case grid (see
   `docs/SDK_COMPATIBILITY_HARNESS.md`) and register the probe in
   `scripts/compat/run_matrix.py`.
5. For Node probes, add or update the isolated `npm run probe:all`
   suite. Node probes intentionally do not feed `run_matrix.py`; that
   runner is Python/unittest-shaped. Add a dedicated Node JSON reporter
   before merging Node results into the matrix.

For long-tail apps (Open WebUI, LibreChat, SillyTavern, Continue,
Cline, Claude Code, n8n, Home Assistant) the right primitive is a
**smoke probe**: drive the app's expected request shape through the
gateway and check just the envelope and headers. See the smoke-test
table in `docs/SDK_COMPATIBILITY_HARNESS.md`.
