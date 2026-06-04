# Full Design Pass — Plan (for approval)

Status: proposed, awaiting Tim's approval before execution.
Date: 2026-06-03.

This plan covers a systematic design pass over the whole gateway UI — every
screen, at **mobile and desktop** — implemented in the existing server-rendered
Python stack, consuming the `@vivid-fish/brand` tokens, and built to a
**migration-safe seam** so the work is never walked back if the UI is later
ported to Next.js/React.

Source of truth for the design: `docs/design/vivid-fish-handoff/` (the Claude
Design handoff). Authoritative files: `gateway/atoms.jsx`, `screens-user.jsx`,
`screens-admin.jsx`, `screens-admin-v2.jsx`, `brief.jsx`.

---

## Governing principles (non-negotiable for every screen)

1. **Migration-safe seam: pure-data snapshot → JSON endpoint → view.**
   Every screen renders from a JSON-serializable snapshot built by a pure
   function reading server state in-process, exposed at a stable endpoint. No
   business logic or data computation inside the HTML renderers. This is the one
   rule that guarantees a later Next.js view can consume the same endpoints with
   zero backend rewrite. The user portal already proves it
   (`build_user_portal_snapshot` → `/me/api/session`); the admin snapshot
   (`ADMIN_STORE.dashboard_snapshot` + `_admin_*` enrichers) must get its own
   JSON endpoint (`/admin/api/overview`) as an explicit deliverable.

2. **Mobile-first, desktop expands.** The phone layout is canonical; desktop is
   the same components with more columns. Per the design: bottom-tab nav on
   mobile, sidebar on desktop; sheets not modals; cards over tables on narrow
   screens; generous touch targets (>=44px). Not a desktop layout with a thin
   `@media` afterthought (which is what exists today: ~6 media queries total).

3. **Brand as data.** Consume `@vivid-fish/brand` tokens (its `tokens.css` is
   plain CSS vars identical to our `SHELL_CSS` palette). Vendor/import the token
   layer so we are a real consumer of the brand package, not a copy. Deployment
   brand stays config (already done: `admin.brand.wordmark`).

4. **Honesty + progressive disclosure.** Lead with data/actions; explanation on
   demand. No silent defaults, no fake compatibility, no jargon in the decision
   surface. (Already the pattern after the admin-nav + key-reveal work.)

5. **Verify every screen live, at both breakpoints**, via the fresh-instance
   harness + headed browser, before marking it done. Keep the render-test
   contract green; update tests deliberately, never silently.

---

## Phase 0 — Shared mobile foundation (do first; everything rides on it)

The systemic gap is that there is no mobile foundation — only desktop layouts
with thin media queries. Build the shared shell the design specifies so every
later screen inherits it instead of re-solving mobile per screen.

- Responsive app shell in `gateway_shell.py`: bottom-tab nav on mobile (Home /
  Keys / What works / Activity / You|Admin per the design `Tabs`), sidebar/header
  on desktop. One shell, both portals.
- Brand token import layer (consume `@vivid-fish/brand` tokens).
- Mobile primitives the design uses that we lack: sheet/drawer pattern,
  cards-over-tables helper, touch-target sizing pass on `.vf-btn`/inputs.
- Acceptance: a representative screen (Home) is fully correct at 390px and
  1280px with the new shell; no horizontal scroll, tap targets >=44px.

## Phase 1 — User portal (the brief's "Week 1: shell + first key")

Most of this exists; this phase is mobile + design fidelity, not greenfield.

- **Sign in / door** (`_signed_out_hero`): the design's warm sign-in at both
  breakpoints. (Dead-end already fixed; this is fidelity + mobile.)
- **Home** (`_signed_in_account`, `_health_card`): "your gateway" + health card +
  app cards with status dots, mobile card layout. (Exists; mobile + polish.)
- **Create App + Key**: the design's task-shaped flow (name → access → reveal).
  Auto-mint reveal already done; this adds the explicit named-step flow for
  *additional* keys (design screens 04–06) and makes it mobile-correct.
