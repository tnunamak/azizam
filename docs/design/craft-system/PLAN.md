# Craft system: application plan

Spec: `../DESIGN.md`, "System numbers v2". Evidence: `MEASUREMENTS.md`. Paths below are relative to `web/src/` unless they start with `scripts/` or `docs/`.

**Headline.** One PR changes the tokens and the shared components and adds a gate that measures the numbers. Eight surface PRs then adopt them, each one recaptured and checked against the numbers before it is judged. Surfaces adopt in the order that proves each template on a simple page first (Settings, People) before applying it to the pages the judges said "don't hold up" (Home, Library). Most of the gap is cross-cutting, so the first PR alone should remove four of the six control heights and the 44 px leak on every page.

**The page's job comes first.** When a measured rule and the page's job conflict, the job wins. Amend the rule in this document and in `../DESIGN.md` with the evidence: Usage must distinguish tasks, and workflow rows must show what each choice needs and makes (set-10, both judges).

## PR 1: tokens, shared components, gate (no page layouts)

Branch `design/craft-tokens`. It changes no page's layout on purpose. Pages change only where they already use the shared parts. Visual diffs are expected on every route. Snapshot and e2e selectors stay stable.

### Tokens: `styles/tokens.css`

1. **Type roles** in `@theme inline`, using Tailwind v4's `--text-*--font-weight` and `--text-*--letter-spacing`:
   - `--text-title` 1.5rem / 2rem, 600, −0.015em
   - `--text-verdict` 1.25rem / 1.75rem, 600
   - `--text-heading` 1rem / 1.5rem, 600
   - `--text-body` 0.9375rem / 1.375rem
   - `--text-meta` 0.875rem / 1.25rem
   - `--text-label` 0.8125rem / 1.125rem, 500 (new: the missing 13 step)
   - `--text-badge` 0.75rem / 1rem, 500
   - `--text-figure` 1.5rem / 2rem, 600

   Keep `text-xs` through `text-4xl` as they are. They become deprecated aliases.
2. **Spacing:**
   - `--page-pad` (2rem, with 1.5rem and 1.25rem set by media query outside the theme)
   - `--page-top` 2rem
   - `--content-read` 720px
   - `--space-section` 2rem, `--space-heading` 0.75rem, `--space-title` 1.5rem, `--space-stack` 1rem
3. **Rows and controls:**
   - `--row-1` 2.5rem, `--row-1-dense` 2.25rem, `--row-2` 3.75rem, `--row-setting` 4rem
   - `--row-phone-1` 3rem, `--row-phone-2` 4rem
   - `--control-sm` 2rem, `--control-md` 2.5rem, `--control-touch` 2.75rem
4. **Radius:** `--radius-card` 10px → **8px**; a new `--radius-badge` 4px with a theme alias `--radius-badge`.
5. **Charts:** `--chart-1` (= `--primary` in light, `--link` in dark), `--chart-2`, `--chart-3` (two quieter teals or neutrals that pass 3:1 on the card), and `--chart-other` (= `--neutral`). Add them to `scripts/ui/state_palette.py`'s 3:1 fill check.
6. Remove `--vf-text-md` (17 px, unused by the new system) only after `rg vf-text-md` is empty.

### Global CSS: `app/globals.css`

- `.shell-content`: `px-5 pt-6 sm:px-6 sm:pt-8 lg:px-8 lg:pt-8` (32 instead of 48 at ≥ 1024).
- New `.page-read` (max-width `--content-read`) and `.page-stack > * + * { margin-top: var(--space-section) }`.

### Shared components to change

