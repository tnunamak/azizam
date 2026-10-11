# Web UI Steering — the design contract

**Superseded — 2026-10-10, owner-supplied audit at `fdc3c9d`:** The external
brand-package and operational-widget requirements below are historical.
Follow `docs/design/DESIGN.md` for design rules. Tokens are local in
`web/src/styles/tokens.css`. The old operational widgets are being removed.
Do not use these older requirements to restore them.

This is the canonical product information-architecture and presentation
contract for the React UI. The shared component contracts live in
`docs/design/AZIZAM_COMPONENT_LIBRARY_SPEC.md`; historical design and
implementation plans remain useful context but do not override these docs.

This document is the binding spec for the React rebuild of the gateway UI.
Agents implementing surfaces MUST follow it; deviations require editing this
doc first. It exists because a screenshot audit (2026-06-06,
the local deployment evidence directory `~/.tmp/ui-audit/`) found the
server-rendered UI uneven: two hand-crafted
surfaces were good, everything else dumped database records into HTML (raw
epoch ints as dates, `usr_…`/`cred_…` IDs as primary content, `window.confirm()`
for deletes, a dozen ad-hoc font sizes, duplicated badge/notice primitives).
The root cause was the absence of a presentation layer. This rebuild makes
that layer structural.

## Architecture

- **App**: `web/` — Next.js 15 App Router, React 19, TypeScript strict,
  Tailwind v4. `output: 'export'` (static). No SSR/Node in production: the
  gateway (FastAPI) serves the built assets; in dev, `next dev` proxies API
  routes to the gateway via `rewrites`.
- **Data**: existing JSON APIs only (`/admin/api/*`, `/me/api/*`). Same-origin
  session cookie auth + `x-azizam-admin-action: 1` +
  `x-azizam-csrf: <token>` headers on writes. NO new server rendering of
  HTML in Python for these surfaces. If an API gap blocks a surface, extend
  `gateway/admin_api.py` (JSON), never the HTML renderers.
- **Cutover**: strangler. New surfaces mount under `/ui/` (gateway
  `StaticFiles`) until parity, then take over the canonical routes. Python
  renderers are not deleted in this phase.
- **Brand**: tokens are local to Azizam. The external `@vivid-fish/brand`
  dependency and import instructions are superseded. Keep shared tokens as
  the source of color, type, radius, shadow and animation decisions.

## The presentation law (non-negotiable)

1. **No raw timestamps.** Every time value renders through `<RelativeTime>`
   ("2m ago", `title` = absolute UTC). Epoch integers in UI = build-blocking
   review failure.
2. **No raw entity IDs as primary content.** `<EntityRef name id>` renders the
   human name; the ID is a quiet copyable mono affordance (click-to-copy),
   never a table's main cell text.
3. **No raw secrets/prefixes.** `<SecretMask prefix>` → `opk_ •••• ab12`.
4. **One of each primitive.** Exactly one Badge, one Button, one input set.
   No `.badge`-vs-`.vf-pill`-style duplication ever again.
5. **No native `confirm()`/`alert()`.** Destructive actions use
   `<ConfirmDialog>`; high-impact deletes require typing the entity name.
6. **Destructive actions are demoted.** Never a column of red buttons. Rows
   get one overflow menu (⋯); destructive items live there, styled danger.
7. **Every component ships its states**: hover, focus-visible, disabled,
   loading, empty, error. A surface PR without designed empty/error states is
   incomplete.
8. **List-first IA.** A section opens with its data. "Create …" is a Button
   that opens a focused Dialog/flow. Creation forms are never permanently
   open above the list.
9. **Components are token-only.** No literal colors, font sizes, radii, or
   shadows inside components — Tailwind utilities bound to brand `@theme`
   variables only. Rebranding must be achievable by editing
   the local token file alone.
10. **Copy is product language.** Services/Apps/Keys/Profiles vocabulary per
    the operator-vocab pass; no `opk_`/`oak_`/policy-internal jargon on
    operator surfaces; sentence case everywhere ("Configured services", not
    Title Case).

