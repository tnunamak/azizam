# Azizam design system (D1)

Owner: the D1 designer (Opus 5.5). Status: v1, 2026-10-08. Scope of this round: the system, the shell, Home, Status and Library. Other routes inherit the tokens and the shell; their own layouts come in later rounds.

## Direction

**Deep water, daylight.** The household's gateway is a calm, dark-teal frame that never changes, and everything a person came for sits on a clean daylight page inside it (a night page in dark theme). Every surface opens with one plain verdict sentence, says when that verdict was last checked, and shows the people in the household as their own coloured initials, so the product reads as one home with many members rather than an admin console. The memorable element is the pairing of the deep-water chrome with the verdict line: the chrome tells you whose gateway this is; the verdict tells you, in five words or fewer, whether it works. Nothing is decorative: colour carries state or identity, and every number on screen comes from the gateway.

Why this direction and not another:

- The owner's binding visual authority is the designer handoff ("dark, dense, high-contrast operations surfaces; deep-water backgrounds", `assets/reference-ui/README.md`). A fully dark product drifts toward ban A2 and A5, so the deep water is kept for the chrome and the media stage, and content uses a cool daylight surface.
- Round S5 judges preferred Astra for "distinctive household identity" and Console for owner troubleshooting (`judge-s5-r2`). This system keeps Astra's petrol frame and household names, and Console's layer naming, uptime evidence and incident card.
- Cream, sand and serif (the shipped look) are banned (A1, "sand is claudy"). The content surface is a cool grey-green, never warm.

## Principles

Each rule names its evidence. When two rules conflict, the earlier one wins.