| File | Change |
|---|---|
| `components/ui/button.tsx` | Sizes: `sm` (h-8, px-3, `text-meta` 500) becomes **the default**. `md` = h-10, px-4, `text-body`. Keep `xs`, `icon-sm` (32), `icon` → 32, and `lg` (44) for phone full-width actions. Every size keeps `max-sm:min-h-11 max-sm:min-w-11` |
| `components/ui/input.tsx` | `size` prop: `md` (h-10, the default in forms) and `sm` (h-8, toolbars); `max-sm:h-11` for both. Text `text-body` |
| `components/ui/select.tsx` | Trigger: `data-[size=default]:h-11` → `h-10`, `sm` → h-8, plus `max-sm:h-11`. Radius `rounded-control` (it uses `rounded-md` today). Border `border-input` stays (DESIGN, "Field edge") |
| `components/ui/textarea.tsx` | min-h-20 (80), `max-sm:min-h-24` |
| `components/ui/tabs.tsx` | `line` list 40 tall (drop `min-h-12.5`), labels `text-meta` 500, 2 px indicator, `max-sm:min-h-11` |
| `components/product/segmented.tsx` | 32 tall, segments 28 (`min-h-9` → h-7 inside an h-8 group), `max-sm:min-h-11` |
| `components/ui/switch.tsx` | 32 × 20 track, 44 hit area on phone |
| `components/ui/card.tsx` | `rounded-panel border bg-card` with **no shadow**, `gap-4`, padding `p-6 max-sm:p-4`. Remove `shadow-sm`, `rounded-xl` and `py-6` |
| `components/ui/table.tsx` | th: h-9 (36), `text-label`, `text-subtle-foreground`, no fill. td: row h-10 (40); a `dense` prop gives 36. A `numeric` cell class: right-aligned and `figures` |
| `components/ui/badge.tsx` | h-5 (20), `rounded-badge`, px-1.5, `text-badge`. New `variant="state"` with `state` in Ready, Busy, Degraded, Down, Blocked, Unknown and Off, mapped to tint and ink |
| `components/product/page-header.tsx` | h1 `text-title`; count `text-heading` subtle; subtitle `text-meta mt-1`; actions default to `size="sm"`; bottom margin `--space-title` (24) |
| `components/product/readiness.tsx` (`SectionHeading`) | `text-heading`, `mb-3` (12); the action is `size="sm"` |
| `components/product/status-ui.tsx` | `StateWord` and `StateIcon` sizes (icon 14 in badges, 20 in the verdict); `StatusLine` uses `text-verdict`; a new `StateBadge` built on `badge.tsx`'s `state` variant; `CapabilityList` rows to `--row-1` (40, 48 on phone); capability icons in ink, with no tinted 32 tile (DESIGN "Colour budget") |
| `components/product/empty-state.tsx` | Anatomy from the "Empty, loading and error" table; drawn inside a ListCard when given `inCard` |
| `components/product/error-state.tsx` | Title `text-body` 600; Retry at `size="sm"` |
| `components/ui/skeleton.tsx` | `SkeletonRows` helper: n rows at `--row-1` or `--row-2` |
| `components/ui/charts.tsx` | Single-series default `--chart-1`; at most 3 series plus Other; legend dots 8; axis labels `text-badge` in subtle ink |
| `components/ui/compact-touch-targets.tsx` | The breakpoint moves from 980 to 640, so every route follows one rule |

### Shared components to create

| File | Job |
|---|---|
| `components/product/list-card.tsx` | `ListCard` (a bordered card with an optional 36 header band and hairline dividers) and `ListRow` (`lines={1 \| 2}`, `leading`, `title`, `meta`, `trailing`, `href`; heights from the row tokens; on a phone the second line joins facts with " · " and never wraps into a third) |
| `components/product/setting-row.tsx` | `SettingsSection` (heading plus card) and `SettingRow` (label, one-line description, control slot; min 64) |
| `components/product/stat-strip.tsx` | `StatStrip` and `Stat` (label, figure, note), up to 4 cells divided by hairlines |
| `components/product/key-value.tsx` | `KeyValueList` for detail pages, receipts and Status facts (rows 32–36, a 160 key column, stacked on phone) |

Each goes into the Components table in `DESIGN.md` in the same PR ("No new component without a director decision": this plan is that decision request).

### Gate

