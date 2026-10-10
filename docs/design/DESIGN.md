# Azizam design system (D1.2)

Owner: the D1 designer (Opus 5.5). Status: v1.6, 2026-10-09 (D1.6: a continuous Library grid with type, play, maker and day cues; Status cards that lead with the action; one sidebar type size; larger, darker secondary text; Home tiles that say why they lead; D1.5b: Status is one reading column at every width and folds healthy engines during an incident again; the gateway's own "degraded" status; Try's failed-run title and primary Run; D1.5: a state palette that colour-blind readers can part, date groups and calm neutral audio in Library, plain-word evidence with clock labels on Status, one control vocabulary, a quieter owner group in the sidebar; D1.4: a media-only Library with hover titles and a three-column phone grid, a Home with favourites, a balanced Status, and red-team honesty fixes; D1.1: one status model, freshness, plan blocks, task-first Home, a system instead of pages; D1.2: media that identifies itself, one search, one row grammar, a craft pass against the benchmarks; D1.3: a problem card that tells the strip's story, scope before evidence, a Library control row, dated captions and one grid, a Home that leads with tasks and shows Recent on the first phone screen). Scope: the system, the shell, Home, Status and Library. 2026-10-09: "System numbers v2" (below "Tokens") specifies the craft system measured against the benchmarks; it is the target for every surface, applied by `craft-system/PLAN.md`. Other routes inherit the tokens and the shell; their own layouts come in later rounds.

## Direction

**Deep water, daylight.** Everything a person came for sits on a clean daylight page (a deep-water night page in dark theme). Every surface opens with a title and one verdict label, says how old the evidence behind it is, and shows the people in the household as their own coloured initials, so the product reads as one home with many members rather than an admin console. The memorable element is the goldfish-orange fish with the verdict line: the fish tells you whose gateway this is; the verdict tells you, in a few words, whether it works. Nothing is decorative: colour carries state or identity, and every number on screen comes from the gateway.

D1.1 change: in light theme the sidebar is now a quiet grey-green panel one step off the page, not a dark-teal frame. The Linear-designer red team read the dark frame plus pastel tiles as two products glued together; a quieter frame lets the page carry the hierarchy. Dark theme keeps the deep-water chrome.

Why this direction and not another:

- The owner's binding visual authority is the designer handoff ("dark, dense, high-contrast operations surfaces; deep-water backgrounds", `assets/reference-ui/README.md`). A fully dark product drifts toward ban A2 and A5, so deep water is the dark theme (chrome, page and media stage), and the light theme uses a cool daylight surface with a quiet chrome one step off it.
- Round S5 judges preferred Astra for "distinctive household identity" and Console for owner troubleshooting (`judge-s5-r2`). This system keeps Astra's household names (and its petrol frame in dark theme), and Console's layer naming, uptime evidence and incident card.
- Cream, sand and serif (the shipped look) are banned (A1, "sand is claudy"). The content surface is a cool grey-green, never warm.

## Principles

Each rule names its evidence. When two rules conflict, the earlier one wins.

1. **A verdict first, as a label.** Home and Status open with a title, then one verdict label with its state icon and the age of its evidence (Linear status 2a.1, Apple System Status 2d.1, ban 16). Labels, not sentences: no full stop. Examples: "Everything works", "Video degraded", "Video down, chat degraded", "3 of 8 capabilities need attention". Status shows it at 20 px; Home shows the same words, icon and age as one 14 px line, because a landing page leads with what to make (D1.3).
2. **Every state is an icon, a colour and a word.** Never colour alone (Linear 2a.2, Vercel 12b.1). The words are the fixed set in "Status model" below, and nothing else.
3. **A check is never a use, and old evidence is Unknown.** A time from a health check reads "Checked 3 min ago"; a time from a request reads "Last used 2 h ago"; configuration has no time (brief "Data you can rely on"; judge-s5-r1 "false confidence"). A check older than 15 minutes cannot vouch for Ready: the state becomes Unknown with "Last checked 7 h ago". The verdict carries the oldest check behind it. A row repeats nothing the state word already says: it keeps a detail only for a reason or an age.
4. **Unknown is never zero, and missing cost is never free.** "Not checked yet", "Spend not recorded", "Cost not recorded" (VOICE "Money words", TARGET T-2.5).
5. **Name the subject, then one action.** A problem card's title names the subject and its fault in plain words ("Video studio is answering with errors", "The gateway's computer is low on disk space", "The Everyone plan reached its spending limit"); the verdict line above carries the state word, so the card does not repeat it (round-2 judges: "degraded is stated in two stacked headings"). The action sits on the title row. It says what happened in plain words, the one fix, and its evidence: what it affects (and whether another engine still serves it), since when, health checks for the last 24 hours, and failed requests with the people and a receipt. The one action is named after its object: "Inspect Video studio" (incident.io 2c.3, judge-s5-r2, Tim 2026-10-02 "it's not clear if there is a gateway problem or a user problem or a model problem").
6. **People are part of the picture.** A person keeps one hue and initial everywhere: the account menu, Library's person filter, the viewer, recent failures. Hues mark people, never media: D1.5 took the maker's hue off audio tiles, where seven hues read as arbitrary colour (Suno 3b.1, Apple Home 1a; Tim 2026-06-14 "an experience my wife can understand and find delightful").
7. **Spend attention like money.** One fact once per screen, healthy detail folded behind one control while something needs attention, no helper sentence under a title (Apple Home 1a.1, Apple 2d.2, ban 20; Tim 2026-06-04 "everything in the design that requires cognitive processing carries a cost").
8. **Media is the content, in one continuous grid with quiet cues.** D1.5's date groups left part-filled rows (round 6: "Today and Yesterday rows are mostly blank"), and bare tiles gave no type, maker or play cue (round 6: "video and audio look like stills"). D1.6 rules: one continuous grid, as dense as round 4's; the day rides on the first tile of each day as a small chip, so no row breaks (Apple Photos' All Photos scroll); no captions (a wide screen shows a short whole-word title, the maker and the day on hover or focus); video and audio carry their type glyph and length bottom left and a play disc in the middle; with Everyone shown, every tile carries its maker's mark top right. All cues share one dark scrim. Items without a prompt (apps' requests never store prompts, docs/CORE_CONTRACTS.md) are named by type and time, "Video, Oct 20 at 7:45 PM", never "Image" alone. Audio has no picture, so every clip gets one calm neutral face in the grid's rhythm: the sunken surface and its real waveform (or the glyph) under the play disc. No colour per person, no words on the tile: voice clips must never be the loudest thing on the page. Never a blank square. Selection opens a viewer with the media on the media stage and the facts beside it (Apple Photos 3c, Suno 3b.2).
9. **One pattern per job.** One segmented control (ban 21), one focus ring, one section heading, one table rhythm, one dialog grammar, one disclosure chevron, one "selected" (the accent tint, on chips, tabs and the sidebar's current place). D1.5: controls are crafted from the tokens, never the kit default (a sunken track with a raised white segment, the browser's disclosure triangle).
10. **Not a toy.** No slogans, no gradients, no glow, no pill primary buttons, no tracked-caps eyebrows (bans 6, 13, 15, A4, A5; Tim 2026-10-03 "not a toy").

## Status model

One per-capability state, derived for the person who is looking, feeds every surface: the Home verdict and tasks, the Status verdict and capability list, the member's failure callout and the shell's health line. Code: `web/src/lib/capability-state.ts` (the rules) and `web/src/lib/use-capability-views.ts` (the evidence for an owner or a member). Tests: `web/tests/lib/capability-state.spec.ts`.

### The words

| Word | Tone | Means | Never means |
|---|---|---|---|
| **Ready** | quiet (green icon, muted word) | A check or request newer than 15 minutes says it works, nothing failed since, and no limit applies. A fresh lifecycle check can say a managed engine is stopped and ready to start on request; the detail says "Ready to start when you make a request." | "Configured", or "it worked yesterday". |
| **Busy** | blue | The gateway or an engine check says it is working on other requests or starting up, and no request for it has failed since its last success. | A failure. A busy report next to a failed request becomes Degraded. (Before main 00f10bc the gateway folded "degraded" into "busy", so the UI distrusted a bare "busy"; backend ask 16 is done.) |
| **Degraded** | amber | It works, but not fully: the gateway reports it degraded ("Answering with errors. Requests may fail."; it is still marked available, so this is read before Ready); one of its engines fails while another serves it; its only engine answers badly; a request failed in the last hour with no success since; or a spending limit paused some paid models. | Down. |
| **Down** | red | Every engine or provider that serves it fails its check, or the gateway reports it unavailable. | Off. |
| **Blocked** | violet, with a lock | The person's access plan does not include it. | A fault. |
| **Unknown** | grey dashed | There is no check, or the last one is older than 15 minutes, or it did not answer. The detail gives the age of the last recorded check when there is one ("Last checked 7 h ago; no answer since"). | Ready, or zero. |
| **Off** | grey | Turned off by the owner, or not set up yet. Off capabilities do not count against the verdict. | A fault. |

Receipts use three words of their own: **Failed** (finished with an error), **Refused** (the gateway did not run it, e.g. a spending limit), **In progress** (queued or running; never counted as a failure).

### Rules

1. **Order.** Off and Blocked come from the access plan and configuration. Then engines: if every engine for the capability fails, it is Down (or Degraded if they all still answer). Then evidence: none, or older than 15 minutes, is Unknown. Then one failing engine of several is Degraded; the gateway's "unavailable" is Down; the gateway's "degraded" is Degraded; "starting" and "busy" are Busy unless a request failed since the last success, then Degraded; an active spending-limit refusal or an unresolved failure in the last hour is Degraded; otherwise Ready.
2. **Verdict.** "Everything works" only when every counted capability is Ready. One or two exceptions are named ("Video down, chat degraded"); more read "3 of 8 capabilities need attention"; all Down reads "Nothing works right now"; nothing counted reads "Nothing is turned on yet". The check time beside the verdict counts the capabilities it does not vouch for: "Checked just now · 4 unknown". Owner Status adds problems the capability model cannot see (the gateway's computer, an access plan) and checks whose data is missing: "Checking", "Couldn't check status", "Engine health unknown", "Some engines are unchecked", "Provider status unavailable". None of those may read as working.
3. **The shell.** The health line says "Gateway unreachable" only for reachability; otherwise it counts the same capability states: "All working", "1 issue", "Not checked yet", "Busy" (only busy exceptions; Busy is not an issue), "Nothing turned on", or "Status unknown" when the states (or an owner's engine checks) did not load. The word must match the dot. "N issues" counts known faults (Down, Blocked, Degraded) and takes the dot colour of the worst one. "Not checked yet" means every counted problem is Unknown: Unknown is missing evidence, not a known fault, so it is never an "issue" and its dot is the grey ring, not a filled dot. The pill does not show the count of unchecked capabilities; the popover does. In a mix, only the faults are counted, as issues, in the fault colour. The popover lists each counted capability as "Images · Down" with its detail sentence, and its link reads "See why" when something is counted, otherwise "View status". "Gateway reachable" appears only to someone signed out or using a service key. It never reads as all-clear while a capability the person depends on fails.
4. **Uptime, defined once.** "Health checks, last 24 h": the 24 hours ending at the status time, split into hours. An hour is up only if every check in it was up; an hour with no check is empty, not up. The figure is "16 of 24 checked hours up"; it is never a percentage of samples. The bar shows the same hours, oldest left, with the hour-by-hour list in a disclosure for screen readers and colour-blind readers. A problem card says what the bar shows, counted from the same hours (`healthHistory`, `healthStory`), in two plain lines: "Degraded since about 1:30 PM" (or "Working again since about …"), then "Down for 4 hours and degraded for 2 in the last 24 hours, in 3 separate stretches." (D1.5: "3 spells: down 4 h" read as jargon). On a card the strip has its legend above it and clock times every six hours under it, so "since about 1:30 PM" can be matched to a cell. It never gives a start time for only the latest run of bad hours (D1.3: "Started 1:30 to 2:30 PM" above a day of red hours was a trust bug).
5. **Failures, defined once.** "Failed requests" counts finished failures (failed, error, timeout) among the latest 50 household requests (the API's page), and says so: "1 of the latest 50 requests". Queued and running requests are never failures. A failure stops counting for a capability's state once a later request for it succeeds.
6. **Spending limits.** A refusal counts while its limit cannot have reset and no later paid request succeeded: a daily limit (rolling 24 hours) clears within a day; a monthly limit (calendar month, UTC) clears on the 1st. Owners see the plan, its limits, which limit was reached in the gateway's words, when it resets, who was refused, and "Edit the <plan> plan". Members see "Paid models were paused for you", when it resets, and who to ask, in words that do not suggest they spent anyone's money.

## Tokens

All raw values live in `web/src/styles/tokens.css`. Components use Tailwind utilities that resolve to these variables. `scripts/ui/token_drift.py --fail` enforces it.

### Colour

| Role | Token | Light | Dark | Use |
|---|---|---|---|---|
| Page | `--background` | #F3F6F6 | #0C1F25 | Content canvas |
| Surface | `--card` | #FFFFFF | #112930 | Panels, tiles, inputs |
| Sunken | `--muted` | #E9EEEF | #16313A | Tracks, skeletons, empty media |
| Ink | `--foreground` | #10232A | #E3ECEE | Text |
| Secondary ink | `--muted-foreground` | #3D4F55 | #B4C6CB | Labels, metadata. D1.6 (round 6: "secondary text small and low-contrast"): 7.9:1 on the page and 8.6:1 on cards in light theme (was 5.9 and 6.4), 9.6 and 8.6:1 in dark (was 7.5 and 6.7) |
| Hairline | `--border` | #D7E0E2 | #213E46 | Structure |
| Control edge | `--input` | #7B8E93 | #58777F | Inputs and outline buttons (3:1) |
| Action | `--primary` / `-foreground` | #0D4552 / white | #1F6F79 / white | The one primary button per view (D1.1: the old sea-glass dark primary read as a disabled button) |
| Link | `--link` | #0A6676 | #8CC8CB | Text links |
| Selected | `--accent` / `--accent-foreground` | #DCF0F2 / #0B4A57 | #163C45 / #8FE0E7 | Selected options |
| Focus | `--ring` | #1A8C9C | #8CC8CB | 2 px outline, 2 px offset |
| Chrome | `--chrome` | #EEF2F3 | #08171C | Sidebar, phone header (D1.1: quiet light panel in light theme) |
| Chrome raised | `--chrome-raised` | #E1E8EA | #13292F | Hover and current item |
| Chrome text | `--chrome-foreground` / `--chrome-muted` | #10232A / #3D4F55 | #E3EEF0 / #B0C6CC | Nav rows: 7.6:1 on the light chrome (was 5.7), 10.3:1 on the dark (was 7.9) |
| Chrome edge | `--chrome-border` | #D7E0E2 | #1A333A | Sidebar right edge, phone header bottom |
| Chrome accent | `--chrome-accent` | #D2702A (goldfish) | #F2A65A | The brand mark; used nowhere else |
| Media stage | `--media-stage` / `-foreground` | #E6ECEE / #10232A | #050F12 / #E3EEF0 | Behind media in the viewer |
| Media badge | `--media-badge` / `-foreground` / `-hover` | rgb(5 15 18 / 64%) / white / 80% | same | The duration badge on video and audio tiles and the audio play badge (white text 9:1 or better over the scrim on white) |
| Subtle ink | `--subtle-foreground` | #4A5C62 | #9FB2B9 | Group labels, axis labels, quiet counts: 6.4:1 on the light page (was 4.8), 7.7:1 on the dark (was 5.5) |
| Success | `--success` / tint / fill | #15703F / #E3F3EA / #2E9E63 | #6FD39B / #12352A / #4CC283 | Ready |
| Warning | `--warning` / tint / fill | #B45309 / #FEF4E3 / #D97706 | #F5B544 / #3A2E15 / #F2A93B | Degraded, Refused |
| Danger | `--danger` / tint / fill | #B3261E / #FCE9E7 / #C62828 | #FF8F85 / #3F1F1E / #F0655A | Down, Failed |
| Info | `--info` / tint / fill | #1F5FA6 / #E6EFFA / #3B82D6 | #8DB8F2 / #182E4A / #6AA2EC | Busy, In progress |
| Blocked | `--blocked` / tint / fill | #6B4FB0 / #EFE9FB / #5B3A9E | #C2A8F0 / #2A2340 / #D6C4FF | Blocked (with a lock icon) |
| Neutral | `--neutral`, `--nocheck-fill` | #6A7C81, #C9D3D6 | #8AA1A7, #3B5961 | Off, Unknown (with the subtle ink for the word); an hour with no check |
| People | `--person-1…7`, `--person-ink` | coral, sky, sage, violet, amber, rose, teal on #10232A | same | Person marks (all 6:1 or better) |
| Capabilities | `--capability-*` | seven hues | lighter pairs | The capability's glyph where it is the subject (the Try chip, the Library type badge); never a list icon or a tinted tile (System numbers v2, "Colour budget") |
| Charts | `--chart-1`, `--chart-2`, `--chart-3`, `--chart-other` | = primary, #2F8F9B, #8C7A55, = neutral | = link, #2F7F88, #B9A274, = neutral | One series in `--chart-1`; a breakdown in at most three hues plus Other (`chartColors`). Every fill is 3:1 or better on the card |

All text pairs pass WCAG AA in both themes: axe colour-contrast passes on every gated page (owner and member; Home, Status, Library and the viewer; light and dark; 1440 and 390).

#### State palette checks (D1.5)

Judges read the old Degraded (#8F5300 text, a brown fill) as muddy and close to Down. Each state now has a text colour (words and icons) and a fill (strip cells, dots, legend swatches, meters). Degraded is a clear amber that is much lighter than Down's red, so the two part by lightness, which survives every colour-vision deficiency; Blocked is a violet with a lock, deep in light theme and pale in dark, so it parts from Busy's mid blue by lightness too. Every state still has its own icon and word (principle 2), and down hours are taller than up hours in a strip.

Contrast (WCAG 2 ratio; text against card and page, fills against card; from `scripts/ui/state_palette.py`, which reads `tokens.css`):

| State | Light text (card / page) | Light fill | Dark text (card / page) | Dark fill |
|---|---|---|---|---|
| Ready | 6.1 / 5.6 | 3.4 | 8.3 / 9.3 | 6.8 |
| Busy | 6.5 / 6.0 | 3.9 | 7.4 / 8.3 | 5.8 |
| Degraded | 5.0 / 4.6 | 3.2 | 8.4 / 9.3 | 7.6 |
| Down | 6.5 / 6.0 | 5.6 | 6.9 / 7.7 | 4.9 |
| Blocked | 6.2 / 5.7 | 8.3 | 7.3 / 8.2 | 9.5 |
| Unknown, Off | 5.2 / 4.8 | (no-check cell 1.5, drawn shorter) | 5.0 / 5.5 | (2.0) |

Text passes 4.5:1 and fills pass the 3:1 non-text minimum everywhere, except the no-check cell, which is deliberately faint: it means "no evidence" and is also the shortest cell.

Separation between fills (CIELAB ΔE76 after simulating each deficiency at full severity with the Machado 2009 matrices; above about 20 reads as a different colour at a glance):

| Pair | Light: normal / protan / deutan / tritan | Dark: normal / protan / deutan / tritan |
|---|---|---|
| Degraded – Down | 41 / 38 / 25 / 27 | 49 / 46 / 29 / 34 |
| Ready – Degraded | 89 / 32 / 48 / 97 | 78 / 36 / 46 / 84 |
| Ready – Down | 108 / 26 / 28 / 119 | 102 / 21 / 21 / 116 |
| Busy – Blocked | 38 / 23 / 19 / 38 | 28 / 20 / 27 / 41 |
| Degraded – Blocked | 119 / 112 / 120 / 70 | 91 / 93 / 91 / 44 |

The weakest pairs (Ready – Down for protan and deutan readers in dark theme, Busy – Blocked for deutan readers in light theme) are near the line; there the icon, the word and the taller down cell carry the difference. Busy and Blocked never share a strip: strips show only up, degraded, down and no check.

### Type

One family, Hanken Grotesk (OFL, variable 100–900); its figures are tabular by default, so counts, money and times align. Geist Mono only for values a person copies or matches: model names in Details, request IDs, addresses, errors (ban 10).

D1.1 retuned the scale so weight and size carry the hierarchy (judges: "craft fails"; Linear red team: "everything is the same weight"). Each step is one role.

| Token | Size / line | Weight | Use |
|---|---|---|---|
| `text-4xl` | 40 / 44 | 600 | Reserved (marketing-size numbers); unused in D1.1 |
| `text-3xl` | 30 / 36 | 600 | Reserved |
| `text-2xl` | 24 / 30 | 600, −0.015em | Page titles (PageHeader) |
| `text-xl` | 20 / 28 | 600 | The verdict label (StatusLine), viewer title |
| `text-lg` | 16 / 24 | 600 | Section headings (SectionHeading), panel headings |
| `text-base` | 15 / 22 | 400–600 | Body; callout titles and row names at 500–600 (D1.6: was 14) |
| `text-sm` | 14 / 20 | 400–500 | Rows, controls, facts, secondary text (D1.6: was 13; Linear and Apple set secondary text at 13 to 15 px) |
| `text-xs` | 12 / 16 | 500 | Metadata, durations, axis labels |
| `text-2xs` | 10 / 12 | 600 | Initials inside a 20 px person mark only |

### Spacing, radius, elevation, motion

- **Spacing:** Tailwind's 4 px scale (`--spacing: 0.25rem`). Page padding 20 / 24 / 32 px at 390 / 640 / 1024+ (`--page-pad`, craft PR 1; it was 48 at 1024+). Section rhythm and the rest: System numbers v2 below. Content width caps at 1200 px.
- **Radius:** controls 6 px (`rounded-control`), cards, panels, dialogs and popovers 8 px (`rounded-panel`; 10 before craft PR 1), status badges and chips 4 px (`rounded-badge`), media 6 px (`rounded-media`), uptime segments 2 px, people and the round play button full. No pill buttons (ban 13).
- **Elevation:** borders build structure; a shadow only lifts something above the page: menus, popovers, dialogs, the viewer (`--shadow-overlay`). Cards never carry a shadow (ban A4).
- **Icons:** lucide at 16 px in rows and 20 px for state, stroke 1.75 (the default 2 read heavy beside 13–14 px text).
- **Motion:** 120 ms for hover and press (`--motion-fast`), 200 ms for disclosure and overlays (`--motion-duration`), easing cubic-bezier(0.2, 0, 0, 1). `prefers-reduced-motion` sets both to 0.
- **Targets:** at < 640 px every control is at least 44 × 44 px (`max-sm:min-h-11`). Desktop controls are 32 or 40 px.

## System numbers v2 (craft system, 2026-10-09)

Status: **PR 1 shipped** (`craft-system/PLAN.md`): the tokens, the shared components (controls, cards, tables, badges, headers, empty and loading states, charts), the four new components in "Components" and the gates. Pages adopt the templates surface by surface in PRs 2–9, so a page that has not moved yet can still differ from the numbers here; `scripts/ui/craft_numbers.py` reports by how much. Where this section and "Tokens" disagree, this section wins.

Why this section exists: craft levelled off at about 7.0 against about 7.9 for the benchmarks after four rounds of page-by-page patches. The measurements (`craft-system/MEASUREMENTS.md`) show the gap is in the system. Desktop uses six control heights, the same two-line row is drawn at five heights, two sizes serve one text role, 44 px touch targets leak onto the desktop, and cards are still the kit default. Every number below cites its evidence: a benchmark file under `benchmarks/` (measured in `MEASUREMENTS.md` §1), or a measurement of Azizam (§2).

**Font correction.** Hanken Grotesk's x-height is 0.493 em, against 0.54–0.55 for Geist, Inter and SF. Text sizes therefore sit 1 px above the benchmark size, so they read at the same size. Heading sizes are the same as the benchmarks', because the cap heights match (`MEASUREMENTS.md` §1.4).

### Type

| Role | Token (new utility) | Size / line height | Weight | Phone (< 640) | Reason |
|---|---|---|---|---|---|
| Page title | `text-title` | 24 / 32, −0.015 em | 600 | 24 / 32 | Vercel ×4 and Linear ×2 at 24 (`people/vercel-team-members-roles-desktop`, `settings/linear-security-saved-desktop`). The line height moves from 30 to 32 to sit on the 4 px grid |
| Verdict and dialog title | `text-verdict` | 20 / 28 | 600 | 20 / 28 | Vercel's card titles are 20. Only Status's verdict and dialog titles use it |
| Section heading | `text-heading` | 16 / 24 | 600 | 16 / 24 | Linear 15–16, incident.io 16, AI Studio 16. Replaces the 15 headings on Usage and Settings |
| Body and row name | `text-body` | 15 / 22 | 400; 500 for a row's name | 15 / 22 | The benchmarks' 14 at Hanken's x-height (14 × 0.54 ÷ 0.493 = 15.3). Ends the 14-or-15 split (Home 14, People 15) |
| Secondary | `text-meta` | 14 / 20 | 400, muted ink | 14 / 20 | Linear 13 and Stripe 13–14, corrected for Hanken. Keeps D1.6's floor of 14 |
| Label (table header, field label, group label, key in a key–value list) | `text-label` | 13 / 18 | 500 | 13 / 18 | Stripe th 13 / 600, Mistral th 13 / 500, corrected. Replaces 12 / 400 table headers (People) and 14 / 500 field labels (Usage) |
| Badge, chip, duration | `text-badge` | 12 / 16 | 500 | 12 / 16 | Stripe status badge 12 / 600, Vercel tag 12. Library's 10 px badges go to 12 |
| Figure (one KPI number) | `text-figure` | 24 / 32, tabular | 600 | 20 / 28 | OpenAI Usage KPI 24–28 / 600 (`usage/openai-usage-spend-desktop`) |
| Person initial | `text-2xs` | 10 / 12 | 600 | same | Unchanged: initials inside a 20 px mark only |

- **At most four text roles on one page, plus badges.** Linear uses 3 and Vercel and Stripe 4. Home today uses 5 sizes and 3 heading levels.
- **Two heading levels per page: title and section.** Delete sub-headings such as "Your usual" and "Everything else": the section heading plus the layout carry that grouping. Vercel, Linear and Stripe show no third level on any measured screen.
- **Weights 400, 500 and 600 only.** Every benchmark except Stripe's 700 title does the same.
- Mapping: `text-title` = today's `text-2xl` with line height 32. `text-heading` = `text-lg`. `text-body` = `text-base`. `text-meta` = `text-sm`. `text-badge` = `text-xs`. `text-label` is new (there is no 13 step today). The old utilities stay as aliases until every surface has moved, and then `token_drift.py` flags them.

### Spacing and page geometry

| Token (new) | Value | Reason |
|---|---|---|
| `--page-pad` | 32 at ≥ 1024, 24 at 640–1023, 20 on phone | Vercel 27, OpenAI 20, Stripe 38 beside the sidebar; Linear iOS 20. Today 48, which reads as a sparse template on every page |
| `--page-top` | 32 at ≥ 640, 24 on phone | Vercel's title sits 30–40 below the top bar. Today 40 |
| `--content-max` | 1200 (unchanged) for list, table and grid pages | Vercel and Stripe tables run full width |
| `--content-read` | 720 for settings, detail, Status and the Try form | Linear settings 610, incident.io 670. Settings today is 1024 wide |
| `--space-section` | 32 between sections, always | Vercel 30, Linear 30, incident.io 24. Today 0–120 within one page (Workflows) |
| `--space-heading` | 12 from a section heading to its content | incident.io 12, Linear 16 |
| `--space-title` | 24 from the page header to the first section; 4 from the title to a one-line subtitle | Vercel 27, Linear 38 |
| `--space-stack` | 16 between blocks inside a card; 8 between a field label and its control (6 at 13 px) | Vercel and Linear forms |

### Controls

| Control | Desktop | Phone (< 640) | Reason |
|---|---|---|---|
| Button, `size="sm"` (**the new default**): page header, toolbar, row and card actions | 32, padding 0 12, `text-meta` 500, icon 16 | 44 | Vercel 32, Linear 32, OpenAI 32, Stripe 28–32 |
| Button, `size="md"`: a form's submit, the Try Run button, a dialog's primary | 40, padding 0 16, `text-body` 500 | 44; 48 when full width at the bottom | Vercel Invite 40 in forms, Mistral 40, ElevenLabs 36 |
| Input and select in a form | 40 | 44 | Vercel 40, ElevenLabs 40 |
| Input, select and segmented control in a toolbar or filter row | 32 (segments 28) | 44 | Linear and AI Studio 32, Vercel segmented 32 with 24–28 segments |
| Textarea | min 80, grows to 240 | min 96 | AI Studio composer 105 |
| Tabs (`line`) | 40 tall, labels `text-meta` 500, 2 px indicator | 44 | Vercel, Mistral and OpenAI underline tabs at 40–44 |
| Toggle (Switch) | 32 × 20 | 44 hit area | Linear 29 × 20 |
| Icon button | 32 square | 44 | Vercel "…" row menus |
| **One rule:** controls that share a row share a height | | | Every benchmark. Usage's header mixes 44 selects with a 40 button today |
| **One rule:** 44 px applies below 640 only (`max-sm:`) | | | 76 unprefixed `min-h-11` and 17 `h-11` put phone sizes on the desktop today (`MEASUREMENTS.md` §2.3) |

Desktop therefore uses **two control heights, 32 and 40**, where today it uses six.

- **Field edge.** Inputs and selects keep `--input` (3:1 against the card). The benchmarks use a lighter hairline (about 1.3:1), but WCAG 1.4.11 needs a 3:1 boundary to identify a text field, and T-4 requires AA. This is a deliberate exception to "match the benchmarks", and it is recorded here so no later round reopens it. Outline buttons keep the hairline `--border`, which darkens to `--input` on hover, as today. A shorter control (32 rather than 44) is what removes most of the heaviness the judges saw.

### Radius, edges, elevation

| Token | Value | Reason |
|---|---|---|
| `--radius` (controls) | 6 (unchanged) | Vercel, Linear, Stripe |
| `--radius-card` (cards, dialogs, popovers) | **8** (was 10) | Linear, incident.io and Vercel 8. 10 reads as the kit's `rounded-xl` |
| `--radius-badge` (new) | 4 for status badges and chips | Stripe 4. Today badges are full pills |
| `--radius-media` | 6 (unchanged) | Midjourney 8, Apple Photos 0–4 |
| Full radius | person marks, avatars, the play disc, toggles only | |
| Card edge | 1 px `--border`, **no shadow** | All 14 measured screens. The kit Card's `shadow-sm` goes |
| Shadow | `--shadow-overlay` for menus, popovers, dialogs and toasts only. `--shadow-float` for the Try composer when it docks at the bottom | Vercel role menu, Linear toast, AI Studio composer |

### List, table and card anatomy

| Part | Numbers | Reason |
|---|---|---|
| **List card** (`ListCard`, new) | A bordered card (radius 8, no padding of its own). Rows have 16 px horizontal padding and hairline dividers. An optional header band of 36 holds `text-label` in muted ink | Vercel Members, Linear Security, Mistral keys |
| One-line row | 40 (36 when `dense`, for Usage and Activity tables) | Stripe 35–37, Vercel Usage 38 |
| Two-line row (name and secondary) | 60: name `text-body` 500, secondary `text-meta` muted, 2 px between them | Mistral 58, Linear 60–62, Vercel models 61. People today 75, Usage 77 |
| Leading element | a 32 avatar or person mark, or a 16 icon in ink; 12 gap | Vercel avatars 30–32 |
| Trailing element | meta right-aligned in `text-meta`, numbers tabular; a 32 row action or "…" icon button | Vercel, Stripe |
| Phone row | one line 48, two lines 64. Secondary facts collapse into the second line, joined by " · ", and never stack into a third and fourth line | Linear iOS 48, Instacart 64. People today 165 |
| **Table** (`components/ui/table.tsx`) | header 36, `text-label` muted, no fill; rows 40 (`dense` 36); numbers right-aligned and tabular; the first column is the name in `text-body` 500 | Stripe Transactions, Vercel models and Usage |
| **Setting row** (`SettingRow`, new) | min height 64, padding 10 × 16 (22 + 2 + 20 + 20 = 64; 12 gave 68): label `text-body` 500 and one line of description in `text-meta`, the control right-aligned at 32. Rows sit in one card per section, under a `text-heading` with a 12 gap | Linear Security and Preferences 60–62, Vercel Firewall 80. Settings today 113 |
| **Key–value list** (detail pages, receipts, Status facts) | rows 32–36; key `text-meta` muted in a 160 column (stacked on phone); value `text-body` | Stripe Workbench detail rows 31 |
| **Content card** | padding 24 (16 on phone); title `text-heading`; body `text-body`; blocks 16 apart; actions at the bottom left or in the title row | Vercel 24–32, incident.io 16 |
| **Stat strip** (`StatStrip`, new) | one bordered card with up to 4 cells divided by hairlines; each cell 16 × 20 padding, a `text-meta` label above a `text-figure` number, an optional `text-meta` line under it; no icon tiles | Vercel Usage credit card, OpenAI Usage, Stripe Transactions filter tiles (66 tall) |
| **Media grid** | gutter 6 (4 on phone), unchanged and within Apple Photos' 2 and Midjourney's 8; square tiles, radius 6; **at most two cues always on a tile**: the type and length badge on video and audio, and the maker mark (shrunk from 20 to 16) when Everyone is shown. The day chip is the one exception: it stays visible on the first tile of each day, because removing dates is what lost round 6 ("no dates"). The video play disc and the title show on hover or focus only, since the type badge already says "video". Audio keeps its play disc, because it has no picture | Apple Photos shows only the duration on video; Midjourney shows nothing. Today a tile carries up to 4 cues, and one viewport holds 24 maker marks in 7 hues. The exception guards against the caption and date-group oscillation recorded in D1.5 and D1.6 |

### Page templates

Every route uses one of these five. A route that fits none needs a director decision.

| Template | Routes | Numbers |
|---|---|---|
| **List page** | People, Apps and keys, Models, Workflows, Activity, Engines, Providers | Header: title, optional count in `text-heading` subtle, and one primary action (32) on the right; optional one-line subtitle in `text-meta`. Toolbar 24 below it: search 32 (max 320 wide), then filters and segments at 32, all one row. Then one ListCard or table at full width (≤ 1200). Footer count in `text-meta`. Empty, loading and error states draw inside the card |
| **Detail page** | an engine, a receipt, a person, a workflow, an app | A back link (`text-meta`), title and state badge, and actions at 32 on the right. Facts as a key–value list. Sections 32 apart in a 720 column |
| **Settings page** | Settings, a person's access plan, an app's settings | A 720 column. Each section is a heading and one card of SettingRows. No section tabs or jump links while every section fits on the page (ban 22) |
| **Overview page** | Home, Status, Usage | Title and verdict line; a StatStrip when there are figures; sections 32 apart. Status and Usage use the 720 column for prose and cards, and Usage's chart and table run full width |
| **Creation page** | Try (task pages and Playground) | At ≥ 1024, two panes: the composer at 400, sticky, with form controls at 40 and Run at 40 at the end of the form; and the results feed filling the rest, 24 away. This matches `MakePage` in `briefs/try-redesign-plan.md` §4.1. The result pane shows the last result or an example, never an empty dashed box. On phone, the form comes first and the new result appears directly under the Run button (44), with nothing sticky, as in `briefs/try-redesign-plan.md` §4.1: the 4-tab bar is already fixed. The content fills the viewport: no page ends above 70 % of 900 px (ban 27) |

### Status display

The words and rules are in "Status model" above. Only the drawing is set here.

| Element | Numbers | Use | Reason |
|---|---|---|---|
| `StateBadge` | 20 tall, radius 4, padding 0 6, a 14 icon and the word in `text-badge`, tint background and state ink | A row or card title whose state is not Ready | Stripe "Failed ✕" and Vercel "Invitation Expired" are 20 tall |
| Quiet Ready | a 14 success icon and the word in `text-meta` muted, or nothing at all in a row | Lists where most things work | Supabase "Healthy" is muted under a small green check |
| Dot | 8 | The shell health line and chart legends | Vercel Usage legend dots |
| Strip | cells 24 tall on a problem card, 16 in a table | Status, Engines | incident.io 16 |
| Verdict line | `text-verdict` on Status, one `text-body` line on Home | | Unchanged from principle 1 |
| Colour share | Status hues only inside badges, dots, strips and meters: never on a card edge, a row background or a page band | | Benchmarks: 0.1–1.7 % coloured pixels |

### Colour budget

- **Outside media and charts, coloured pixels stay at or under 2 % of a 1440 × 900 screen, and at or under 10 % of the ink** (benchmarks: 0.1–1.7 % and a median of 5 %; `tools/colorshare.py`). Usage today is at 10.6 % and 70 %.
- **Charts:** one series takes one hue, `--chart-1` (the primary teal). A breakdown shows at most 3 hues plus "Other" in `--neutral`. Capability hues never fill bars. The breakdown moves to the table under the chart, with 8 px dots (Vercel Usage: single-hue bars and a dotted legend; OpenAI: one purple).
- **Icons are ink**, `--muted-foreground` or `--foreground`. Capability hues colour the 16 px glyph only where the capability is the subject (the Try chip, the Library type badge), and never as a 12 % tinted tile. Every measured benchmark draws neutral icons, and Supabase's are in bordered neutral tiles. This retires the "pastel tile grid" the Linear-designer red team named in round 1.
- **People** keep their hue in marks of 16–32 px only (principle 6).

### Icons

16 in navigation, rows, buttons and inputs (Vercel and Linear nav glyphs measure 14 in a 16 box); 14 in badges; 20 for the verdict icon, phone tabs and an empty state's icon. Nothing larger except media cues (the 40 play disc). Stroke 1.75 (unchanged).

### Empty, loading and error

| State | Numbers | Reason |
|---|---|---|
| Empty | Drawn inside the region it would fill (the ListCard or the grid), left-aligned: a 20 icon in muted ink, a title in `text-body` 500, one sentence in `text-meta`, one action at 32. Minimum height 160. A dashed 1 px `--border` only when the region has no card of its own | Geist empty state (`empty-error/vercel-geist-empty-state-desktop`); ban 23 |
| Loading | Skeleton rows at the exact row heights (3 rows of 40 or 60), shown after 300 ms; no full-page spinner. A 16 spinner inside a button while it works | Stripe and Vercel tables |
| Error | A Callout in place of the region: title in `text-body` 600 naming the layer, one sentence in `text-meta`, Retry at 32; the HTTP code under "Technical details". Never a toast for a page that failed to load | P3, VOICE rule 4 |

### Gates for these numbers

PR 1 turns `craft-system/azizam/_scripts/measure.mjs` and `summarize.py` into a check, `scripts/ui/craft_numbers.py --fail`, that runs on the fixture at 1440 and 390. It fails when any of these holds:
- desktop uses a control height outside {32, 40} (media tiles and textareas excepted);
- a page uses more than 4 text sizes plus badge text, or any size outside the type table;
- a row in a ListCard or table is outside {36, 40, 60} on desktop or {48, 64} on phone;
- the page padding or section gaps differ from the tokens;
- a card has a shadow or a radius other than 8;
- the coloured-pixel budget is broken outside media and charts.

How it runs (PR 1): `scripts/ui/craft_numbers.py [--fail] [--routes home,status] [--manifest PATH]` starts the household fixture on `UI_E2E_PORT` (or measures a running one), runs `measure.mjs` and reports every check for every measured route. `--fail` fails only the checks `scripts/ui/craft_numbers.json` enforces for a route. PR 1 enforces the control heights on Home, Status and Library, the pages that already use the shared parts; each surface PR turns on the rest for its routes. Exempt from the control check: buttons taller than 64 (media tiles, whole-card buttons), textareas, switches, checkboxes and link-styled buttons; a tab or segment counts as its group. A 10 px text is allowed only as a person mark's initial.

`token_drift.py` also flags unprefixed `min-h-11` and `h-11`, `rounded-lg` and `rounded-xl` in `app/`, `shadow-*` on non-overlay elements, and the old text utilities once their surface has moved. They fail in the files listed in its `SYSTEM_FILES` (PR 1: the shared parts) and are warnings elsewhere; a `craft-allow` comment exempts one line and says why (Button's `lg`, the full-width phone action).

## Shell

- **Sidebar (≥ 1024 px):** 232 px; a quiet light panel with a hairline edge in light theme, deep water in dark. The brand mark and name (plus the instance name when it differs), a search field with its shortcut, then two groups: **You** (Home, Library, Try, Apps & keys, Models, Workflows, Usage) and, for owners, **Gateway** (Status, Engines, Providers, People, Activity, Settings). Members see Status in You. Order is by how often a household member needs each place. Every destination has its own icon (ban 19). The footer holds the health line (a dot and the shared verdict count, Status model rule 3; its popover separates gateway reachability from "Your AI"), the account (person mark and name) and the theme toggle. Nav items are 32 px, 13 px text, medium weight only for the current page; group labels in subtle ink. D1.6: one type size and one row height for every nav item; the owner's Gateway group is set apart by space and its small label only (D1.5 made it smaller and quieter, and round 6 read the two sizes as incoherent).
- **Rail (640–1023 px):** 72 px icon rail with tooltips; a menu button opens the full list as a sheet.
- **Phone (< 640 px):** a 56 px header in the chrome colour with the mark, the health dot (opens the same popover) and the person mark (account menu). A bottom bar with four labelled tabs: Home, Library, Apps, More (Midjourney 3a.4: four or fewer). More opens every destination, search and the theme toggle.
- **Owner alert:** when an engine in use is down or a provider fails its check, a one-line warning band ("Image engine is down. Check its connection on Status.") sits above the page on every route except Status, with "View status"; the health popover repeats it for owners.
- **One search per screen (D1.2):** the sidebar's entry is "Go to…", a navigation row with its shortcut, not a search field; it opens the command palette. Pages search their own content (Library: "Search your creations"), so a screen never shows two search boxes.
- **Command palette (⌘K / Ctrl K, "Go to"):** pages, your apps, models, people, and three actions: create a key, copy the gateway address, check gateway reachability. Focus returns to its opener; when the opener was the navigation sheet's "Go to…" button, focus returns to the control that opened the sheet.

## Components

| Component | File | Job |
|---|---|---|
| Status model | `lib/capability-state.ts`, `lib/use-capability-views.ts` | The state words, the rules above, the verdict; `useCapabilityViews(owner)` gathers the evidence |
| PageHeader | `components/product/page-header.tsx` | The one page header: `text-title`, optional count in `text-heading` subtle, actions at 32 on the right, a `text-meta` subtitle 4 under the title, the StatusLine under it, 24 to the first section. `tight` (Home) keeps a one-line status close under the title, as its subtitle |
| StatusLine | `components/product/status-ui.tsx` | The verdict label with its icon and "Checked …" age; `role=status`, `aria-atomic`. `compact` draws it as one 14 px line (Home) |
| StateWord, StateIcon | same | One icon, colour and word per state; Ready stays quiet. Icons 16 in rows, 14 in badges, 20 beside the verdict |
| StateBadge | same, on `components/ui/badge.tsx` (`variant="state"`) | A state that is not Ready, on a row or card title: 20 tall, radius 4, the 14 icon and the word in `text-badge`, the state's ink on its tint (4.5:1 or better, `state_palette.py`) |
| Callout | same | The one callout for every problem, notice and error: severity icon, a title naming the subject, meta, facts, body, actions. On wide screens the header action sits on the title row; on a phone it follows the body, so the instruction comes before its button, and everything under the title uses the full width. `evidence` facts follow `facts` on wide screens and fold behind "Show details" on a phone. D1.4 phone order: title, facts (scope, history), the instruction, its button, then evidence |
| CapabilityList | same | The one way to draw capabilities: rows of 40 (60 with the reason or age, 48 on a phone), the capability icon in ink, the name in `text-body` 500, the state word; one or two columns |
| CapabilityChips | same | While problem cards are open, the capabilities as 40 px rows (48 on a phone) in one card (state icon, name, state word when not Ready): one column on a phone, two from 640 px, under the cards (D1.4 replaced the wrapping pills; D1.5b removed the side column) |
| AudioFace, MediaBadge, PlayDisc, `playDiscButton`, DayChip, Waveform, `audioKind` | `components/product/audio-cover.tsx` | The tile cues (D1.6), all on one dark scrim. AudioFace: one neutral face for every clip (sunken surface, real waveform or the glyph) with a `center` slot that lets clicks through to the tile; MediaBadge: type glyph and length, bottom left (the glyph alone without a length); PlayDisc: the 40 px play cue in the middle of video and audio tiles; `playDiscButton`: the disc as the in-place audio play button on wider screens; DayChip: the day on the first tile of each day. The waveform doubles as the position in the viewer |
| `useAudioPeaks` | `lib/audio-peaks.ts` | Peaks decoded from the file with Web Audio, one file at a time, cached; null (no waveform drawn) when decoding is unavailable |
| CapNotice | `components/product/cap-notice.tsx` | A spending-limit refusal for owner or member (rule 6) |
| SectionHeading | `components/product/readiness.tsx` | Every titled region: `text-heading`, 12 above its content, optional count and one action at 32 |
| `capabilityMeta` | same | Capability name, icon and hue |
| UptimeBar, `hourSlots`, `hoursUp`, `failingSince`, `healthHistory`, `healthStory` | `components/product/uptime-bar.tsx` | Rule 4: 24 hourly segments, down hours taller as well as red, a legend of the colours shown, hour-by-hour list (`hourByHour={false}` in the engine table). `size="lg"` on problem cards: 24 px cells, the legend above the strip, clock times every six hours and "Now" under it. `healthStory` gives a card its two plain lines (rule 4) |
| AppMark | `components/product/app-mark.tsx` | Real marks for known apps (Simple Icons, CC0) and a monogram otherwise |
| PersonMark | `components/product/person-mark.tsx` | A person's initial in their stable hue |
| BrandMark | `components/product/brand-mark.tsx` | The fish, in the chrome accent |
| Segmented, Tabs | `components/product/segmented.tsx`, `components/ui/tabs.tsx` | The only segment pattern for filtering, and the tab pattern. D1.5: no sunken track; quiet chips, the chosen one in the accent tint and semibold (the sidebar's "you are here"). Options may carry an icon (hidden on phones, where four segments share the width). Tabs' `line` variant (People) keeps its underline. Segmented and chip tabs are a 32 group of 28 segments; `line` tabs are 40 with a 2 px indicator; all 44 on a phone |
| Disclosure | `app/globals.css` (`summary`) | Every `<summary>` draws one chevron in the text colour that turns 90° when open; a summary with `list-none` draws its own. Disclosure buttons (the Callout's "Show details") lead with the same chevron |
| CopyField | `components/product/copy-field.tsx` | One boxed value with the copy button inside its edge (Cloudflare 6a.2) |
| Button | `components/ui/button.tsx` | default (primary), outline, secondary, ghost, link, destructive (D1.5 removed `cover`). Sizes: 32 by default (also `sm`), `md` 40 for a form's submit, Run and a dialog's primary, `icon` 32, `lg` 44 for a full-width phone action; every size 44 below 640 |
| Input, Select, Popover, Dialog, Sheet, DropdownMenu, Tooltip, Skeleton | `components/ui/*` | shadcn primitives on the tokens. Input and Select: `md` 40 in forms (the default), `sm` 32 in toolbars, 44 on a phone. `SkeletonRows` draws loading rows at the row heights |
| Card | `components/ui/card.tsx` | The content card: radius 8, a hairline, no shadow, padding 24 (16 on a phone), blocks 16 apart |
| Table | `components/ui/table.tsx` | A 36 header in `text-label` subtle ink with no fill, rows of 40 (`dense` 36), `numeric` cells right-aligned in tabular figures |
| ListCard, ListRow | `components/product/list-card.tsx` | The list template: a bordered card with an optional 36 header band and hairline rows at 40 (`lines={1}`) or 60 (`lines={2}`), 48 and 64 on a phone; a leading mark or icon, the title in `text-body` 500, a `text-meta` second line, trailing facts (folded into the second line with " · " on a phone) and a 32 row action; `href` opens the row (craft PR 1, director decision) |
| SettingsSection, SettingRow | `components/product/setting-row.tsx` | The settings template: a `text-heading` 12 above one card of rows at least 64 tall, each a label in `text-body` 500, one line of description and the control right-aligned at 32 (craft PR 1, director decision) |
| StatStrip, Stat | `components/product/stat-strip.tsx` | Up to four figures in one bordered card divided by hairlines: a `text-meta` label, the `text-figure` number, an optional note; no icon tiles (craft PR 1, director decision) |
| KeyValueList | `components/product/key-value.tsx` | Facts on a detail page, a receipt or a Status card: 32 rows, the key in `text-meta` muted in a 160 column (stacked on a phone), the value in `text-body` (craft PR 1, director decision) |
| ChartLegend, `chartColors` | `components/ui/charts.tsx` | A breakdown's colours (three hues plus Other) and its legend of 8 px dots |
| EmptyState | `components/product/empty-state.tsx` | Drawn in the region it would fill, left-aligned, at least 160 tall: a 20 icon in muted ink, the title in `text-body` 500, one sentence in `text-meta`, the one action at 32 (Geist 12c.2); a dashed edge only outside a card (`inCard` drops it); never a centred illustration |
| ErrorState | `components/product/error-state.tsx` | A Callout with heading, an optional sentence, retry, and the HTTP code under "Technical details" |
| Favourites and task rows | `app/home/page.tsx` | D1.4: the person's two most-used tasks (from their own successful requests; pictures and chat for someone new) as two 128 px tiles with "Last used 22m ago"; every other task a quiet 44 px row with a small icon in its colour, no chip; a state only when not Ready; a link into Try with the right mode, or a plain row with the reason when unusable. D1.5: the rows sit in one card under the tiles, with hairlines between rows (one column on a phone, two on a wide card), so the section reads as one family of cards. D1.6: "Your usual" ("Start here" for someone new) heads the tiles and "Everything else" the rows, so the hierarchy says why |
| Problem card | `app/status/status-client.tsx` | A Callout in the `incident` variant (D1.6), one order at every width: a title that names the consequence ("Video requests can fail right now"; the verdict above names the state, so nothing repeats), "What's wrong" (the cause) and "Affects" (scope and who already hit it); what to do in full-weight 16 px text with its button right under it; then "Last 24 hours" (the story, one line tying red hours to the verdict, the 32 px strip with 14 px clock labels); then evidence behind "Show details". Only the first card's action is primary |
| Layer table | same | Engines and providers: name, serves, uptime bar, "16/24 h up", state word; spend meter and "USD 2.02 of 8.00 this month" on one line for providers. Rows are one line; the hour-by-hour list lives on the card and the engine sheet. Open only when nothing needs attention, or after "Show all" |
| Creation tile and viewer | `app/library/page.tsx`, `app/library/library-format.ts` | Square media tile in one continuous grid, with the day chip on the first tile of each day (`dayMarks`), the maker's mark with Everyone shown, and on video and audio the MediaBadge and PlayDisc; on hover or focus a short title (`captionTitle`, or `untitledName` when there is no prompt), maker and day (`tileDate`); audio as an AudioFace; viewer with stage, designed video and audio controls (audio on a plain card with its kind and a primary play button; its words appear once, in the details), facts and actions, and Share (the file itself) first where the browser has a share sheet |

Removed in D1.1: Verdict, StateLabel, `availabilityState`, EvidenceTime, `evidenceStamp` and Notice. They were a second status vocabulary; the Status model replaces them.

No new component without a director decision.

## Surfaces in this round

- **Home.** Title and a one-line verdict (with "Open Status", or for a member "See what is happening", when something is wrong), with "Connect an app" in the header on phones and tablets. Apple Home's order: "Recent", a strip of four creations whose files still exist (audio as an AudioFace with a play badge, video with a play badge), with placeholders for work still running (and "it may not finish" when its capability is failing); then "Make something": two favourite tiles and the other tasks as quiet rows in one card. The status line sits close under the title (`tight`), so no gap floats above Recent. "Connect an app" is the right-hand panel on desktop, aligned with the first list, and last on phones. A spending-limit refusal shows a CapNotice above the tasks.
- **Status, owner.** One reading column up to 896 px at every width, problem or not (D1.5b, like Linear's and incident.io's incident pages: a side column stood empty below its list). A problem card (D1.6) names the consequence, then "What's wrong" and "Affects" (what stops working, whether another engine covers it, and who already hit it, with their app), then what to do with its primary button, then "Last 24 hours" (the story and the strip), then evidence on request. The folded engines line is two sentences ("4 other engines are ready and 1 is turned off. 1 remote provider is set up."). Title, verdict label with check age, Refresh and "Technical details". "Needs attention": full-width split problem cards for engines (grouped when several fail the same way), providers, the gateway's computer (disk faults on their own card with "Free disk space") and access plans that reached a spending limit. "Running now" lists work in progress. "Capabilities": the CapabilityList in two columns. "Engines and providers": the layer table. While cards explain a problem, capabilities fold to two columns of 44 px rows and engines fold to one line of counts with "Show all" at every width (principle 7 ranks above filling space: D1.5's open table took owner Status from 86 to 212 words on the production copy, and D1.5b folded it again). On a phone the problem card is one column: each label sits above its value in 12 px medium, and the evidence itself is 14 px or larger; the header pill is hidden on Home and Status, which carry their own verdict.
- **Status, member.** The same header and verdict as Home; a CapNotice for a current refusal; "Your video request failed" for the latest failure with no later success, saying what state the capability is in now; "Your capabilities" (CapabilityList); "Model names" one tap away; "Your latest requests" (the gateway sends five) with Failed, Refused and In progress, each link named "View request receipt" and described by its capability, state and time.
- **Library.** Media only, newest first, in one continuous grid; the day ("Today", "Yesterday", "Monday", "Oct 20") rides on the first tile of each day (principle 8). Work still being made comes first. Three columns on a phone. The count is what the grid shows; files no longer stored are counted beside it ("4 no longer stored") with one explanation for owners and members. One control row: search first, then kind (chips, with icons), the owner's person filter (people's marks) and "Filters" with model, date and app (named "Filters 2 active" when set). On a phone one row holds the kind chips, a Search button and an icon Filters button, so the title and controls take under a fifth of an 844 px screen; the search field opens below the row and stays open while it has text; the person picker is inside Filters. Video and audio tiles carry the type glyph and length bottom left and the play disc in the middle; on audio the disc plays the clip in place on wider screens. With Everyone shown, each tile carries its maker's mark. The viewer shows audio on a plain card (kind, a primary play button, the waveform as position); the words are in the details beside it, once. Display names for models; the raw id only in Details. Videos without a poster show their first frame (a seek to 0.1 s plus a `#t=0.1` media fragment for Safari); real video jobs have no poster today (backend ask 8). The viewer has designed play, position, time and mute controls. Running work shows calm placeholders. Owners are warned before pictures expire. Posters the gateway lists as separate images are folded into their video.
- **Try.** The selected chip names the task; the card under it has no second heading. The chip strip keeps full labels and scrolls on a phone, with the chosen chip kept in view. Run is always enabled; an incomplete form says what is missing on the field ("Choose a picture to start from", "Describe the video first") and sends nothing. A form starts empty: the catalog's example body shows the request's shape, and its made-up values ("value", seed 0, a one-second clip) never become the person's input. A media input offers "Choose a file" (up to 10 MB, read in the browser) and "Choose from Library" with a preview; the link or data URI sits under Advanced. Inputs show plain labels and one helper line; type, request field and "Writes …" sit under "Input details". "Make a picture" lists only models that make a picture from a prompt (catalog `capabilities` intent and input modalities, and the manifest `task`); tools that start from a picture are under Workflow. A picture runs as a media job with an idempotency key and is polled, and polling resumes when the page is shown again, so switching apps cannot lose it. A dropped connection never claims failure and never shows the browser's words: "Your picture may still be finishing" with a link to the Library, or the picture itself when the Library already has it. A failed run's card offers "Edit and run again", which returns to the form with its values; it never repeats a request by itself.

## Token drift, bans and exceptions

- Run `scripts/ui/token_drift.py --fail` (default scope: the surfaces above and their primitives). Legacy routes report with `--all` and are out of D1 scope.
- Ban-list exceptions: none recorded.
- Benchmark exception (director decision, 2026-10-09): fields keep the darker `--input` edge (3:1, WCAG 1.4.11) where the benchmarks use about 1.3:1 (System numbers v2, "Field edge").
- Impeccable detector exception: `layout-transition` (height) comes from Sonner's own toast stylesheet, a third-party package; no Azizam style animates height.
- D1.4's detector exception (`text-overflow` on an audio cover's top line) is gone with the covers (D1.5).
- Console exception: signed-out pages get a 401 from `/admin/api/session` while the shell checks for a service key. It is expected and carries no user-facing error.

## How the gates run (T-4)

- `scripts/ui/token_drift.py --fail`: token drift, and the craft-system rules (unprefixed `min-h-11` and `h-11`, `rounded-lg` and `rounded-xl`, kit shadows outside overlays, `text-[Npx]`): failures in files moved to the system, warnings elsewhere.
- `scripts/ui/state_palette.py --fail`: state text 4.5:1, fills 3:1, Degraded and Down apart under every colour-vision simulation; StateBadge ink 4.5:1 on its tint; chart fills 3:1 on the card.
- `scripts/ui/craft_numbers.py --fail`: the System numbers v2 at 1440 and 390 on the household fixture ("Gates for these numbers"). `scripts/ui/craft_numbers.json` lists the checks each route enforces; a surface PR turns its route on.
- `node web/e2e/d1-gates.mjs <out>`: axe WCAG 2 AA (contrast included), horizontal overflow, clipped text, 44 px targets at 390, visible focus on every tab stop, console errors and failed requests; owner and member, both themes, both widths, plus the Library viewer.
- `node web/e2e/d1-snapshots.mjs <dir>` then `npx impeccable detect <url>` on each snapshot: the detector cannot sign in, so signed-in pages are saved without scripts and served locally.
- `~/.tmp/gw-product/audit/{boot.py,crawl.mjs,lint.py}` on a copy of the production data: repeated facts and word budgets.
- `scripts/ui/d1-states-capture <out>`: every state and scenario for the contact sheet.

## Backend asks

1. **Owner name and contact for members.** Member payloads have no owner display name or contact, so copy says "Ask the gateway owner" where VOICE wants "Ask Sam". Add `gateway.owner_name` (and an optional `owner_contact`) to `/me/api/home` and `/me/api/status`.
2. **Evidence source on `/me/api/status`.** Its capabilities carry `observed_at_s` without the evidence `source`, so member Status reads evidence from `/me/api/home`. Add `availability.evidence` to each status capability.
3. **Evidence time must be the check time.** `gateway/availability.py: evidence_from_status_rows` defaults a row's `observed_at_s` to now when the row has none, and the live health probe stamps a failed probe as "now". Stale evidence then looks fresh unless `kind: no_response` is read. Carry the last successful check time and the attempt time separately.
4. **Busy versus failing.** The projection maps `degraded` and `busy` to `available: null` and the capability status can read "busy" while requests fail with HTTP 503. The UI now downgrades Busy to Degraded when requests failed since the last success; the gateway should project 5xx failures as degraded or unavailable itself.
5. **Creation retention per kind.** `images.cache_ttl` covers images only; nothing states video or audio retention. Expose `retention_s` per kind in `/me/api/media`, and consider a longer default (24 hours empties a family's Library daily).
6. **Failed receipts filter.** `/admin/api/activity/requests` ignores a status filter, so Status samples the latest 50 requests. Add `status=failed` and `backend=<service_id>` filters, and counts over a stated window.
7. **Provider connection checks.** `checked_at` is null until a connection test runs. A periodic provider check (or the last successful request time per provider) would let Status say more than "Not checked yet".
8. **Video posters.** No real video job records a poster: on the live gateway 0 of 50 video assets carry `poster_asset_id` (`video_media_job_http.completed_asset_from_stored_video` builds the asset without one; only `scripts/ui/household_seed.py` writes one). Every live tile therefore loads the video's metadata to show a frame. Extract a poster frame when a video job completes and record it as `poster_asset_id`. Separately, the gateway lists a seeded poster as its own image in `/me/api/media`; the UI hides any asset another asset names as `poster_asset_id`, and the API should do that itself.
9. **Plan spend.** Members learn that a limit was reached only from refused receipts, and owners cannot see how much of a limit is used. Add a `plan_spend` block (spent, cap, period, resets_at) to `/me/api/home` and to `/admin/api/profiles`.
10. **Refusal reason on receipts.** The UI finds spending-limit refusals by matching warning text ("spending allowance", "cap … would exceed"). Put the `BudgetExceeded` code (`budget_exceeded`) and which limit (daily, monthly; plan, provider, global) on the receipt.
11. **Capability-to-engine map.** Status links engines to capabilities by the capability labels on each engine (`capability_labels`). An explicit list of the engines and providers that serve each capability, in routing order, would make "another engine still serves it" exact.
12. **In-flight work.** Running media jobs are not in `/me/api/media`. Home and Library show placeholders from running receipts in `/me/api/home`; a `running` list with progress would let them say "Making your video, 42%".
13. **Voice and speaker on audio.** `/me/api/media` audio items carry the model (`tts-1`) but not the voice or speaker used, so the viewer says "Voice" where it could say "Voice · Nova". Add `voice` to audio provenance.
14. **Ranged video delivery.** `/v1/assets/{id}/content` reads the whole file and re-runs the metadata strip (an ffmpeg remux for video) on every Range request (`proxy.py`, the asset content route), and the call is synchronous inside an async handler. A grid of posterless videos makes several Range requests per tile. Cache the stripped bytes per asset, or strip once at store time.
15. **Who a capability serves.** Status's "Affects" can say whose requests failed, but not who could be affected: nothing lists which people's plans include a capability. A per-capability list of plans (or a count of people) would let a card say "Video, for 4 of 5 people".
16. **Done in main 00f10bc (merged in D1.5b).** **Carry "degraded" through the capability projection (P0, red team round 2).** `gateway/availability.py:22` puts "degraded" in `BUSY_STATUSES`, so `_from_evidence` (lines 344-348) projects a degraded engine as capability status "busy"; `/me/api/home` and `/me/api/status` then say `status: "busy", reason_code: "busy"`, while `/admin/api/status` keeps the raw "degraded" (`gateway/admin_api.py:612-617`). Members and owners got two stories about one engine. The UI no longer reads "busy" as a queue without an engine check; the gateway should project "degraded" (or a `reason_code` that says so) so members can be told "answering with errors".
17. **Failed creations in `/me/api/media`.** Only succeeded jobs are listed, so a failed video never appears where it would have been (red team G3). List failed and refused media jobs with their state.
18. **Engine capability labels are incomplete.** In the household fixture the voice engine serves voice, transcription, music and sound effects, but its `capability_labels` name only Music and Sound effects, so Status cannot attribute Unknown Voice and Transcription to it (the UI now shows them on their own card).
19. **The members' request window.** `build_user_activity_snapshot(limit=5)` (`gateway/home_api.py:354`) gives members their latest five requests, so a failure the owner's card attributes to them can be missing from their list. Say the window in the payload, or page it.