- **App / Key detail** (design 07): "what this key can do" in plain language,
  copy + revoke, recent activity. NEW screen.
- Data seam: all from `/me/api/session` + per-credential endpoints (mostly exist).

## Phase 2 — Reading the gateway (the brief's "Week 2")

- **What works / Capabilities** (design 08): health + per-modality model list,
  folded sensibly on mobile. Backed by `/gateway/v1/capabilities`.
- **Activity + Receipt detail** (design 09–10): the "why did this fail" screen —
  the most valuable debugging surface. Backed by receipts endpoints.
- These are largely NEW user-facing surfaces (today they live only in admin).

## Phase 3 — Admin, per section, mobile + contents

Sidebar nav + prose cut already done. This phase redesigns each section's
**contents** at both breakpoints, in the design's IA:

- Apps & Keys, Profiles (template-first editor + JSON escape hatch),
  Identity mappings, Services, APIs, Admin Keys, Activity.
- Admin overview tightened per the brief (cut the stat-grid dashboard-itis; the
  "things that want a look" list is the screen).
- Deliver `/admin/api/overview` JSON endpoint (the seam deliverable).
- Mobile: tables → cards, sidebar → bottom-tabs/drawer.

## Deferred (per the brief's own cuts — not in this pass)

- Policy simulator UI (brief: cut for v1).
- Dedicated activity feed as a separate dashboard (folded into Home "recent").
- People/Invite/User-detail admin screens (design has them; lower priority for a
  single-operator deployment — revisit if multi-user grows).

---

## Process

- One phase at a time. Within a phase, one screen at a time: build mobile +
  desktop, verify live at both breakpoints, keep tests green, show Tim.
- Phase 0 is the gate — nothing else starts until the mobile foundation is real.
- Each completed screen updates this doc's checklist.
- Stack stays server-rendered Python; the seam keeps a future Next port open
  (LiteLLM deployment-mode and any UI-stack migration remain separate, open
  questions tracked elsewhere — not part of this pass).

## Phase 1 build order (synthesized from 4-agent spec fan-out, owner-edited)

Owner overrides applied to the agents' specs (SLVP + honesty):
- **No fabricated data.** The agents suggested hardcoding sample activity rows
  on Home and a "What works" feed. Rejected — those are Phase 2 (real data). If
  per-key activity isn't available, show an honest "No activity yet", not fake
  rows.
- **Key-detail = a sheet, not a new page route.** Reuse the `.vf-sheet` Phase-0
  primitive; no new routing/history. Matches the design's "sheets not modals".
- **"What this key can do" sentence = derived** from the profile via the
  existing `_profile_access_summary()` helper; do not store a new field.
- **Sign-in copy stays brand-neutral.** The design headline hardcodes "Tim's
  gateway" — use the wordmark/brand instead (brand-is-data).
- **Create-Key = 2 steps** (name → reveal-once); the v0.2 design cut the
  profile-pick step. Auto-mint already covers the first key; the sheet flow is
  for *additional* keys.

Order: Home (anchor) → Sign-in (fidelity) → Create-Key sheet → Key-detail sheet.
Each: build, verify live at 390px + 1280px, keep/upgrade tests, then next.

## Checklist

- [x] Phase 0 — shared mobile foundation (shell, tabs, brand tokens, sheets)
      + admin mobile hardening (tables scroll, 44px nav).
