# Gateway UI

Next.js 15, React 19 and Tailwind v4 export static files to `web/out`.
The gateway serves them at the origin root: Home is `/`, other pages are
`/<page>/` (for example `/library/`) and assets are under `/_next/static/`. No
Node server is needed in production. `gateway/ui_pages.json` lists every page
and who may open it (`public`, `member` or `admin`); the gateway, the shell and
the sign-in return-path check all read it, and a component test keeps it equal
to the `src/app/**/page.tsx` directories.

The foundation has a role-aware shell, light and dark themes, and Radix-based
shadcn components. Apps & keys uses the signed-in person's `/me/api/*` endpoints.
Activity uses the owner-only audit API.

Tokens live in `src/styles/tokens.css`. Hanken Grotesk (interface) and Geist
Mono (code) are bundled from npm packages, with no font CDN or external checkout
dependency.
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

`npm run test:all` runs the component tests and then the production journey.
`/design/` is the component reference.

For local development, `npm run dev` starts Next. A development-only fallback
rewrite sends every path Next does not serve to the gateway on port 5000. Implementation lanes should use
the isolated browser harness instead of the running operator service.
