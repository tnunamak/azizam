# Gateway UI

Next.js 15, React 19 and Tailwind v4 export static files to `web/out`.
The gateway serves them under `/ui/`; no Node server is needed in production.

The foundation has a role-aware shell, light and dark themes, and Radix-based
shadcn components. Apps & keys uses the signed-in person's `/me/api/*` endpoints.
Activity uses the owner-only audit API. Other new routes intentionally show
“Coming in this release” until their surface lanes arrive.

Tokens live in `src/styles/tokens.css`. Geist Sans and Geist Mono are bundled
from the `geist` npm package, with no font CDN or external checkout dependency.
`src/styles/legacy-components.css` preserves the old operator layouts while their
replacement pages are built. Its variables map to the new local tokens.

```sh
npm ci
npm run lint
npm run build
npm run test:components
npx playwright install chromium
npm run test:e2e
```

`test:e2e` starts a temporary gateway and fake engine on ports 18180 and 18181,
seeds them through real APIs, and stops both processes after the tests.
It uses `../venv/bin/python` or `UI_E2E_PYTHON`. Install gateway dependencies
with `uv venv venv` and `uv pip sync --python venv/bin/python
 tests/requirements-hermetic.lock` from the repository root.
Set `UI_E2E_PORT` to another unused port from 18000 through 18998 if needed.
Test state and cookies stay in ignored directories. Screenshots are saved to
`test-results/screens/`; the HTML report is in `test-results/report/`.

`npm run test:browser` retains the older browser fixture suite.
`/ui/design/` is the component reference; `/ui/gallery/` links to it.

For local development, `npm run dev` starts Next. Its default development-only
API rewrites target the gateway on port 5000. Implementation lanes should use
the isolated browser harness instead of the running operator service.