- [x] Phase 1 — user portal, VISUALLY verified (actual screenshots, not just
      DOM) at 390px + 1280px. The screenshot pass found and fixed real defects
      the DOM tests had missed:
      - **Double-nav bug (fixed):** on mobile the header section links
        (Home/Admin/Status) AND the bottom tabs both showed — redundant. Now
        `body.has-tabbar .shell-nav-section` is hidden ≤720px (header keeps only
        the logout control); desktop keeps the full header nav, no tab bar.
      - **Home app-card reshaped (was button-heavy → calm):** the card used to
        carry two red Delete buttons + an open create-key form + "View access"
        on the surface (read like an admin panel). Now the key is a calm,
        tappable row (glyph · name · masked prefix · chevron) that opens the
        detail sheet; "+ New key" and "Remove this App" are quiet disclosures.
        Matches design screen 03. No "last used" time (not recorded — honest).
      - **Key-detail sheet reworked (was a 32-card wall → grouped summary):**
        dumped every backend model (with duplicates). Now leads with a calm
        serif sentence + modality pills with counts (CHAT ×4, IMAGES ×10, …)
        and tucks the full de-duplicated item list behind "Show all N items".
        Matches design screen 07. Honest "Per-key activity isn't recorded yet."
      - Sign-in door + Create-Key reveal sheet: verified good as-was.
      - No fabricated data anywhere. 98 render/shell + 61 integration green.
      - **Desktop note:** design's DT-1 Home is a 2-col (1.4fr 1fr) layout whose
        right column is the "Lately" activity feed — that needs real activity
        data (Phase 2). Phase-1 desktop stays a single calm column; the 2-col
        lands with Phase 2 rather than fabricating an activity panel now.
- [x] Phase 2 — capabilities, activity, receipt detail, all on REAL owner-scoped
      data, verified live at 390px + 1280px:
      - **Capabilities** folded into the Home health card (design SLVP call, not
        a separate tab): one `<details>` card shows "N capabilities · M kinds of
        model" + reachability, expands to plain-language per-modality rows
        (Chat/Text/Images/Audio/Video ×count) from `/gateway/v1/capabilities`.
        Honest empty state points at Admin.
      - **Recent activity** = a "RECENT" feed on Home from the NEW owner-scoped
        `/me/api/activity` (derives the user's app_ids server-side; never leaks
        other users' calls). Rows: dot · model · endpoint · status · latency,
        status-toned. Honest empty state ("No calls yet"), no fabricated rows.
      - **Receipt detail** = an ADDRESSABLE route `/r/{request_id}` rendering as
        the `.vf-sheet` (mobile) / dialog (desktop). The "why did this fail"
        screen: serif summary + a tinted "Why" block (rejected_inputs,
        fallback_reasons, warnings) + labelled facts. Shareable URL: back button
        closes + restores `/`; a pasted `/r/:id` opens on cold start. Backed by
        owner-scoped `/me/api/receipts/{id}` (404 on non-owned, client audience
        strips api_key_name/app_id/metadata).
      - Data-plane prerequisite (reviewed by data-integrity-guardian): receipts
        gained an `app_id` column (schema v1→v2 recreate) threaded through
        RuntimeIdentity + the 3 receipt-write sites, so the feed can be scoped by
        an unforgeable owned-app id. 1380 tests green.
- [x] Phase 3 — admin, verified live at 390px:
      - **Seam delivered:** `/admin/api/overview` JSON now exposes an
        `attention` list (the migration-safe contract). Pure function
        `derive_admin_attention(snapshot)` in `admin_api.py` backs BOTH the JSON
        and the HTML overview — single source of truth.
      - **Overview redesign (cost lens):** the apps-keys landing leads with
        "things that want a look" — a ranked list (problems then attention) of
        Apps that are disabled / have no profile / profile missing / no active
        key, and services that are down/degraded. Each row jumps to the section
        that fixes it. Empty = one calm "Everything looks healthy" line, not a
        green stat grid.
      - **Mobile (tables→cards):** the admin no longer scrolls the PAGE
        sideways. Wide tables drop their grid on ≤760px — header row hidden,
        each `tr` becomes a stacked card; the sidebar nav scroller is clipped
        (`min-width:0` grid chain); `.admin-content`/`.admin-shell` get
        `overflow-x:hidden` as a backstop. Verified 0 page h-scroll across
        apps-keys/services/apis/identity.
      - 1384 tests green.