- `scripts/ui/craft_numbers.py --fail`: runs `docs/design/craft-system/azizam/_scripts/measure.mjs` against the fixture (household seed, port from `UI_E2E_PORT`), then checks the rules in DESIGN "Gates for these numbers". The thresholds live in one JSON file next to it. It runs per route, so later PRs can turn routes on one at a time (`--routes settings,people`).
- `scripts/ui/token_drift.py`: new rules. Fail on unprefixed `min-h-11` and `h-11` (allowed under `max-sm:`) in files already moved to the system. Warn everywhere else. Fail on `rounded-lg` and `rounded-xl` in `app/`, on `shadow-sm` and `shadow-md` outside overlays, and on `text-[Npx]`.
- `web/e2e/d1-gates.mjs` must stay green: 44 px targets at 390, axe AA, no overflow.

### Done when

- tsc, eslint, the full web unit suite and the full e2e suite pass. Specs that assert on old copy or old sizes are updated, never deleted (see the 2026-10-08 e2e triage lesson in `ux-findings/README.md`).
- The measure script at 1440 shows control heights only in {32, 40} on Home, Status and Library, which already use the shared parts.
- Contact sheet: before and after for 8 surfaces × 2 widths × 2 themes in `docs/design/craft-system/after-pr1/`.

## PRs 2–9: adopt per surface

Each PR:
1. moves its route's files onto the shared parts;
2. removes unprefixed `min-h-11` and `h-11`, `rounded-lg` and `rounded-xl`, raw `text-sm` and `text-xs` where a role token applies, and `Card` overrides;
3. turns its routes on in `craft_numbers.py`;
4. recaptures with `measure.mjs` and adds the numbers diff to the PR body;
5. runs blind judge pairs for that surface (T-1 and T-2) before merging when the surface was judged before (Home, Status, Library).

The order: templates first on simple pages, then the judged weak surfaces, then the rest.

### PR 2: Settings (settings template)

Files: `app/settings/page.tsx`.

- [ ] Remove the "Settings sections" link row (`page.tsx:1500`; ban 22).
- [ ] Six sections become `SettingsSection` with `SettingRow`s in a `.page-read` (720) column. Rows go from 113 to 64.
- [ ] Section headings 15 → `text-heading` (16 / 24).
- [ ] "Edit" buttons 40 → `size="sm"` (32), right-aligned in the row.
- [ ] Phone: rows 155 → at most 64 per setting line. The description is one line, and the facts move into the edit sheet.

### PR 3: People, invites and plans; Apps and keys (list template)

Files: `app/people/page.tsx`, `app/people/plans.tsx`, `app/apps/page.tsx`, `app/invite/page.tsx` (the owner side only).

- [ ] The people list becomes a `ListCard` with a 36 header band (`text-label`). Rows `lines={2}` at 60 (75 today): the name in `text-body` 500, the email in `text-meta`.
- [ ] Role and provenance chips ("owner · Manual", "admin · OIDC claim") fold into the row menu or the person's detail page. One `StateBadge` at most per row (Disabled, Pending).
- [ ] Access plan and Last active become trailing `text-meta` columns. The plan's "Assigned" second line goes.
- [ ] Tabs (`line`) at 40. "Invite someone" `size="sm"` (32) on desktop.
- [ ] Phone: rows 165 → 64 ("Builder · just now" on the second line).
- [ ] Apps and keys: the same ListCard. Key rows 60. The reveal-once dialog uses `size="md"` actions.

### PR 4: Home (overview template)

Files: `app/home/page.tsx`, `app/home/presentation.ts`, `components/product/audio-cover.tsx` (the Recent strip only).

