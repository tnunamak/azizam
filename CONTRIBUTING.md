# Contributing

Thanks for helping improve Azizam. This project is a self-hosted,
OpenAI-compatible gateway for local and remote AI backends.

## Development setup

1. Clone the repository.
2. Create or refresh the local environment with `uv`:
   ```bash
   uv venv venv --python 3.12
   uv pip install --python venv/bin/python -r requirements.txt
   venv/bin/python -m prisma generate --schema venv/lib/python3.12/site-packages/litellm/proxy/schema.prisma
   ```
3. Install Node.js 20 or newer with npm, then install web dependencies:
   ```bash
   npm --prefix web ci
   ```
4. Copy `.env.example` to `.env` only if you need local overrides.
5. Start from the docs that match your change:
   - `CONFIG.md` for environment and bootstrap settings.
   - `AUTH_GUIDE.md` for Apps, Profiles, keys, and admin access.
   - `docs/EXTENSION_ARCHITECTURE.md` for extensions.
   - `docs/VENDOR_FACADES.md` for vendor-compatible routes.

LiteLLM needs its generated Prisma client on a fresh environment, even when
`DATABASE_URL` is unset. `start.sh` generates it before starting managed
LiteLLM and repeats generation when the schema or requirements change, or
when the client is missing. Generation failure stops startup.

Do not commit real API keys, local database files, generated media, or machine
specific paths.

## Tests

Use the hermetic runner before you report a change as ready. It expects a clean,
committed tree because it proves the checked-in state, not a local scratch diff:

```bash
scripts/run-hermetic-pytest.sh -q -n 12
```

`-n N` runs the suite in N pytest-xdist workers (about 3.5 minutes with 12,
against 34 minutes in one process). Leave it out to run serially.

For a narrow Python check while iterating, pass the test path through the same
runner, or let `scripts/test-affected` pick the tests your commits can reach:

```bash
scripts/run-hermetic-pytest.sh -q tests/test_name.py
scripts/test-affected --list   # show the selection; drop --list to run it
```

For web changes, also run:

```bash
npm --prefix web run lint
npm --prefix web run build
```

## Commit style

Keep commits small and reviewable. A good commit message names the concrete
behavior or documentation contract that changed:

```text
docs: add route support matrix check
gateway: reject unsupported rerank fields explicitly
web: show provider spend cap status
```

Do not mix unrelated cleanup with a behavior change.

## Adding a workflow

Workflows are data contracts. Add or update the manifest under `workflows/` or
an extension package under `extensions/`. Add a focused test for request
binding and rejected fields, usually near the existing media, manifest, or
extension tests in `tests/`. Document the client-facing shape in the relevant
API or recipe doc, such as `docs/LOCAL_CLIENT_RECIPES.md`,
`docs/EXTENSION_TARGETS.md`, or `docs/EXTENSION_ARCHITECTURE.md`. The gateway
should report unsupported inputs clearly instead of silently ignoring them.

## Adding an extension

Extensions live under `extensions/` and are loaded through the package model
documented in `docs/EXTENSION_ARCHITECTURE.md`. They must have a namespaced
input shape, deterministic validation, and tests that cover accepted, rejected,
and ignored fields. Keep extension logic outside the hot request path unless
the route needs it.

## Adding a provider or backend

Treat engine-specific behavior as data. Use service records, presets, and
declarative transforms where possible. Do not add per-engine branches or parser
classes to core code. Read `docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md` before you
change provider or engine handling.

Provider secrets are write-only. Use Providers to store an encrypted key in
`admin.db`, or reference an environment variable supplied by Docker secrets or
another deployment secret store. The dashboard may show whether a key is set
and its suffix, but it must not expose the full value. See
[`docs/REMOTE_PROVIDERS.md`](docs/REMOTE_PROVIDERS.md#key-custody-and-threat-model).

## Pull requests

Every pull request should include:

- What changed.
- Why it changed.
- How you tested it.
- Any operator action needed after update.

Use `.github/PULL_REQUEST_TEMPLATE.md` when opening the PR.
