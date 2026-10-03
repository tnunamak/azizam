# Canonical UI route cutover plan

Status: plan only. Home ships under `/ui/home/`. The existing `/ui/` entry
sends signed-in people there. This lane does not change canonical Python
routes, authentication handlers, or the static asset mount.

## Serve the export without a Node server

Today `mount_web_ui` in `proxy.py` mounts `web/out` at `/ui` with
`StaticFiles(html=True)`. Next builds with `basePath: "/ui"`,
`assetPrefix: "/ui"`, and `trailingSlash: true`. The resulting page links
and script paths therefore include `/ui`; moving that directory to `/`
alone would break navigation.

For cutover, build a separate release artifact with an empty `basePath`
and `assetPrefix`, while retaining `output: "export"` and
`trailingSlash: true`. Keep the current `/ui` artifact available for
rollback. Publish the new `_next` assets under `/_next`; use hashed-asset
caching there and `no-store` on session-sensitive page entry responses.
All data continues to come from the same-origin JSON APIs. The export
contains no credentials or person-specific data.

Register explicit FastAPI GET routes for product pages and serve their
exported `index.html` files after the applicable session check. Do not
mount a catch-all static application at `/`: that could swallow API
routes, receipts, authentication callbacks, or unknown paths. Add explicit
asset mounts and keep a real 404 for unknown destinations.

## Route map

| Canonical route | Export entry | Authentication |
| --- | --- | --- |
| `/` | `home/index.html` | Signed-in person |
| `/home/` | Redirect to `/` | Signed-in person |
| `/models/` | `models/index.html` | Signed-in person |
| `/try/` | `try/index.html` | Signed-in person |
| `/library/` | `library/index.html` | Signed-in person |
| `/usage/` | `usage/index.html` | Signed-in person |
| `/apps/` | `apps/index.html` | Signed-in person |
| `/status/` | `status/index.html` | Owner or admin |
| `/engines/` | `engines/index.html` | Owner or admin |
| `/providers/` | `providers/index.html` | Owner or admin |
| `/people/` | `people/index.html` | Owner or admin |
| `/activity/` | `activity/index.html` | Owner or admin |
| `/settings/` | `settings/index.html` | Owner or admin |
| `/sign-in/` | `sign-in/index.html` | Public |
| `/invite/` | `invite/index.html` | Public; invite checked by API |
| `/setup/` | `setup/index.html` | Public; claim checked by API |
| `/showcase/` | `showcase/index.html` | Public, read-only |

The HTML entry at `/` serves Home directly; it does not serve the export's
root redirect page, which would otherwise redirect back to Home. Normalize
slash variants consistently. Retain query strings on compatibility redirects,
including receipt filters and authentication return destinations.

## Retire Python rendering in stages

After the replacement pages meet parity, change these browser handlers:

| Existing handler or route | Result after cutover |
| --- | --- |
| `root_browser_entrypoint` | Serve the protected Home export |
| `user_portal` at `/me` | Redirect to `/` |
| `receipt_deep_link` at `/r/{request_id}` | Redirect to `/usage/?receipt={request_id}` once Usage supports opening that receipt |
| `status_dashboard_index` and `_render_status_html` | Serve the protected Status export |
| `admin_dashboard` at `/admin` | Redirect to `/status/` for browser sessions |
| `/admin/services`, `/admin/status`, `/admin/apps` | Redirect to `/engines/`, `/status/`, `/apps/` |
| `/admin/users`, `/admin/profiles` | Redirect to `/people/` with the People or Access plans tab selected |
| `/admin/audit`, `/admin/settings` | Redirect to `/activity/`, `/settings/` |
| `/services/` | Redirect to `/engines/` |
| `setup_page` | Serve the Setup export after the account API supports its complete flow |
| Python showcase renderer | Serve the public Showcase export after content parity |

Keep `/me/api/*`, `/admin/api/*`, public protocol routes, OIDC callbacks,
logout, and setup POST handlers intact. Preserve supported bearer-auth
automation behavior independently of browser redirects. `/docs` and
`/redoc` already use the local API reference; keep them outside the static
product route map. Do not delete `gateway/user_render.py`,
`gateway/admin_render.py`, `gateway/setup_render.py`, or the status renderer
until the rollback period has ended.

## Release and rollback gates

1. Build both artifacts from one committed release. Verify fresh installs
   without an export still boot and offer owner setup.
2. Exercise sign-in, owner claim, invite acceptance, key minting, app
   connection, and receipt deep links against an isolated gateway. Prove
   member pages never call owner APIs and cannot read another person's data.
3. Test every canonical route and slash variant, reloads, query strings,
   signed-out redirects, owner restrictions, and real 404s. Verify public
   API methods and protocol aliases still route to their existing handlers.
4. Review loading, empty, and error states at phone and desktop widths in
   both themes. Check keyboard access, axe results, and phone overflow.
5. Deploy the artifact and explicit route changes together. If navigation
   or authentication regresses, restore the prior Python handlers and `/ui`
   artifact. No database migration is required for route cutover itself.

Cutover remains blocked on the full replacement surfaces and their account
flows. A placeholder route does not establish parity. Use the repository's
safe restart procedure at deployment time; implementation lanes must not
restart the running gateway.
