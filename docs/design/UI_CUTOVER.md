# Product UI cutover

The browser front door is the single React app under `/ui/`. The gateway
serves the existing Next.js static export from `web/out`; production does
not need a Node server. Build with `cd web && npm run build`. Retain
`basePath: "/ui"`, `assetPrefix: "/ui"`, and `trailingSlash: true`.

This replaces the earlier plan for a second export mounted at `/`.
An HTML-accepting request to `/` redirects to `/ui/home/`. Requests without
`text/html` in Accept retain the previous root response. No catch-all
static mount is registered at `/`, so protocol routes keep their handlers
and unknown paths remain 404. A source checkout without `web/out` still
boots and offers `/setup`; browser redirects do not depend on an export.
The export must be built and shipped with the release to use product pages.

## Browser route map

All compatibility redirects use relative Locations, preserve query strings,
and bypass absolute HTTP redirects behind TLS termination. Both slash
variants are supported for the legacy pages listed below. Browser entry
redirects use `Cache-Control: no-store`.

| Legacy entry | Product destination |
| --- | --- |
| `/` with HTML Accept | `/ui/home/` |
| `/me` | `/ui/home/` |
| `/admin`, `/admin/overview`, `/ui/admin`, `/ui/admin/overview` | `/ui/home/` |
| `/admin/services`, `/ui/admin/services` | `/ui/engines/` |
| `/admin/status`, `/ui/admin/status` | `/ui/status/` |
| `/admin/apps`, `/ui/admin/apps` | `/ui/apps/` |
| `/admin/profiles`, `/ui/admin/profiles` | `/ui/people/?tab=plans` |
| `/admin/users`, `/ui/admin/users` | `/ui/people/` |
| `/admin/audit`, `/ui/admin/audit` | `/ui/activity/` |
| `/admin/settings`, `/ui/admin/settings` | `/ui/settings/` |
| `/status` with HTML Accept and no Authorization | `/ui/status/` |
| `/firehose` | `/status/raw` (301) |

The Profiles redirect prepends `tab=plans`, followed by the unchanged
incoming query text. Unknown `/ui/admin/*` pages remain 404 after the owner
check; they do not become Home. `/admin/*` and `/ui/admin/*` aliases retain
the existing owner/admin authorization checks. Canonical owner static pages
(Status, Engines, Providers, People, Activity and Settings) have the same
server guard. The shared app shell determines the person's visible navigation.

## Retained compatibility and technical details

Every `/me/api/*`, `/admin/api/*`, and protocol API keeps its route and
method. Logout, setup POST handlers, OIDC callbacks, `/docs`, and `/redoc`
remain outside the static route map. Bearer-authenticated `/admin` retains
its Python admin response for existing automation; it does not redirect.
The Python portal renderer remains referenced by non-HTML root clients
and `/r/{request_id}` receipt links. Those links keep their existing receipt
sheet until Usage supports selecting a receipt from the query string.

`/status/raw` and `/status/raw/` render the Python operator firehose and
require owner/admin authentication. Status exposes this as “Technical
details.” Non-browser `/status` and bearer-authenticated `/status` retain
the previous status renderer. The renderer's owner guard also protects the
new raw route. The renderer modules remain for these supported paths and
rollback; the old React `web/src/app/admin/*` surfaces are retired.

## Sign-in return destinations and OIDC hosts

Built-in sign-in and OIDC carry a validated same-origin return path through
authentication. New-app paths, query strings and receipt destinations are
retained. Legacy browser aliases are translated to their product destinations.
External URLs, scheme-relative URLs and malformed paths are rejected. The
default destination after sign-in is `/ui/home/`.

Settings owns the configured public base URL. When it is set, OIDC uses that
URL's host and scheme for its configured callback path. Without it, the
explicit OIDC redirect URI remains authoritative. An OIDC flow started on
`openai.vivid.fish` can therefore return on `ai.vivid.fish` when Settings
selects that canonical host. Operators must register
the resulting exact redirect URI with their identity provider. Local sign-in
uses same-origin requests and does not move between hosts.

## Release checks and rollback

Docker builds the UI in a pinned Node build stage and copies only the static
export into the Python image. Bare Python installations must run `npm --prefix
web ci` and `npm --prefix web run build` before starting the gateway.

Build and ship `web/out` from the same committed release as the Python
route changes. Test anonymous root → sign-in → Home and owner `/admin` →
Home with Gateway navigation against an isolated gateway. Exercise browser
aliases, queries, owner restrictions, receipt links, and missing exports.
Run `scripts/run-hermetic-pytest.sh -q`, the web checks and Playwright.
Regenerate `docs/SUPPORT_MATRIX.md` after route changes.

The route cutover itself needs no database migration. At deployment, follow
`scripts/safe-restart` and `docs/ops/SAFE_RESTART.md`; implementation lanes
must not restart the running gateway. If navigation or sign-in regresses,
restore the prior committed Python handlers and matching `/ui` export.