1. **A verdict first, in five words or fewer.** Home and Status open with one sentence and an icon, then a single detail line (Linear status 2a.1, Apple System Status 2d.1, ban 16). Example: "Ready, except video and embeddings." / "Video is degraded."
2. **Every state is an icon, a colour and a word.** Never colour alone (Linear 2a.2, Vercel 12b.1). The words come from one vocabulary: Ready, Ready to start, Starting, Busy, Degraded, Unavailable, Turned off, Not set up, Not in your plan, Not checked yet.
3. **A check is never a use.** A time from a health check reads "Checked 3 min ago"; a time from a request reads "Used 2 h ago" or "Failed 2 h ago"; configuration has no time (brief "Data you can rely on"; judge-s5-r1 "false confidence"). The verdict carries the oldest check behind it.
4. **Unknown is never zero, and missing cost is never free.** "Not checked yet", "Spend not recorded", "Cost not recorded" (VOICE "Money words", TARGET T-2.5).
5. **Name the layer, then one action.** A problem card says where it stopped (Gateway, Engine, Provider, the gateway's computer), what it affects, the last error, the last 24 hours, who recently failed, and one action named after its object: "Inspect Video studio" (incident.io 2c.3, judge-s5-r2, Tim 2026-10-02 "it's not clear if there is a gateway problem or a user problem or a model problem").
6. **People are part of the picture.** A person keeps one hue and initial everywhere: the account menu, Library tiles, the viewer, recent failures (Suno 3b.1, Apple Home 1a; Tim 2026-06-14 "an experience my wife can understand and find delightful").
7. **Spend attention like money.** One fact once per screen, healthy detail folded behind one control while something needs attention, no helper sentence under a title (Apple Home 1a.1, Apple 2d.2, ban 20; Tim 2026-06-04 "everything in the design that requires cognitive processing carries a cost").
8. **Media is the content.** Library tiles carry no card chrome or text, except audio, which has no picture (Midjourney 3a.2). Selection opens a viewer with the media on the deep-water stage and the facts beside it (Apple Photos 3c, Suno 3b.2).
9. **One pattern per job.** One segmented control (ban 21), one focus ring, one section heading, one table rhythm, one dialog grammar.
10. **Not a toy.** No slogans, no gradients, no glow, no pill primary buttons, no tracked-caps eyebrows (bans 6, 13, 15, A4, A5; Tim 2026-10-03 "not a toy").

## Tokens

All raw values live in `web/src/styles/tokens.css`. Components use Tailwind utilities that resolve to these variables. `scripts/ui/token_drift.py --fail` enforces it.

### Colour

| Role | Token | Light | Dark | Use |
|---|---|---|---|---|
| Page | `--background` | #F3F6F6 | #0C1F25 | Content canvas |
| Surface | `--card` | #FFFFFF | #112930 | Panels, tiles, inputs |
| Sunken | `--muted` | #E9EEEF | #16313A | Tracks, skeletons, empty media |
| Ink | `--foreground` | #10232A | #E3ECEE | Text |
| Secondary ink | `--muted-foreground` | #4F6268 | #9AB0B6 | Labels, metadata (5.5:1 or better) |
| Hairline | `--border` | #D7E0E2 | #213E46 | Structure |
| Control edge | `--input` | #7B8E93 | #58777F | Inputs and outline buttons (3:1) |
| Action | `--primary` | #0D4552 | #67CBD5 | The one primary button per view |
| Link | `--link` | #0A6676 | #67CBD5 | Text links |
| Selected | `--accent` / `--accent-foreground` | #DCF0F2 / #0B4A57 | #163C45 / #8FE0E7 | Selected options |
| Focus | `--ring` | #1A8C9C | #67CBD5 | 2 px outline, 2 px offset |
| Chrome | `--chrome` | #0C2830 | #071A1F | Sidebar, phone header |
| Chrome raised | `--chrome-raised` | #143841 | #0F2A31 | Hover and current item |
| Chrome text | `--chrome-foreground` / `--chrome-muted` | #E3EEF0 / #93B0B7 | same | |
| Chrome accent | `--chrome-accent` | #6FD3DC | same | Brand mark, current-item icon |
| Media stage | `--media-stage` | #0A2229 | #050F12 | Behind media in the viewer |
| Success | `--success` / tint | #17703F / #E3F3EA | #6FD39B / #12352A | Ready |
| Warning | `--warning` / tint | #8F5300 / #FDF1DC | #E8B65A / #3A2E15 | Degraded, busy |
| Danger | `--danger` / tint | #B3261E / #FCE9E7 | #FF8F85 / #3F1F1E | Unavailable, failed |
| Info | `--info` / tint | #1F5FA6 / #E6EFFA | #8DB8F2 / #182E4A | Starting |
| Neutral | `--neutral` | #6A7C81 | #8AA1A7 | Turned off, unknown |
| People | `--person-1…7`, `--person-ink` | coral, sky, sage, violet, amber, rose, teal on #10232A | same | Person marks (all 6:1 or better) |
| Capabilities | `--capability-*` | seven hues | lighter pairs | Capability icons and tints at 12% |

All text pairs pass WCAG AA in both themes (checked with `contrast.py`; lowest body pair 5.47:1).

### Type

One family, Hanken Grotesk (OFL, variable 100–900); its figures are tabular by default, so counts, money and times align. Geist Mono only for values a person copies or matches: model names in Details, request IDs, addresses, errors (ban 10).

| Token | Size / line | Weight | Use |
|---|---|---|---|
| `text-3xl` | 30 / 36 | 600, −0.015em | Verdicts and page titles at ≥ 640 px |
| `text-2xl` | 24 / 30 | 600 | Verdicts at 390 px |
| `text-xl` | 20 / 28 | 600 | Viewer title |
| `text-lg` | 17 / 24 | 600 | Section headings, card titles |
| `text-base` | 15 / 22 | 400 | Body |
| `text-sm` | 14 / 20 | 400–500 | Rows, controls, facts |
| `text-xs` | 12 / 16 | 500 | Metadata, durations |
| `text-2xs` | 10 / 12 | 600 | Initials inside a 20 px person mark only |

### Spacing, radius, elevation, motion

- **Spacing:** Tailwind's 4 px scale (`--spacing: 0.25rem`). Page gutters 20 / 32 / 48 px at 390 / 640 / 1024+. Section rhythm 40 px; inside a section 12 px; rows 8–12 px vertical. Content width caps at 1200 px.
- **Radius:** controls 6 px (`rounded-control`), panels and dialogs 10 px (`rounded-panel`), media 6 px (`rounded-media`), uptime segments 2 px, people and the round play button full. No pill buttons (ban 13).
- **Elevation:** borders build structure; a shadow only lifts something above the page: menus, popovers, dialogs, the viewer (`--shadow-overlay`). Cards never carry a shadow (ban A4).
- **Motion:** 120 ms for hover and press (`--motion-fast`), 200 ms for disclosure and overlays (`--motion-duration`), easing cubic-bezier(0.2, 0, 0, 1). `prefers-reduced-motion` sets both to 0.
- **Targets:** at < 640 px every control is at least 44 × 44 px (`max-sm:min-h-11`). Desktop controls are 32–40 px.

## Shell

- **Sidebar (≥ 1024 px):** 232 px of deep water. The brand mark and name (plus the instance name when it differs), a search field with its shortcut, then two groups: **You** (Home, Library, Try, Apps & keys, Models, Workflows, Usage) and, for owners, **Gateway** (Status, Engines, Providers, People, Activity, Settings). Members see Status in You. Order is by how often a household member needs each place. Every destination has its own icon (ban 19). The footer holds the gateway health line (dot and two words, with a popover that separates reachability from engine health and says when each was checked), the account (person mark and name) and the theme toggle.
- **Rail (640–1023 px):** 72 px icon rail with tooltips; a menu button opens the full list as a sheet.
- **Phone (< 640 px):** a 56 px deep-water header with the mark, the health dot (opens the same popover) and the person mark (account menu). A bottom bar with four labelled tabs: Home, Library, Apps, More (Midjourney 3a.4: four or fewer). More opens every destination, search and the theme toggle.
- **Owner alert:** when an engine is down or a provider fails, a one-line warning band sits above the page on every route except Status, with "View status".
- **Command palette (⌘K / Ctrl K):** pages, your apps, models, people, and three actions: create a key, copy the gateway address, check gateway reachability.

## Components

| Component | File | Job |
|---|---|---|
| Verdict | `components/product/readiness.tsx` | The opening sentence with its state icon and a detail line |
| StateLabel, `availabilityState` | same | The one state vocabulary, as icon + colour + word |
| EvidenceTime, `evidenceStamp` | same | "Checked …" vs "Used …" vs "Failed …" |
| SectionHeading | same | Every titled region: 17 px heading, optional count and one action |
| `capabilityMeta` | same | Capability name, icon and hue |
| UptimeBar | `components/product/uptime-bar.tsx` | 24 hourly segments with an accessible summary |
| PersonMark | `components/product/person-mark.tsx` | A person's initial in their stable hue |
| BrandMark | `components/product/brand-mark.tsx` | The fish, in the chrome accent |
| Segmented | `components/product/segmented.tsx` | The only segment or tab pattern for filtering |
| CopyField | `components/product/copy-field.tsx` | One boxed value with the copy button inside its edge (Cloudflare 6a.2) |
| Button | `components/ui/button.tsx` | default (primary), outline, secondary, ghost, link, destructive |
| Input, Select, Popover, Dialog, Sheet, DropdownMenu, Tooltip, Skeleton | `components/ui/*` | shadcn primitives on the tokens |
| EmptyState, ErrorState | `components/product/*` | Blank-slate with one action; error with heading, sentence and retry (Geist 12c) |
| Capability tile | `app/home/page.tsx` | Icon, name, state; a link to Try when usable; "Inspect <engine>" for owners when not |
| Problem card | `app/status/status-client.tsx` | Where, Affects, Last error, Last 24 hours, Recent failures, one action |
| Layer table | same | Engines and providers: name, serves, uptime bar, %, state; spend meter for providers |
| Creation tile and viewer | `app/library/page.tsx` | Square media tile with maker; viewer with stage, facts and actions |

No new component without a director decision.

## Surfaces in this round

- **Home.** Verdict, capability tiles (4 × 2 at 1440, compact rows at 390), "Connect an app" (gateway address, your apps with last use, add an app with the recipe flow), recent creations. Owners see the same Home, plus "Inspect <engine>" on any capability that is down.
- **Status, owner.** Verdict with check time; "Needs attention" problem cards; every capability with its state; the layer table (folded while something needs attention). **Status, member.** The Home verdict, the member's own recent failure on a capability that is down now (with its receipt), their capabilities, their recent requests.
- **Library.** One grid in buckets (this week, last week, months); kind as a segmented control; the owner's person filter; model, date and app behind "Filters"; the viewer with image, video and audio playback, download, prompt, receipt and cost.

## Token drift, bans and exceptions

- Run `scripts/ui/token_drift.py --fail` (default scope: the surfaces above and their primitives). Legacy routes report with `--all` and are out of D1 scope.
- Ban-list exceptions: none recorded.

## Backend asks

1. **Owner name for members.** Member payloads have no owner display name, so copy says "Ask the gateway owner" where VOICE wants "Ask Sam". Add `gateway.owner_name` to `/me/api/home` and `/me/api/status`.
2. **Evidence source on `/me/api/status`.** Its capabilities carry `observed_at_s` without the evidence `source`, so the member Status page reads evidence from `/me/api/home` to keep checks and uses apart. Add `availability.evidence` to each status capability.
3. **Creation retention.** The default image cache keeps files 24 hours (`images.cache_ttl`), so a family's Library empties daily. Expose the retention in `/me/api/media` (e.g. `retention_s`) so Library can say "Files are kept for 30 days", and consider a longer default.
4. **Failed receipts filter.** `/admin/api/activity/requests` ignores a status filter, so Status reads the latest 50 requests to find recent failures for an engine. Add `status=failed` and `backend=<service_id>` filters.
5. **Provider connection checks.** `checked_at` is null until a connection test runs, so a configured provider reads "Not checked yet" indefinitely. A periodic provider check (or a last successful request time per provider) would let Status say more.
6. **Video posters.** The seed's video has no `poster_asset_id` although a poster exists; the gateway could attach the first-frame poster it already extracts.