## Type & spacing scale (total, enforced)

Exactly five text styles + one display, mapped to brand tokens (extend
the local token file if a slot is missing — do not invent component literals):

| Role        | Size/line     | Usage |
|-------------|---------------|-------|
| `display`   | 28/1.1 serif  | page greeting (portal only; Newsreader voice is brand identity — do not genericize) |
| `title`     | 20/1.2 sans 600 | page titles |
| `heading`   | 16/1.3 sans 600 | card/section titles |
| `body`      | 14/1.5 sans   | default |
| `small`     | 13/1.4 sans   | secondary, table cells |
| `micro`     | 12/1.3 mono   | kickers (uppercase, tracked), IDs, code |

Spacing: 4px grid only (`gap-1..gap-8`); containers max-w consistent with
current shell (1080px content column). Radius/shadows: brand tokens only.

## Component library (`web/src/components/ui/`)

Build in this order; each is small, typed, token-only, with states:

- `RelativeTime` (ts: number|string, live-updating optional)
- `EntityRef` (name, id, href?) — name-first + copyable id detail
- `SecretMask` (prefix) — scheme + dots + last4
- `Badge` (tone: neutral|ok|warn|bad|info|accent; sm?)
- `Button` (variant: primary|secondary|ghost|danger; size; loading; asChild)
- `IconButton`, `OverflowMenu` (Radix DropdownMenu)
- `Field`, `Input`, `Select`, `Checkbox`, `RadioCards`, `Switch`
- `DataTable` (typed columns; cell renderers default to the formatters; row
  overflow menu slot; loading skeleton; `EmptyState` slot; mobile card
  collapse)
- `Dialog`, `ConfirmDialog` (typed-name confirmation mode), `Sheet`
- `SectionHeader` (kicker, title, action slot)
- `EmptyState` (icon?, line, action?) — light, never a heavy bordered box
- `Toast` (one system; Radix or sonner)
- `CopyButton`

Headless behavior: Radix UI primitives (menu, dialog, switch) — accessibility
for free; styling 100% ours via tokens. Composition over boolean-prop
sprawl (see vercel composition patterns). No client state library; React
Query (TanStack) for API data (cache + optimistic revoke/disable), typed API
client in `web/src/lib/api.ts` with one `adminFetch` that injects CSRF.

## Surface spec #1 — Admin → Apps & Keys (the proving ground)

Replaces the worst audited screen. IA (list-first, one job per view):

1. **Header**: `SectionHeader` kicker "access" / title "Apps & keys" /
   action `Button` "Create app" → Dialog (shared vs personal as RadioCards,
   name, profile select; the existing POST /admin/api/apps).
2. **Apps table** (`DataTable`): columns — App (`EntityRef` name + kind
   badge), Owner (human name/email via EntityRef; never `usr_…` as text),
   Profile (name + quiet "change" action opening a Dialog, NOT an inline
   select-per-row), Keys (count), Created (`RelativeTime`), ⋯ menu (Mint
   key → Dialog with reveal-once result; Change profile; Disable →
   ConfirmDialog).
3. **Row expansion or detail panel** for an app's keys: each key row =
   label, `SecretMask`, `RelativeTime` last-used (real data), status Badge,
   ⋯ (Revoke → ConfirmDialog typed-name). The current second "Keys" table of
   raw cred ids disappears; admin automation keys get their own quiet
   subsection with the same patterns.
4. **States**: loading skeletons; empty ("No apps yet" + Create action);
   error banner with retry; optimistic disable/revoke with toast + undo
   where the API allows.
5. **Key reveal** (mint result): reveal-once Dialog matching the portal's
   pattern — masked-after-close, copy button, runnable curl, no secret ever
   logged.

Acceptance: a screenshot of this surface must read as a peer of Stripe's
API-keys page — calm rows, one accent action, zero raw IDs/epochs, states
designed. The verify loop captures desktop (1280×900) + mobile (390×844),
checks the law mechanically (no `confirm(`, no epoch regex `>1[67]\d{8}<`,
no `usr_`/`cred_` as cell text) and judges against this spec.

