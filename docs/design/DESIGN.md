# Azizam design system (D1.2)

Owner: the D1 designer (Opus 5.5). Status: v1.5, 2026-10-09 (D1.5: a state palette that colour-blind readers can part, date groups and calm neutral audio in Library, plain-word evidence with clock labels on Status, one control vocabulary, a quieter owner group in the sidebar; D1.4: a media-only Library with hover titles and a three-column phone grid, a Home with favourites, a balanced Status, and red-team honesty fixes; D1.1: one status model, freshness, plan blocks, task-first Home, a system instead of pages; D1.2: media that identifies itself, one search, one row grammar, a craft pass against the benchmarks; D1.3: a problem card that tells the strip's story, scope before evidence, a Library control row, dated captions and one grid, a Home that leads with tasks and shows Recent on the first phone screen). Scope: the system, the shell, Home, Status and Library. Other routes inherit the tokens and the shell; their own layouts come in later rounds.

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
8. **Media is the content; the grid is organised the way Apple Photos does it.** D1.5 decided the Library by principle instead of by patch (captions were cut, then "no cues"; week groups were cut, then "no dates"). The rules: date groups with quiet headers (Today, Yesterday, This week, then the month), where a part-filled last row is normal; no captions in the grid (a wide screen shows a short whole-word title, the maker and the day on hover or focus); a small duration badge on video and audio, which is the media-type cue; the person filter is how to find who made something. Audio has no picture, so every clip gets one calm neutral face in the grid's rhythm: the sunken surface, its real waveform (or the glyph) and the badge with its length, which also plays it. No colour per person, no words on the tile: voice clips must never be the loudest thing on the page. Never a blank square. Selection opens a viewer with the media on the media stage and the facts beside it (Apple Photos 3c, Suno 3b.2).
9. **One pattern per job.** One segmented control (ban 21), one focus ring, one section heading, one table rhythm, one dialog grammar, one disclosure chevron, one "selected" (the accent tint, on chips, tabs and the sidebar's current place). D1.5: controls are crafted from the tokens, never the kit default (a sunken track with a raised white segment, the browser's disclosure triangle).
10. **Not a toy.** No slogans, no gradients, no glow, no pill primary buttons, no tracked-caps eyebrows (bans 6, 13, 15, A4, A5; Tim 2026-10-03 "not a toy").

## Status model

One per-capability state, derived for the person who is looking, feeds every surface: the Home verdict and tasks, the Status verdict and capability list, the member's failure callout and the shell's health line. Code: `web/src/lib/capability-state.ts` (the rules) and `web/src/lib/use-capability-views.ts` (the evidence for an owner or a member). Tests: `web/tests/lib/capability-state.spec.ts`.

### The words

| Word | Tone | Means | Never means |
|---|---|---|---|
| **Ready** | quiet (green icon, muted word) | A check or request newer than 15 minutes says it works, nothing failed since, and no limit applies. | "Configured", or "it worked yesterday". |
| **Busy** | blue | An engine check itself says it is working on other requests, or the gateway reports it starting up, and no request for it has failed since its last success. | A failure. A busy report next to a failed request becomes Degraded. The capability payload's "busy" alone is not enough: the gateway also uses it for a degraded engine (backend ask 16), so without an engine check it reads Degraded, "May be slow or fail right now." |
| **Degraded** | amber | It works, but not fully: one of its engines fails while another serves it; its only engine answers badly; a request failed in the last hour with no success since; or a spending limit paused some paid models. | Down. |
| **Down** | red | Every engine or provider that serves it fails its check, or the gateway reports it unavailable. | Off. |
| **Blocked** | violet, with a lock | The person's access plan does not include it. | A fault. |
| **Unknown** | grey dashed | There is no check, or the last one is older than 15 minutes, or it did not answer. The detail gives the age of the last recorded check when there is one ("Last checked 7 h ago; no answer since"). | Ready, or zero. |
| **Off** | grey | Turned off by the owner, or not set up yet. Off capabilities do not count against the verdict. | A fault. |

Receipts use three words of their own: **Failed** (finished with an error), **Refused** (the gateway did not run it, e.g. a spending limit), **In progress** (queued or running; never counted as a failure).

### Rules

1. **Order.** Off and Blocked come from the access plan and configuration. Then engines: if every engine for the capability fails, it is Down (or Degraded if they all still answer). Then evidence: none, or older than 15 minutes, is Unknown. Then one failing engine of several is Degraded; the gateway's "unavailable" is Down; "starting" is Busy; "busy" is Busy only when an engine check says busy and no request failed since the last success, else Degraded; an active spending-limit refusal or an unresolved failure in the last hour is Degraded; otherwise Ready.
2. **Verdict.** "Everything works" only when every counted capability is Ready. One or two exceptions are named ("Video down, chat degraded"); more read "3 of 8 capabilities need attention"; all Down reads "Nothing works right now"; nothing counted reads "Nothing is turned on yet". The check time beside the verdict counts the capabilities it does not vouch for: "Checked just now · 4 unknown". Owner Status adds problems the capability model cannot see (the gateway's computer, an access plan) and checks whose data is missing: "Checking", "Couldn't check status", "Engine health unknown", "Some engines are unchecked", "Provider status unavailable". None of those may read as working.
3. **The shell.** The health line says "Gateway unreachable" only for reachability; otherwise it counts the same capability states: "All working", "1 issue", "Busy" (only busy exceptions; Busy is not an issue), "Nothing turned on", or "Status unknown" when the states (or an owner's engine checks) did not load. "Gateway reachable" appears only to someone signed out or using a service key. It never reads as all-clear while a capability the person depends on fails.
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
| Secondary ink | `--muted-foreground` | #4F6268 | #9AB0B6 | Labels, metadata (5.5:1 or better) |
| Hairline | `--border` | #D7E0E2 | #213E46 | Structure |
| Control edge | `--input` | #7B8E93 | #58777F | Inputs and outline buttons (3:1) |
| Action | `--primary` / `-foreground` | #0D4552 / white | #1F6F79 / white | The one primary button per view (D1.1: the old sea-glass dark primary read as a disabled button) |
| Link | `--link` | #0A6676 | #8CC8CB | Text links |
| Selected | `--accent` / `--accent-foreground` | #DCF0F2 / #0B4A57 | #163C45 / #8FE0E7 | Selected options |
| Focus | `--ring` | #1A8C9C | #8CC8CB | 2 px outline, 2 px offset |
| Chrome | `--chrome` | #EEF2F3 | #08171C | Sidebar, phone header (D1.1: quiet light panel in light theme) |
| Chrome raised | `--chrome-raised` | #E1E8EA | #13292F | Hover and current item |
| Chrome text | `--chrome-foreground` / `--chrome-muted` | #10232A / #4F6268 | #E3EEF0 / #93B0B7 | |
| Chrome edge | `--chrome-border` | #D7E0E2 | #1A333A | Sidebar right edge, phone header bottom |
| Chrome accent | `--chrome-accent` | #D2702A (goldfish) | #F2A65A | The brand mark; used nowhere else |
| Media stage | `--media-stage` / `-foreground` | #E6ECEE / #10232A | #050F12 / #E3EEF0 | Behind media in the viewer |
| Media badge | `--media-badge` / `-foreground` / `-hover` | rgb(5 15 18 / 64%) / white / 80% | same | The duration badge on video and audio tiles and the audio play badge (white text 9:1 or better over the scrim on white) |
| Subtle ink | `--subtle-foreground` | #5E7076 | #8197A0 | Group labels, axis labels, quiet counts (4.5:1 or better on page and chrome) |
| Success | `--success` / tint / fill | #15703F / #E3F3EA / #2E9E63 | #6FD39B / #12352A / #4CC283 | Ready |
| Warning | `--warning` / tint / fill | #B45309 / #FDF1DC / #D97706 | #F5B544 / #3A2E15 / #F2A93B | Degraded, Refused |
| Danger | `--danger` / tint / fill | #B3261E / #FCE9E7 / #C62828 | #FF8F85 / #3F1F1E / #F0655A | Down, Failed |
| Info | `--info` / tint / fill | #1F5FA6 / #E6EFFA / #3B82D6 | #8DB8F2 / #182E4A / #6AA2EC | Busy, In progress |
| Blocked | `--blocked` / tint / fill | #6B4FB0 / #EFE9FB / #5B3A9E | #C2A8F0 / #2A2340 / #D6C4FF | Blocked (with a lock icon) |
| Neutral | `--neutral`, `--nocheck-fill` | #6A7C81, #C9D3D6 | #8AA1A7, #3B5961 | Off, Unknown (with the subtle ink for the word); an hour with no check |
| People | `--person-1…7`, `--person-ink` | coral, sky, sage, violet, amber, rose, teal on #10232A | same | Person marks (all 6:1 or better) |
| Capabilities | `--capability-*` | seven hues | lighter pairs | Capability icons and tints at 12% |

All text pairs pass WCAG AA in both themes: axe colour-contrast passes on every gated page (owner and member; Home, Status, Library and the viewer; light and dark; 1440 and 390).

#### State palette checks (D1.5)

Judges read the old Degraded (#8F5300 text, a brown fill) as muddy and close to Down. Each state now has a text colour (words and icons) and a fill (strip cells, dots, legend swatches, meters). Degraded is a clear amber that is much lighter than Down's red, so the two part by lightness, which survives every colour-vision deficiency; Blocked is a violet with a lock, deep in light theme and pale in dark, so it parts from Busy's mid blue by lightness too. Every state still has its own icon and word (principle 2), and down hours are taller than up hours in a strip.

Contrast (WCAG 2 ratio; text against card and page, fills against card; checked with `~/.tmp/d12c/palette.py`, the WCAG relative-luminance formula):

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
| `text-base` | 14 / 22 | 400–600 | Body; callout titles and row names at 500–600 |
| `text-sm` | 13 / 20 | 400–500 | Rows, controls, facts |
| `text-xs` | 12 / 16 | 500 | Metadata, durations, axis labels |
| `text-2xs` | 10 / 12 | 600 | Initials inside a 20 px person mark only |

### Spacing, radius, elevation, motion

- **Spacing:** Tailwind's 4 px scale (`--spacing: 0.25rem`). Page gutters 20 / 32 / 48 px at 390 / 640 / 1024+. Section rhythm 40 px; inside a section 12 px; rows 8–12 px vertical. Content width caps at 1200 px.
- **Radius:** controls 6 px (`rounded-control`), panels and dialogs 10 px (`rounded-panel`), media 6 px (`rounded-media`), uptime segments 2 px, people and the round play button full. No pill buttons (ban 13).
- **Elevation:** borders build structure; a shadow only lifts something above the page: menus, popovers, dialogs, the viewer (`--shadow-overlay`). Cards never carry a shadow (ban A4).
- **Icons:** lucide at 16 px in rows and 20 px for state, stroke 1.75 (the default 2 read heavy beside 13–14 px text).
- **Motion:** 120 ms for hover and press (`--motion-fast`), 200 ms for disclosure and overlays (`--motion-duration`), easing cubic-bezier(0.2, 0, 0, 1). `prefers-reduced-motion` sets both to 0.
- **Targets:** at < 640 px every control is at least 44 × 44 px (`max-sm:min-h-11`). Desktop controls are 32–40 px.

## Shell

- **Sidebar (≥ 1024 px):** 232 px; a quiet light panel with a hairline edge in light theme, deep water in dark. The brand mark and name (plus the instance name when it differs), a search field with its shortcut, then two groups: **You** (Home, Library, Try, Apps & keys, Models, Workflows, Usage) and, for owners, **Gateway** (Status, Engines, Providers, People, Activity, Settings). Members see Status in You. Order is by how often a household member needs each place. Every destination has its own icon (ban 19). The footer holds the health line (a dot and the shared verdict count, Status model rule 3; its popover separates gateway reachability from "Your AI"), the account (person mark and name) and the theme toggle. Nav items are 32 px, 13 px text, medium weight only for the current page; group labels in subtle ink. D1.5: the owner's Gateway group is quieter than the household's places, under a hairline with 28 px rows, 12 px text and subtle ink (the current page keeps full ink), because Engines and Providers weighed as much as Home and Library. The rail and the phone sheet keep 44 px targets.
- **Rail (640–1023 px):** 72 px icon rail with tooltips; a menu button opens the full list as a sheet.
- **Phone (< 640 px):** a 56 px header in the chrome colour with the mark, the health dot (opens the same popover) and the person mark (account menu). A bottom bar with four labelled tabs: Home, Library, Apps, More (Midjourney 3a.4: four or fewer). More opens every destination, search and the theme toggle.
- **Owner alert:** when an engine in use is down or a provider fails its check, a one-line warning band ("Image engine is down. Check its connection on Status.") sits above the page on every route except Status, with "View status"; the health popover repeats it for owners.
- **One search per screen (D1.2):** the sidebar's entry is "Go to…", a navigation row with its shortcut, not a search field; it opens the command palette. Pages search their own content (Library: "Search your creations"), so a screen never shows two search boxes.
- **Command palette (⌘K / Ctrl K, "Go to"):** pages, your apps, models, people, and three actions: create a key, copy the gateway address, check gateway reachability. Focus returns to its opener; when the opener was the navigation sheet's "Go to…" button, focus returns to the control that opened the sheet.

## Components

| Component | File | Job |
|---|---|---|
| Status model | `lib/capability-state.ts`, `lib/use-capability-views.ts` | The state words, the rules above, the verdict; `useCapabilityViews(owner)` gathers the evidence |
| PageHeader | `components/product/page-header.tsx` | The one page header: 24 px title, optional count, actions on the right, the StatusLine under it. `tight` (Home) keeps a one-line status close under the title, as its subtitle |
| StatusLine | `components/product/status-ui.tsx` | The verdict label with its icon and "Checked …" age; `role=status`, `aria-atomic`. `compact` draws it as one 14 px line (Home) |
| StateWord, StateIcon | same | One icon, colour and word per state; Ready stays quiet |
| Callout | same | The one callout for every problem, notice and error: severity icon, a title naming the subject, meta, facts, body, actions. On wide screens the header action sits on the title row; on a phone it follows the body, so the instruction comes before its button, and everything under the title uses the full width. `evidence` facts follow `facts` on wide screens and fold behind "Show evidence" on a phone. D1.4 phone order: title, facts (scope, history), the instruction, its button, then evidence |
| CapabilityList | same | The one way to draw capabilities, in the task-row anatomy: tinted 32 px icon tile, name, state word, reason or age; one or two columns |
| CapabilityChips | same | While problem cards are open, the capabilities as one column of 44 px rows in a card (icon, name, state word when not Ready), in Status's side column at 1280 px and wider and under the cards elsewhere (D1.4 replaced the wrapping pills) |
| AudioFace, MediaBadge, `mediaBadgeButton`, Waveform, `audioKind` | `components/product/audio-cover.tsx` | D1.5 replaced the per-person audio covers. AudioFace: one neutral face for every clip (sunken surface, real waveform or the glyph, the badge); MediaBadge: the duration badge on video and audio tiles (a play glyph and "0:04" on a dark scrim; the glyph alone without a duration); `mediaBadgeButton`: the same badge as the in-place play button, with an invisible 40 px target. The waveform doubles as the position in the viewer |
| `useAudioPeaks` | `lib/audio-peaks.ts` | Peaks decoded from the file with Web Audio, one file at a time, cached; null (no waveform drawn) when decoding is unavailable |
| CapNotice | `components/product/cap-notice.tsx` | A spending-limit refusal for owner or member (rule 6) |
| SectionHeading | `components/product/readiness.tsx` | Every titled region: 16 px heading, optional count and one action |
| `capabilityMeta` | same | Capability name, icon and hue |
| UptimeBar, `hourSlots`, `hoursUp`, `failingSince`, `healthHistory`, `healthStory` | `components/product/uptime-bar.tsx` | Rule 4: 24 hourly segments, down hours taller as well as red, a legend of the colours shown, hour-by-hour list (`hourByHour={false}` in the engine table). `size="lg"` on problem cards: 24 px cells, the legend above the strip, clock times every six hours and "Now" under it. `healthStory` gives a card its two plain lines (rule 4) |
| AppMark | `components/product/app-mark.tsx` | Real marks for known apps (Simple Icons, CC0) and a monogram otherwise |
| PersonMark | `components/product/person-mark.tsx` | A person's initial in their stable hue |
| BrandMark | `components/product/brand-mark.tsx` | The fish, in the chrome accent |
| Segmented, Tabs | `components/product/segmented.tsx`, `components/ui/tabs.tsx` | The only segment pattern for filtering, and the tab pattern. D1.5: no sunken track; quiet chips, the chosen one in the accent tint and semibold (the sidebar's "you are here"). Options may carry an icon (hidden on phones, where four segments share the width). Tabs' `line` variant (People) keeps its underline |
| Disclosure | `app/globals.css` (`summary`) | Every `<summary>` draws one chevron in the text colour that turns 90° when open; a summary with `list-none` draws its own |
| CopyField | `components/product/copy-field.tsx` | One boxed value with the copy button inside its edge (Cloudflare 6a.2) |
| Button | `components/ui/button.tsx` | default (primary), outline, secondary, ghost, link, destructive (D1.5 removed `cover`) |
| Input, Select, Popover, Dialog, Sheet, DropdownMenu, Tooltip, Skeleton | `components/ui/*` | shadcn primitives on the tokens |
| EmptyState | `components/product/empty-state.tsx` | Left-aligned dashed panel with an icon, a label and the one action that fills it (Geist 12c.2); never a centred illustration |
| ErrorState | `components/product/error-state.tsx` | A Callout with heading, an optional sentence, retry, and the HTTP code under "Technical details" |
| Favourites and task rows | `app/home/page.tsx` | D1.4: the person's two most-used tasks (from their own successful requests; pictures and chat for someone new) as two 128 px tiles with "Last used 22m ago"; every other task a quiet 44 px row with a small icon in its colour, no chip; a state only when not Ready; a link into Try with the right mode, or a plain row with the reason when unusable. D1.5: the rows sit in one card under the tiles, with hairlines between rows (one column on a phone, two on a wide card), so the section reads as one family of cards |
| Problem card | `app/status/status-client.tsx` | A Callout: the fault as title with the action beside it, the fix, then Affects, Started, Health checks (with a colour legend) and Failed requests as label-value rows. Only the first card's action is primary |
| Layer table | same | Engines and providers: name, serves, uptime bar, "16/24 h up", state word; spend meter and "USD 2.02 of 8.00 this month" on one line for providers. Rows are one line; the hour-by-hour list lives on the card and the engine sheet |
| Creation tile and viewer | `app/library/page.tsx`, `app/library/library-format.ts` | Square media tile, media only, in date groups (`groupByDate`, `dateGroupLabel`); on hover or focus a short title (`captionTitle`), maker and day (`tileDate`), at the top on audio so its play badge stays usable; video and audio carry the MediaBadge; audio as an AudioFace; viewer with stage, designed video and audio controls (audio on a plain card with a primary play button), facts and actions, and Share (the file itself) first where the browser has a share sheet |

Removed in D1.1: Verdict, StateLabel, `availabilityState`, EvidenceTime, `evidenceStamp` and Notice. They were a second status vocabulary; the Status model replaces them.

No new component without a director decision.

## Surfaces in this round

- **Home.** Title and a one-line verdict (with "Open Status", or for a member "See what is happening", when something is wrong), with "Connect an app" in the header on phones and tablets. Apple Home's order: "Recent", a strip of four creations whose files still exist (audio as an AudioFace with a play badge, video with a play badge), with placeholders for work still running (and "it may not finish" when its capability is failing); then "Make something": two favourite tiles and the other tasks as quiet rows in one card. The status line sits close under the title (`tight`), so no gap floats above Recent. "Connect an app" is the right-hand panel on desktop, aligned with the first list, and last on phones. A spending-limit refusal shows a CapNotice above the tasks.
- **Status, owner.** One reading column up to 896 px; while a card explains a problem, at 1280 px and wider the page is 1152 px with Capabilities (as rows) and Engines and providers in an 18rem column beside the cards. A problem card leads with "Affects" (what stops working, whether another engine covers it, and who already hit it, with their app) and "Last 24 hours" (the strip's story); the strip, its legend and failed-request counts follow as evidence. The folded engines line is two sentences ("4 other engines are ready and 1 is turned off. 1 remote provider is set up."). Title, verdict label with check age, Refresh and "Technical details". "Needs attention": full-width split problem cards for engines (grouped when several fail the same way), providers, the gateway's computer (disk faults on their own card with "Free disk space") and access plans that reached a spending limit. "Running now" lists work in progress. "Capabilities": the CapabilityList in two columns. "Engines and providers": the layer table. While cards explain a problem, capabilities fold to 44 px rows beside the cards and, below 1280 px, engines fold to one line of counts with "Show all" (principle 7); at 1280 px and wider the engine table stays open under the cards, so the lower half of the page is never empty (D1.5). On a phone the problem card is one column: each label sits above its value, and the evidence type is 14 px or larger; the header pill is hidden on Home and Status, which carry their own verdict.
- **Status, member.** The same header and verdict as Home; a CapNotice for a current refusal; "Your video request failed" for the latest failure with no later success, saying what state the capability is in now; "Your capabilities" (CapabilityList); "Model names" one tap away; "Your latest requests" (the gateway sends five) with Failed, Refused and In progress, each link named "View request receipt" and described by its capability, state and time.
- **Library.** Media only, newest first, in date groups with quiet 14 px headers: Today, Yesterday, This week (the five days before), then each month ("September", with the year when it is not this year); a group's last row may be part-filled (principle 8). Work still being made opens the Today group. Three columns on a phone. The count is what the grid shows; files no longer stored are counted beside it ("4 no longer stored") with one explanation for owners and members. One control row: search first, then kind (chips, with icons), the owner's person filter (people's marks) and "Filters" with model, date and app (named "Filters 2 active" when set). On a phone one row holds the kind chips, a Search button and an icon Filters button, so the title and controls take under a fifth of an 844 px screen; the search field opens below the row and stays open while it has text; the person picker is inside Filters. Video and audio tiles carry the duration badge; audio tiles are AudioFaces whose badge plays the clip in place. The viewer shows audio on a plain card (kind, words, a primary play button, the waveform as position). Display names for models; the raw id only in Details. Videos without a poster show their first frame (a seek to 0.1 s plus a `#t=0.1` media fragment for Safari); real video jobs have no poster today (backend ask 8). The viewer has designed play, position, time and mute controls. Running work shows calm placeholders. Owners are warned before pictures expire. Posters the gateway lists as separate images are folded into their video.

## Token drift, bans and exceptions

- Run `scripts/ui/token_drift.py --fail` (default scope: the surfaces above and their primitives). Legacy routes report with `--all` and are out of D1 scope.
- Ban-list exceptions: none recorded.
- Impeccable detector exception: `layout-transition` (height) comes from Sonner's own toast stylesheet, a third-party package; no Azizam style animates height.
- D1.4's detector exception (`text-overflow` on an audio cover's top line) is gone with the covers (D1.5).
- Console exception: signed-out pages get a 401 from `/admin/api/session` while the shell checks for a service key. It is expected and carries no user-facing error.

## How the gates run (T-4)

- `scripts/ui/token_drift.py --fail`: token drift.
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
16. **Carry "degraded" through the capability projection (P0, red team round 2).** `gateway/availability.py:22` puts "degraded" in `BUSY_STATUSES`, so `_from_evidence` (lines 344-348) projects a degraded engine as capability status "busy"; `/me/api/home` and `/me/api/status` then say `status: "busy", reason_code: "busy"`, while `/admin/api/status` keeps the raw "degraded" (`gateway/admin_api.py:612-617`). Members and owners got two stories about one engine. The UI no longer reads "busy" as a queue without an engine check; the gateway should project "degraded" (or a `reason_code` that says so) so members can be told "answering with errors".
17. **Failed creations in `/me/api/media`.** Only succeeded jobs are listed, so a failed video never appears where it would have been (red team G3). List failed and refused media jobs with their state.
18. **Engine capability labels are incomplete.** In the household fixture the voice engine serves voice, transcription, music and sound effects, but its `capability_labels` name only Music and Sound effects, so Status cannot attribute Unknown Voice and Transcription to it (the UI now shows them on their own card).
19. **The members' request window.** `build_user_activity_snapshot(limit=5)` (`gateway/home_api.py:354`) gives members their latest five requests, so a failure the owner's card attributes to them can be missing from their list. Say the window in the payload, or page it.
