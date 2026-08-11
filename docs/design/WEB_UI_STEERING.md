# Web UI Steering — the design contract

This is the canonical product information-architecture and presentation
contract for the React UI. The shared component contracts live in
`docs/design/VIVID_FISH_COMPONENT_LIBRARY_SPEC.md`; historical design and
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
  session cookie auth + `x-ai-gateway-admin-action: 1` +
  `x-ai-gateway-csrf: <token>` headers on writes. NO new server rendering of
  HTML in Python for these surfaces. If an API gap blocks a surface, extend
  `gateway/admin_api.py` (JSON), never the HTML renderers.
- **Cutover**: strangler. New surfaces mount under `/ui/` (gateway
  `StaticFiles`) until parity, then take over the canonical routes. Python
  renderers are not deleted in this phase.
- **Brand**: `@vivid-fish/brand` (a local path dependency in this deployment,
  with a Tailwind v4 peer) is the ONLY source of color, type, radius, shadow,
  animation tokens. Root stylesheet: `@import "tailwindcss";
  @import "@vivid-fish/brand";`. Token gaps are fixed UPSTREAM in the brand
  package, never with local literals. The package name and token contract are
  portable; the dependency checkout path is deployment-specific.

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
   the brand package's `src/tokens.css` alone.
10. **Copy is product language.** Services/Apps/Keys/Profiles vocabulary per
    the operator-vocab pass; no `opk_`/`oak_`/policy-internal jargon on
    operator surfaces; sentence case everywhere ("Configured services", not
    Title Case).

## Type & spacing scale (total, enforced)

Exactly five text styles + one display, mapped to brand tokens (extend
tokens.css upstream if a slot is missing — do not invent locals):

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
    whitelisted away. (Bug of record: saving the generic service Edit form
    for ComfyUI dropped `workflows_dir`/`video_workflows_dir`/`video_timeout`/
    `default_model` — proven, fixed.)
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

## Protocol-aware service editing (binding)

A service's editable shape depends on its `protocol`. The generic
OpenAI-compatible form is wrong for ComfyUI and any non-LLM backend.

