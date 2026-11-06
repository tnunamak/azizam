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
`UI_E2E_FIRST_RUN=1` starts the gateway on the shipped `first-run.yml` (no backends,
no owner, no seed) for `web/e2e/first-run.spec.ts`, which `playwright.config.ts` runs
as the `chromium-first-run` project on `UI_E2E_PORT + 70`.

Screenshots: `web/test-results/screens/`, named by route, theme and width.
Failures include traces under `web/test-results/artifacts/`. All artifacts and
browser cookies are ignored by Git.

After the screenshot tour, use `scripts/ui/collect_screens.py --destination
<review-directory> --since <run-start-unix-seconds>` to exclude earlier captures.
Retired pages and fixtures are excluded even if their old screenshots remain.

For the seeded household review, run `scripts/ui/design-review-capture [output-directory]`
from any directory. It starts the isolated `serve.py`
supervisor with the household profile, signs in as Sam (owner) and Robin
(member), then captures every product route at 1440px and 390px in light and
dark themes. Screenshots and `manifest.json` go to
`~/.tmp/gw-product/ui-review/household` by default; pass a new or empty output
directory or set `UI_CAPTURE_DIR` to choose another destination. The runner
refuses to write into a nonempty directory. `UI_E2E_PORT` can select an isolated port from
18000–18998. The manifest records requested route, view, theme, width, observed
state, actual route after navigation, filename, and adjacent body-text file.
Member admin API restrictions include their HTTP status; redirects include the
requested and actual route. The manifest records interactions that could not be opened under
`skipped_states`. Every requested owner interaction must be captured for all
four theme/width combinations or the run fails with a diagnostic manifest.
Member People and Activity tabs can be unavailable by design. The tour captures
People invite/plan tabs, Activity Changes and Requests tabs, a failed Activity
receipt, the Status alert, New app, invite dialog, Try result, and Library detail.

`UI_SEED_PROFILE=household` is opt-in for `scripts/ui/serve.py`; it seeds only
fresh isolated harness state. The fixture covers Sam, Robin, a restricted
member, apps, requests, provider spend, engine health, and generated media.
Remote provider behavior and login use synthetic local fixtures; the harness
does not contact an external identity provider or remote model service.

## UI quality sweep

`web/e2e/sweep/` visits every route in `web/src/app` and its meaningful states
(tabs, filters, popovers, folds, dialogs, row menus, the Try workflow form with
a picture workflow) at 390 and 1440 px, light and dark, as owner, member and
signed-out visitor. A page with no entry in `routes.ts` fails the run.

```sh
cd web
npm run sweep                    # household fixture + an unclaimed gateway for first run (ports 18860-18863)
npm run sweep:live               # synthetic live-data harness (18866-18867)
npm run sweep:copy               # live-data harness on a COPY of production data (18870-18871)
npm run sweep -- --out ~/report.md   # also save the report, JSON and cited screenshots there
npm run sweep:gate               # pre-deploy gate: fixture sweep, fails only on findings not in the baseline
```

`npm run sweep` exits non-zero on any failing finding, on a state that could not
be swept, on a route with no sweep entry, or on a broken test. `sweep:gate`
passes `--baseline e2e/sweep/baseline.fixtures.json`, the known findings, so only
new ones fail; it also prints the known findings that are no longer seen. After
fixing or accepting findings, refresh it with
`npm run sweep -- --write-baseline e2e/sweep/baseline.fixtures.json` and commit it.
The gate matches a finding by surface, rule and text, not by selector.

Each page fails on horizontal overflow (including popovers), filesystem paths,
raw ids, research notes, raw errors, member jargon, visible Unknown states,
axe WCAG AA violations, controls under the phone bottom nav, and uncaught
script errors. Term lists live in `web/e2e/sweep/lists/`. Results:
`web/test-results/sweep/<target>/{sweep.json,report.md,shots/}`; screenshots
are taken only for findings. Narrow a run with `SWEEP_ROUTES`, `SWEEP_ROLES`,
`SWEEP_WIDTHS`, `SWEEP_THEMES`; `SWEEP_AXE=0` skips axe; `SWEEP_SHOTS=all`
captures every state.

`sweep:copy` reads `~/applications/azizam/data/*.db` through SQLite's read-only
backup API into `.ui-e2e-state/copy/` (about 600 MB), rewrites every backend to
the fake engine, runs the gateway behind the fixture connection guard and
deletes the directory on exit. Media files and the provider secrets key are not
copied, so Library items read "no longer stored" and remote providers report a
missing key there. `SWEEP_DATA_STATE=restarted` drops the last 30 minutes of
receipts and samples to mimic a gateway that just restarted.