## Quality gates (wired, not aspirational)

- `npm run check` in `web/` = `tsc --noEmit` + ESLint (next/core-web-vitals)
  + `npx react-doctor` (threshold: no errors) + `next build`.
- react-doctor also wired as a git pre-commit gate for `web/**` (committed
  hook script + setup note), per the react-doctor-setup recipe.
- Tailwind class order via prettier-plugin-tailwindcss.
- Playwright (python, existing venv) screenshot verification per surface.
- Components must not import from `app/` (one-way dependency: app → ui).

## The honesty law (non-negotiable, added 2026-06-13)

The audited operator (self-hoster: r/selfhosted, r/LocalLLaMA) forgives a
missing feature but never a UI that lies or silently destroys their config.
Above all else:

H1. **No silent data loss.** An edit must never drop a field the operator
    didn't touch. Saving a partial form preserves the ENTIRE stored record;
    fields the form doesn't model are passed through untouched, not
    whitelisted away. (Bug of record: saving a generic service Edit form
    dropped unmodelled config fields — proven, fixed.)
H2. **Show everything the record carries.** If a service holds config keys
    or capabilities the form has no widget for, render them — read-only with
    an "edited elsewhere" note is acceptable; hiding them is not. A form that
    shows a subset misrepresents the object.
H3. **State labels must be true.** Don't say "up" for "enabled" when health
    isn't probed; say "enabled". Don't show a control implying a choice that
    doesn't exist (e.g. "Native passthrough" as a toggle on a backend where
    it's the only mode). Copy states what is, including "this is managed by
    X, not here."
H4. **No dead affordances.** A button that looks actionable does something or
    isn't there. "0 keys" either mints a key or isn't a button. Absent
    capabilities (e.g. user role editing has no API) are explained in plain
    copy, not represented by a control that fails.

## Data-driven service editing (binding)

The service editor is an operator-facing view of service-record data, not a
catalog of inference engines. It renders the generic connection, routing,
capability, and admission fields supplied by the service contract, and it
shows every other stored config field without discarding it (H1/H2).

- A service record may declare fields beyond the generic form. Render those
  fields as data with an honest edit/read-only state; preserve them on every
  partial write. Do not add a client branch, wizard, or remediation copy for a
  named engine or protocol.
- Capability labels and optional presets come from the API as data. The UI does
  not prescribe a preferred preset, synthesize a default choice, or duplicate a
  provider catalog. Add Service begins with no preset selected, renders
  loading/error/empty states explicitly, and sends only an API-supplied preset
  that the operator selected.
- Browser code never scans operator files or directories to populate a control.
  If an API has not supplied a safe, explicit choice set, use the value already
  stored or a plain validated text field. Do not imply discovery exists.
- A derived runtime artifact may be displayed only as generic, read-only
  diagnostic data. It is not a configuration editor and does not create a
  special service type in the UI.

## Audit & sign-in honesty fixes (binding, 2026-06-13)

Two deferred trust-breakers from the UX audit, both honesty-law violations:

