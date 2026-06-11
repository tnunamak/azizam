# Web UI Steering — the design contract

This document is the binding spec for the React rebuild of the gateway UI.
Agents implementing surfaces MUST follow it; deviations require editing this
doc first. It exists because a screenshot audit (2026-06-06,
`~/.tmp/ui-audit/`) found the server-rendered UI uneven: two hand-crafted
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
- **Brand**: `@vivid-fish/brand` (local path dep on `~/code/vivid-fish-brand`,
  Tailwind v4 peer) is the ONLY source of color, type, radius, shadow,
  animation tokens. Root stylesheet: `@import "tailwindcss";
  @import "@vivid-fish/brand";`. Token gaps are fixed UPSTREAM in the brand
  package, never with local literals.

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
   `vivid-fish-brand/src/tokens.css` alone.
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

## Later (explicitly out of this increment)

Portal surfaces, remaining admin sections, status page redesign, absorbing
`status-page/` into `web/`, promoting `ui/` to `@vivid-fish/brand/react`,
deleting Python renderers, canonical-route cutover.
