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

The container packaging check builds the UI from source, then proves Home,
sign-in, owner claim, the admin redirect and Status against fresh private state:

```sh
docker build -t vivid-fish-cutover-check .
python3 scripts/ui/check-cutover-container.py
```

Its host port defaults to 18192 and must stay within 18000–18999. The test
container is stopped and its temporary data directory is removed on exit.

After the screenshot tour, use `scripts/ui/collect_screens.py --destination
<review-directory> --since <run-start-unix-seconds>` to exclude earlier captures.
Compatibility admin routes reuse their destination's screenshot matrix. Retired
admin pages and fixtures are excluded even if their old screenshots remain.