- [ ] Two heading levels only: delete "Your usual" and "Everything else" (14 px sub-headings). "Make something" stays as the section heading. Favourites lead the section, and the other tasks follow them in a ListCard.
- [ ] Favourite tiles 128 → 88 tall (padding 16, a 20 icon in ink, the name in `text-body` 500, "Last used" in `text-meta`), with no tinted icon colour.
- [ ] Task rows 45 → `ListRow lines={1}` at 40 (48 on phone), icons in ink at 16. A state shows only when not Ready, as a `StateBadge`.
- [ ] Section gaps 12–40 → 32. Heading to content 12.
- [ ] "Connect an app" becomes a content card (padding 24), app rows at 40, "Used 6h ago" 12 → `text-meta` 14, "Add an app" `size="sm"`.
- [ ] The verdict line stays one `text-body` line, with "Open Status" as a link.
- [ ] Coloured pixels outside the Recent photos: target ≤ 2 % (6.1 % today with the photos).

### PR 5: Library (media grid)

Files: `app/library/page.tsx`, `app/library/library-format.ts`, `components/product/audio-cover.tsx`.

- [ ] Tile cues: at most 2 always visible (the type and length badge; the maker mark at 16, from 20), with the day chip on the first tile of each day as the stated exception. The video play disc and the title show on hover or focus only. Audio keeps its play disc.
- [ ] Badge text 10 → `text-badge` (12). Chip radius → `rounded-badge`.
- [ ] Control row: search, kind segments and Filters all at 32 (36–40 today), in one row. The person filter is a 32 select.
- [ ] Phone: the controls row stays under 20 % of 844 (D1.4 rule). Controls at 44.
- [ ] Viewer: facts as a `KeyValueList`; actions `size="sm"`, with Share `size="md"` as the primary.
- [ ] Judge pairs against Apple Photos and Midjourney before merging.

### PR 6: Usage and Activity (overview and table templates)

Files: `app/usage/page.tsx`, `app/usage/source-options.ts`, `app/activity/page.tsx`, `app/activity/requests.tsx`, `components/ui/charts.tsx` (already changed in PR 1).

- [ ] Remove the four `border-0! bg-transparent! shadow-none!` Card overrides (`usage/page.tsx:543, 570, 700, 821`). KPIs become a `StatStrip`, and the chart and table go in bordered cards.
- [ ] KPI icon tiles (40 px tinted squares) go. The figure is in `text-figure`.
- [ ] Header filters: the field labels above the selects ("Person", "Source") go, and the select's value carries them ("Everyone", "All sources"). Selects 44 → 32, "View recent requests" 40 → 32, all in one row.
- [ ] Chart: a single-series chart uses `--chart-1`. If the chart encodes tasks, each category stays distinguishable: use the chart tokens' small categorical palette (at most 3 hues plus Other), or remove stacking and put the per-category breakdown in the table, with 8 px dots. Never turn a per-task stack into one colour (set-10, both judges). Target ≤ 2 % coloured pixels (10.6 % today), without hiding categories.
- [ ] The model table becomes `Table dense`: rows 77 → 36–40, with capability and model on one line ("Chat · Default"), Requests and Cost right-aligned and tabular.
- [ ] "Receipt coverage" is folded under the Spend stat as its note line.
- [ ] Activity: the receipts list on the same `Table dense`, using `StateBadge` for Failed, Refused and In progress.

### PR 7: Workflows and Models (list and catalog)

Files: `app/workflows/page.tsx`, `app/workflows/workflow-api.ts` (display only), `app/models/models-client.tsx`, `app/models/page.tsx`.

- [ ] Remove the page-wide `[&_button]:min-h-11` (`workflows/page.tsx:881`) and the one in the sheet (`:463`). Buttons are 44 → 32 on desktop.
- [ ] Cards 350 tall → `ListRow lines={2}` rows at 60: name, then the facts a person compares choices by, "From <inputs> → <outputs>". Required inputs and outputs stay in the row; Optional and fuller explanations move to the detail sheet (set-10, both judges). The Images and Video groups become one ListCard each.
- [ ] "Open in Try" `size="sm"` as the row's trailing action; "Details" opens on a row click.
- [ ] Filter chips 44 → Segmented 32 next to a 32 search (40 today). The "4 workflows" count moves into the title as the page count.
- [ ] Models: the catalog on `Table`, rows 40 (60 with a second line), prices right-aligned and tabular (Vercel AI Gateway model list).

