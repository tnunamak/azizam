# Portable foundation browser checks

From a clean checkout, install Python dependencies into `venv` with `uv`, then:

```sh
cd web
npm ci
npm run build
npx playwright install chromium
npm run test:e2e
```

The harness serves the exported production UI through a real gateway. It creates
fresh private state, starts a deterministic API-compatible engine, claims an
owner and seeds services, access plans, apps, keys and a successful chat request
through HTTP APIs. It never uses an operator database or inherited provider keys.
Both children are stopped and temporary state is removed on exit.

The default gateway/engine ports are 18180/18181. Override `UI_E2E_PORT` only within
18000–18998. `UI_E2E_PYTHON` selects a dependency-installed interpreter when `venv`
is absent. `UI_E2E_STATE_ROOT` can select another disk-backed temporary parent.

Screenshots: `web/test-results/screens/`, named by route, theme and width.
Failures include traces under `web/test-results/artifacts/`. All artifacts and
browser cookies are ignored by Git. The legacy fixture browser suite is retained
in `playwright.legacy.config.ts` and can run with that explicit configuration.