**Audit — filtering must not silently truncate (H1/H3).** The audit UI uses
server-side filters and keyset pagination. Filtering by an actor searches the
whole log, not a client-loaded window. The API contract is:
- `GET /admin/api/audit` gains optional server-side filters `actor_id`,
  `action_prefix` (the family, e.g. `service.`), `before` (keyset cursor =
  the `created_at` of the last seen row; the schema already indexes
  `created_at`). Returns `{records, next_cursor}`; `next_cursor` null when
  the result is exhausted FOR THE CURRENT FILTER. Keep `limit` (page size),
  drop the 200 hard ceiling for pagination (page through, don't cap).
- Filter chips drive the server query, not a client slice. "Load more"
  follows `next_cursor`. When no cursor remains, say "End of audit log"
  (or "No more matching events" under a filter) — never an ambiguous empty
  tail. The chips therefore search the WHOLE log, not a 200-row window.

**Sign-in — OIDC group→role setup must be completable (H4).** The Configure
sign-in dialog lacks a `groups_claim` field, so an operator wiring
group-based roles via Users → profile mappings has no way to tell the
gateway which token claim carries groups — a dead-end setup that looks
complete. Add `groups_claim` to the OIDC config (form field + GET/PUT,
default `groups`), with help copy linking the concept to the Users
mapping flow. Verify GET still never returns the client secret (H: secrets
stay write-only). If the backend OIDC model lacks the field, add it
(additive, defaulted) so existing configs are unaffected.

## Admin information architecture (binding)

Eight flat, noun-named sections (Tailscale-calm; no nesting, no Title Case).
**Status is first and read-first**: it answers the operational question before
an operator enters a configuration surface.

| Route (`/ui/admin/…`) | Nav label | One job |
|---|---|---|
| `/status` | Status | "What is true now, and what needs diagnosis?" |
| `/` (overview) | Overview | "What needs my attention next?" |
| `/services` | Services | The upstream backends this gateway fronts |
| `/apps` | Apps & keys | Who can call in (DONE — surface spec #1) |
| `/profiles` | Profiles | What callers are allowed to reach |
| `/users` | Users | The people, and default access for new ones |
| `/audit` | Audit | Who changed what, when |
| `/settings` | Settings | Sign-in, extensions, gateway-wide config |

**Responsive shell** (`web/src/app/admin/layout.tsx`): the only breakpoint
bands are **Phone `<640px`**, **Compact `640px–979px`**, and **Desktop
`≥980px`**. Desktop has the fixed 240px left sidebar on `vf-surface-1`
(content remains on `vf-bg`), with wordmark, micro "admin" kicker, full
labelled navigation, signed-in email, "Open portal", and "Sign out". Compact
uses the same one-pane content order with labelled navigation in a Sheet or
visible rail; selected detail is a Sheet, never a squeezed second column.
Phone uses the top bar plus labelled primary navigation and a reachable More
menu for every non-primary destination. Nothing may be omitted merely because
the viewport is narrow. All controls remain at least 44px, filters wrap, and
there is no horizontal page overflow. Content uses `max-w-[1080px]`; every page
starts with `SectionHeader`. Rhythm remains section gap-8 and intra-card gap-4;
tables use `small` text with relaxed row height. Prefer spacing and single
hairline rules (`vf-border`) over card borders everywhere.

**Canonical route and link contract:** `/admin/status` and
`/admin/status/` are authenticated browser routes. Each redirects to the
canonical React Status route (`/ui/admin/status/`) while preserving the
complete query string (for example,
`/admin/status/?service=svc_1` → `/ui/admin/status/?service=svc_1`). The same
query-preserving canonicalization applies to every trailing-slash admin section
route. `adminStatusHref(serviceId?)` is the diagnostic link builder and
produces the Status route, optionally focused on a service.
`adminServiceHref(serviceId)` is the configuration link builder and produces
the Services route. `adminTargetHref(targetKind, targetId)` is the one shared
configuration-target mapping; its service mapping is `adminServiceHref`, never
`adminStatusHref`. Do not hand-build competing entity URLs in a surface.

**Attention target contract:** a target is exactly the tuple
`target_kind + target_id`, with optional display-only `target_name`. `href` is
a compatibility field only and, when a target exists, must equal
`adminTargetHref(target_kind, target_id)` exactly. Do not emit or consume
`entity_kind`, `entity_id`, `entity_name`, a second ID, or a section-derived
target. A system advisory has no target tuple, no `target_name`, and no `href`.
Services consumes the canonical `service` target by exact stable `service.id`;
it never normalizes or falls back to a display/config name. Any server-side
legacy route contract uses a separately named parameter.

**Health and principals truth:** Status, Overview, Services, shell status, and
attention all consume one current health projection. It separates configured
(`enabled`), expected role, and observed
(`up`/`degraded`/`down`/`unavailable`) state and routability. An unavailable observation is
never green and is not an empty healthy state; an empty configured-service list
is also not an all-clear. People and service principals are separate kinds.
Users shows only people; automation is named and counted as service principals
in its own context, never fabricated as a person.

**Per-surface specs** (each list-first; create/edit = Dialog; destructive =
overflow + ConfirmDialog; every state designed):

- **Status**: read-only diagnosis first. The current health projection supplies
  the health band, service observations, and current advisories. A focused
  service uses `adminStatusHref(serviceId)`; a configuration action is an
  explicit `adminServiceHref(serviceId)` link. Status never edits service
  records, invents history, or substitutes a green/empty claim when live
  observation is unavailable.
- **Overview**: attention from `GET /overview`, optional quickstart, real
  metrics, selected list/context, and recent activity. Its links use the
  canonical attention target contract. It summarizes Status but does not
  define a second health model or defer diagnosis to a later Status surface.
- **Services**: DataTable — Service (EntityRef name; provenance badge is
  history, not permission), Endpoint (host:port, mono small),
  Capabilities (human-labelled badge row), current observation, Enabled, and
  overflow actions. This is the configuration surface; current diagnosis lives
  on Status. The registry owns all ordinary service records after initial
  import. A record whose fields are genuinely derived may lock only those
  fields with a one-line explanation; it remains data-driven, not a named
  engine exception. Add/Edit exposes API-provided generic fields and preserves
  unknown data. It never scans operator files, assumes a default selector, or
  carries a client-side engine catalog. Show "Saved — changes apply live" only
  when the API says the change has applied.
- **Profiles**: DataTable — Profile (EntityRef), Access summary (e.g.
  "4 models · chat, images" — derived, human), Apps using it (count), ⋯
  (Edit, Duplicate?, Delete → typed-name ConfirmDialog; block/explain when
  apps still reference it). Create/Edit Dialog mirrors the existing profile
  form fields (models, workflows, protocols) progressively disclosed.
  "Test access" disclosure wired to `POST /policy/simulate`: pick app +
  model → allowed/denied with the actual reason. This is a trust feature —
  make it visible but quiet.
- **Users**: DataTable — Person (EntityRef name/email), Role badge, Apps
  (count), Created (RelativeTime). Service principals are not rows in this
  table; show their separate count and named automation context only where it
  answers an access or audit question. Below, a "Default access" group:
  user-access-defaults (PUT) + runtime profile mappings (list + create/
  disable) — these answer "what does a brand-new user get?"; copy must say
  exactly that. No user CRUD (identities come from sign-in) — say so in the
  empty/explainer copy.
- **Audit**: reverse-chron DataTable — When (RelativeTime), Actor (EntityRef),
  Action (sentence-case human phrase mapped from the event type, micro mono
  raw type as the quiet detail), Target (EntityRef). Filter chips drive
  server-side `actor_id` and `action_prefix` filters. Row → Sheet with the
  full record (mono JSON, CopyButton). Keyset-paginated
  `GET /audit?limit=&actor_id=&action_prefix=&before=`, newest first, "Load
  more" until the filtered result is exhausted.
- **Settings (historical surface plan, superseded 2026-10-10)**: the old
  operational widgets are being removed. The Sign-in, Extensions and Media
  admission widget plan is not a requirement to restore removed controls.

**Shared-file discipline for parallel agents**: the shell owns `layout.tsx`
+ nav; surface agents touch ONLY their own `web/src/app/admin/<section>/`
directory. Library gaps are reported, never patched ad hoc; `components/ui`
is frozen during a fan-out.

## Later (explicitly out of this increment)

Historical plans included portal surfaces, absorbing legacy status rendering
into `web/`, and deleting Python renderers. Promotion of `ui/` to
`@vivid-fish/brand/react` is superseded; tokens stay local. The following
cutover plan is historical:

canonical-route cutover (per-area flips: `/admin` first, then
`/me`; `basePath` `''` + gateway serves export at canonical routes +
`/ui/*` redirects).
