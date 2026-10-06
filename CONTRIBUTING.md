# Contributing

Thanks for helping improve Azizam. This project is a self-hosted,
OpenAI-compatible gateway for local and remote AI backends.

## Development setup

1. Clone the repository.
2. Create or refresh the local environment with `uv`:
   ```bash
   uv venv venv --python 3.12
   uv pip install --python venv/bin/python -r requirements.txt
   ```
3. Copy `.env.example` to `.env` only if you need local overrides.
4. Start from the docs that match your change:
   - `CONFIG.md` for environment and bootstrap settings.
   - `AUTH_GUIDE.md` for Apps, Profiles, keys, and admin access.
   - `docs/EXTENSION_ARCHITECTURE.md` for extensions.
   - `docs/VENDOR_FACADES.md` for vendor-compatible routes.

Do not commit real API keys, local database files, generated media, or machine
specific paths.

## Tests

Use the hermetic runner before you report a change as ready. It expects a clean,
committed tree because it proves the checked-in state, not a local scratch diff:

```bash
scripts/run-hermetic-pytest.sh -q
```

For a narrow Python check while iterating, pass the test path through the same
runner:

```bash
scripts/run-hermetic-pytest.sh -q tests/test_name.py
```

For web changes, also run:

```bash
cd web
npm run lint
npm run build
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

Provider secrets belong in environment variables, Docker secrets, or another
operator-controlled secret store. The dashboard may show whether a secret
reference is configured, but it must not expose the secret value.

## Pull requests

Every pull request should include:

- What changed.
- Why it changed.
- How you tested it.
- Any operator action needed after update.

Use `.github/PULL_REQUEST_TEMPLATE.md` when opening the PR.