- **Field schema is keyed on protocol.** `openai-compatible` →
  connection + LLM capabilities + models map (today's form). `comfyui`
  (and future non-LLM protocols) → a protocol section with that backend's
  real fields as first-class inputs: `workflows_dir`, `video_workflows_dir`,
  `video_timeout`, `default_model` (default_model as a Select populated by
  scanning the workflow dir — a free-text model with no matching
  `<name>.json` is a latent 404, so guide the choice). The backend's
  `service_form_to_raw`/validator must know each protocol's keys so they
  round-trip instead of dropping (H1).
- **Capability vocabulary is complete.** `generations` and `videos` join
  the canonical capability list (served from `/operator/service-presets`);
  no capability a record can hold is missing a checkbox.
- **Out of scope (correctly):** editing/uploading/previewing the workflow
  JSON files themselves, or a model catalog browser. ComfyUI's own node
  editor owns the graphs; this gateway points at the directory and picks a
  default. Say so in the form.
- **Honest passthrough copy:** for non-`openai-compatible` protocols,
  native passthrough is stated as the mode, not offered as a toggle.

## Audit & sign-in honesty fixes (binding, 2026-06-13)

Two deferred trust-breakers from the UX audit, both honesty-law violations:

**Audit — filtering must not silently truncate (H1/H3).** Today the audit
endpoint takes only `limit` (hard-capped at 200) and the UI filters the
loaded window client-side. Filtering by an actor whose matching events are
older than the 200 newest rows shows a confident, incomplete result that
reads as "no more matches" — deception by omission. Fix:
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

Seven flat, noun-named sections (Tailscale-calm; no nesting, no Title Case):

| Route (`/ui/admin/…`) | Nav label | One job |
|---|---|---|
| `/` (overview) | Overview | "Does anything need me?" |
| `/services` | Services | The upstream backends this gateway fronts |
| `/apps` | Apps & keys | Who can call in (DONE — surface spec #1) |
| `/profiles` | Profiles | What callers are allowed to reach |
| `/users` | Users | The people, and default access for new ones |
| `/audit` | Audit | Who changed what, when |
| `/settings` | Settings | Sign-in, extensions, gateway-wide config |

**Shell** (`web/src/app/admin/layout.tsx`): desktop ≥960px — fixed left
sidebar 240px on `vf-surface-1` (content column stays on `vf-bg`): wordmark
(text, brand voice) + micro kicker "admin" on top; nav items (icon + label,
active = accent text + subtle accent-tinted background, `rounded-vf-sm`);
bottom = signed-in email (small, muted), quiet links "Open portal" (`/me`)
and "Sign out". Mobile — top bar (wordmark + menu IconButton opening the
nav in a `Sheet`); content full-width. Content column: `max-w-[1080px]`,
generous whitespace (px-6 md:px-10, py-8/12); every page starts with
`SectionHeader`. Rhythm: section gap-8, intra-card gap-4; tables use
`small` text with relaxed row height. No card-borders-everywhere: prefer
spacing and single hairline rules (`vf-border`) over boxes.

**Per-surface specs** (each list-first; create/edit = Dialog; destructive =
overflow + ConfirmDialog; every state designed):

- **Overview**: attention list from `GET /overview` (single source:
  `derive_admin_attention`) — each item one row: tone dot/badge, plain-language
  line, action Button-link to the owning section. Empty = "All quiet." (light
  EmptyState, no box). Below: one quiet stat strip (services up/total, apps,
  active keys — text, not dashboard tiles) and the 3 most recent audit rows
  with "View all" → Audit. NOTHING else; resist dashboard-itis.
- **Services**: DataTable — Service (EntityRef name; PROVENANCE badge
  `config`/`ui`/`managed` — provenance is history, NOT permission), Endpoint
  (host:port, mono small), Capabilities (human-labeled badge row), Status
  (availability incl. overrides), Enabled (Switch), ⋯ (Edit, Models, Test
  connection, Detect models, Remove). EDITABILITY LAW: the DB registry owns
  every service after the one-time config import, so ALL rows are fully
  editable and removable regardless of source. Sole exception: `managed`
  substrate rows (litellm) — the gateway spawns that process, so endpoint/
  protocol are derived (locked with one-line explanation in the dialog);
  rename/enable/note only; no Remove (disable instead). Removing a
  config-imported row uses typed-name ConfirmDialog with provenance-aware
  copy ("imported from your config file; your YAML is not modified and will
  not re-import it"). **Models editor**: per-service dialog listing model
  mappings (public name → upstream name) with add/remove/edit rows — this is
  what makes a backend's models routable; never read-only. "Add service"
  Dialog: preset Select with "Custom (OpenAI-compatible)" FIRST (presets
  served from GET /admin/api/operator/service-presets — never duplicated
  client-side), name, base URL, api-key ENV-REF field (never secret values),
  protocol + capabilities in collapsed advanced. Honest apply: banner
  "Saved — restart the gateway to apply" after writes. LiteLLM substrate
  config = read-only mono viewer behind a disclosure (genuinely derived).
- **Profiles**: DataTable — Profile (EntityRef), Access summary (e.g.
  "4 models · chat, images" — derived, human), Apps using it (count), ⋯
  (Edit, Duplicate?, Delete → typed-name ConfirmDialog; block/explain when
  apps still reference it). Create/Edit Dialog mirrors the existing profile
  form fields (models, workflows, protocols) progressively disclosed.
  "Test access" disclosure wired to `POST /policy/simulate`: pick app +
  model → allowed/denied with the actual reason. This is a trust feature —
  make it visible but quiet.
- **Users**: DataTable — User (EntityRef name/email), Role badge, Apps
  (count), Created (RelativeTime). Below, a "Default access" group:
  user-access-defaults (PUT) + runtime profile mappings (list + create/
  disable) — these answer "what does a brand-new user get?"; copy must say
  exactly that. No user CRUD (identities come from sign-in) — say so in the
  empty/explainer copy.
- **Audit**: reverse-chron DataTable — When (RelativeTime), Actor (EntityRef),
  Action (sentence-case human phrase mapped from the event type, micro mono
  raw type as the quiet detail), Target (EntityRef). Filter chips above the
  table client-side (actor, action family). Row → Sheet with the full record
  (mono JSON, CopyButton). `GET /audit?limit=`, newest first, "Load more".
- **Settings**: grouped cards with SectionHeaders, in order: Sign-in (OIDC
  config — status line + edit Dialog; never render secrets), Extensions
  (package list + validate/enable flow), Media admission (the existing
  override fields, explained in product language). Each group is independent;
  no global save button.

**Shared-file discipline for parallel agents**: the shell owns `layout.tsx`
+ nav; surface agents touch ONLY their own `web/src/app/admin/<section>/`
directory. Library gaps are reported, never patched ad hoc; `components/ui`
is frozen during a fan-out.

## Later (explicitly out of this increment)

Portal surfaces, status page redesign, absorbing `status-page/` into
`web/`, promoting `ui/` to `@vivid-fish/brand/react`, deleting Python
renderers, canonical-route cutover (per-area flips: `/admin` first, then
`/me`; `basePath` `''` + gateway serves export at canonical routes +
`/ui/*` redirects).