### PR 8: Status, Engines and Providers

Files: `app/status/status-client.tsx`, `app/status/status-view.tsx`, `app/engines/page.tsx`, `app/engines/engine-checks.tsx`, `app/providers/page.tsx`, `components/product/uptime-bar.tsx`.

- [ ] The column goes from 896 to `--content-read` (720). The verdict is `text-verdict`.
- [ ] The problem card is a content card at padding 24. "What's wrong" and "Affects" become a `KeyValueList`. The `dt` text goes from 15 / 400 to `text-meta` muted, and the values are `text-body`.
- [ ] Capability rows 44 → 40, with Ready quiet. Buttons: Refresh `size="sm"`; the card's primary `size="md"`.
- [ ] Engines (36 legacy `vf-` classes today) and Providers (18 `rounded-lg`) move onto the list template with `Table` and `StateBadge`, and the `vf-` classes are removed from `app/engines/page.tsx`.

### PR 9: Try (creation template), with the Try redesign

Files: `app/try/page.tsx`, `app/try/workflow-inputs.tsx`, `app/try/workflow-media-input.tsx`, and `MakePage` and `useMakeJob` from `briefs/try-redesign-plan.md`.

- [ ] Build `MakePage` on the creation template (DESIGN "Page templates"): a 400 composer, form controls at 40, Run `size="md"`, and a results feed. No dashed "Run chat" placeholder.
- [ ] The tab strip of 7 chips at 37 inside a 44 list is replaced by the redesign's task navigation. Until then, a Segmented at 32.
- [ ] The select 44 → 40. The textarea min 80.
- [ ] The page fills at least 70 % of 900 px at 1440 (517 today), or the template's column narrows.

### Also adopt, with no PR of their own

These fold into PR 3 or PR 8 by owner, or into a final sweep:
- **Sign-in, invite acceptance, setup** (`app/sign-in/page.tsx`, `app/invite/page.tsx`, `app/setup/page.tsx`): a 400 column, form controls at 40, a primary `size="md"` at full width.
- **Shell** (`components/shell/app-shell.tsx`): nav rows stay 32 and 14, but the group label goes to `text-label` (13). Phone tabs unchanged.

## Order and dependencies

```
PR 1 (tokens + components + gate)
 ├─ PR 2 Settings ─┐
 ├─ PR 3 People ───┼─ templates proven
 │                 ├─ PR 4 Home ── judge pairs
 │                 ├─ PR 5 Library ── judge pairs
 │                 ├─ PR 6 Usage + Activity
 │                 ├─ PR 7 Workflows + Models
 │                 └─ PR 8 Status + Engines + Providers ── judge pairs
 └─ PR 9 Try (with the Try redesign; can run in parallel after PR 1)
```

PRs 2–8 touch disjoint route files, so separate lanes can run them in parallel once PR 2 and PR 3 have proven `SettingRow` and `ListCard`. Each lane rebases on PR 1 only.

## Risks

- **Oscillation.** The Library and Home changes touch elements earlier rounds added after judges complained: day chips (round 6, "no dates") and identity cues (round 2, "AI-made"). The spec keeps the day chip and the maker mark, and removes only the duplicate cues. Judge pairs run before merging.
- **Fixture versus live data.** These numbers were measured on the household fixture. Phase 2's live-data harness must re-run `measure.mjs` before Step 5 judging (long names, many rows).
- **Desktop touch.** 32 px controls on a touch laptop between 640 and 1024 px wide: the 44 rule applies below 640 only, which matches the benchmarks (Vercel and Linear at 32 on tablets). Revisit if the red team's tablet persona objects.
- **Field edge.** Fields keep a 3:1 edge (AA) where the benchmarks use about 1.3:1, so they stay slightly heavier than Linear's. This is a recorded exception, not something to fix later.
