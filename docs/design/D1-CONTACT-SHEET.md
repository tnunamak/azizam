# D1.2 contact sheet: Home, Status, Library

Captures: `~/.tmp/azizam-redesign/d1-8/` (made by `scripts/ui/d1-states-capture`, branch `design/full-surface`). One folder per household scenario. File names are `<scenario>_<role>_<surface>_<state>_<theme>_<width>.png`; each has a `_viewport.png` twin (1440×900 or 390×844, not full page, which is what the benchmarks show) and the page text in a `.txt`. A `manifest.json` per folder records console errors and forced states. Every state exists for owner (Sam) and member (Robin), at 390 and 1440 px, in light and dark. Loading, error, empty and expired are forced by request interception and labelled so in the manifest. The fixture's public address is `https://ai.household.example`.

Benchmark ids refer to `benchmarks/BOARD.md`. State words and rules: DESIGN.md, "Status model".

## Home

| State | What it shows, and the trait it meets |
|---|---|
| populated, member | Title, then the verdict label "Video degraded" with "Checked just now" (Linear 2a.1). "Make something": every task as one 56 px row in one panel (two columns on desktop), the right side saying "Used 39m ago" or the state (Video: Degraded). "Recent" media, audio as covers (Midjourney 3a.2). "Connect an app" in the header on phones, the right-hand panel on desktop. |
| populated, owner | The same Home with "Open Status" beside the verdict. |
| new-member | No app has made a request, so the panel opens on the app chooser with real marks (Open WebUI, LibreChat, SillyTavern, Cursor, Codex, Claude Code, Another app). On a phone it still comes after the tasks. Persian name in the person mark. |
| budget-exhausted, member | "Paid models were paused for you": the household reached its monthly limit, when it resets, ask the gateway owner. Chat reads Degraded: "Some models are paused: a spending limit was reached." |
| budget-exhausted, owner | "A spending limit refused paid requests" with the gateway's sentence, the reset and "Open Status". |
| stale-health | Voice, Transcription, Music and Sound effects read Unknown; the verdict counts them ("5 of 7 capabilities need attention"); nothing says Ready on old evidence. |
| loading / error / empty / signed-out | Skeletons in the final layout; a critical Callout with "Try again"; "Nothing is set up yet"; "Sign in to use your AI" as an info Callout with one button. |

## Status

| State | What it shows, and the trait it meets |
|---|---|
| populated, owner | Verdict "Video degraded" with check time. One card titled with the fault, "Video studio is answering with errors", with "Inspect Video studio" beside it, the fix, then Affects, Started (between 1:30 and 2:30 PM), "16 of 24 checked hours up, last 24 h" with the hourly bar and its legend, and "1 of the latest 50 requests, from Robin" with its receipt (incident.io 2c.1–2c.3, Linear 2a.3). Capabilities as one row of chips, engines and providers as one line. One 896 px reading column. |
| populated, member | The same verdict as Home; "Your video request failed" with what state Video is in now; "Your capabilities"; "Model names"; "Your recent requests" with Failed and In progress. |
| running, owner | "Running now" lists the two requests in progress, with their people. |
| budget-exhausted, owner | Two cards: Video studio, and "The Everyone plan reached its spending limit" with limits (USD 1.00 a day, USD 8.00 a month), spend not reported by the gateway, the gateway's sentence, both resets, who was refused, and "Edit the Everyone plan". |
| provider-down, owner | "Remote backup is down": the last connection check failed, Chat is still served by an engine on this computer, 7 of the latest 50 requests failed from Robin, Zoë and Sam. |
| stale-health, owner | "Home Assistant voice is not answering checks", last check 7 h ago; the Voice, Transcription, Music and Sound effects chips read Unknown (each chip's detail: "Last checked 7 h ago; no answer since"). Member: the same four read Unknown, "No answer to the latest check." |
| error | Owner: "Couldn't check status" and a card naming the gateway. Member: "The gateway returned an error. Try again." with the HTTP code under Technical details; a fetch that never reached the gateway names no layer. |
| loading / empty | "Checking" with skeletons; "No engines yet" with "Add an engine". |

## Library

| State | What it shows, and the trait it meets |
|---|---|
| populated | Title with count, search on the right; kind (segmented), person and "Filters" at one height. "This week", "Last week", "Week of …" groups. Square tiles with the maker's person mark (Midjourney 3a.1–3a.2, Suno 3b.1). The video's poster is folded into the video, so the count is 24. |
| library-audio | Audio tiles are covers: the maker's tint, "Voice", the clip's words, its real waveform and length, a small play control. The viewer shows the same cover at full size with the waveform as position. |
| library-video / library-detail | The viewer: media on the stage, designed play, position, times and mute; title, maker and age; Download, Copy prompt, Receipt; facts with display names (LTX Video), size, length, made in, charge. |
| expired | Gone files fold into one line; owners are warned before pictures expire. |
| loading / error / empty / filtered empty | Square skeletons; a critical Callout with "Try again"; "No creations yet" with "Make an image"; "Clear filters to see more." |
| running | Calm placeholders for work in progress. |

## Craft diff (D1.2, side by side with the benchmarks)

Each pair was read at the same size: Azizam viewport shots (1440 × 900, 390 × 844, DPR 1) next to the benchmark image. "Before" is D1.1 (`d1-7`), "after" is `d1-8`. Sizes are measured in the screenshots in CSS px.

### Home — Apple Home (iPhone), Stripe iOS
| Measured in the benchmark | D1.1 | D1.2 change |
|---|---|---|
| Apple Home: one tile grammar, every tile the same height (56 px tiles, 40 px chips); state as a word only where it adds something | Three tall cards with 2-line hints, then 52 px rows: two heights, uneven (judges: "generic stack of icon-title-chevron cards with uneven heights") | One 56 px row grammar for every task in one panel (hairline-separated, two columns on desktop); 32 px tinted icon tile, 14 px medium title, the reason on the right |
| Apple Home: no helper text under items | A hint sentence under each primary task | Hints removed; the right side carries the one true reason: the state when not Ready, else "Used 39m ago" from the person's own receipts |
| Apple Home trait 5: "+" add in the top bar, one tap from the landing | "Connect an app" was the third block on a phone, below the fold | "+ Connect an app" in the header on phones and tablets, jumping to the panel; on desktop the panel aligns with the first list (was 15 px higher) |
| Section titles 20 px semibold, 16 px side margins | 16 px section heads, 20 px margins | Kept: matches Stripe's 17 px heads and our margins; panel heading raised from 14 to 16 px to match the section scale |

### Status — Linear status, incident.io incident
| Measured in the benchmark | D1.1 | D1.2 change |
|---|---|---|
| incident.io: card title 16 px medium, the state as its own small line, no restated headline | Verdict "Video degraded" then card title "Video studio is degraded" (judges: stated twice) | Card title is the fault in plain words ("Video studio is answering with errors"); the verdict alone carries the state word |
| incident.io: actions at the top of the page, no empty columns in cards | Split card: action alone in a left column, 300 px of empty card under it | Action on the title row, right-aligned; fix sentence, then facts as label–value rows; card height 300 → 300 px but no dead column |
| Linear / incident.io: one reading column of 670 px | Cards stretched to 1100 px | Owner Status is one column up to 896 px |
| incident.io phone: label and value on one line | Labels stacked over values on phones (judges: failed requests fell below the fold) | Label column 96 px beside the value at 390 px; failed requests now at y ≈ 680 on a 844 px screen |
| incident.io affected bar: times at both ends | "24 h ago / Now" but no legend; the last amber cell unexplained | Legend under the bar: Up, Degraded, Down, No check |
| — | "Failing since: The hour from 1:30 PM" | "Started: Between 1:30 PM and 2:30 PM" (hourly checks place it within an hour) |
| Linear: the lower half is content, not summary stubs | Two near-empty "N ready… Show all" rows | Capabilities fold to one row of chips (icon, name, state word when not Ready); engines keep one summary row |

### Library — Midjourney, Apple Photos, Apple Music
| Measured in the benchmark | D1.1 | D1.2 change |
|---|---|---|
| Apple Music: a track is told apart by its title (13–15 px) and its length; artwork identifies the album | Audio tiles: grey squares, a 44 px play button in the centre, two truncated words (judges: "cannot be told apart") | AudioCover: maker's tint (the person hue at 16%), "Voice" with its icon, up to four lines of the clip's words at 14–16 px, the real waveform and the length; 36 px play button at the bottom left |
| Apple Music: the playing view shows the artwork and a scrubber | Viewer: a decorative icon over a white card | Viewer: the same cover at full size; the waveform (64 bars, decoded from the file) shows the position |
| Midjourney: one search for the page's content | Sidebar "Search" box and page "Search your creations" on one screen | Sidebar entry is "Go to…" (a navigation row with its shortcut); the page search is the only search box |
| Midjourney: a filter change updates the grid at once | Kind filter pressed while the old grid stayed until the request returned | The kind applies to the grid immediately |
| Video tiles show a frame (Apple Photos) | Poster shown, but below the fold in the judged shot | Unchanged: posters load (`d1-8` default Library, "Week of Oct 12") |
| Header: title with count, controls on one 40 px line | Title 24 px, 20 px gap to the filters, 24 px to the first group | 16 px and 32 px, the PageHeader rhythm used on Home and Status |

### Across surfaces (coherence)
- One list-row anatomy: 32 px tinted icon tile, name, right-aligned state or reason, 56 px rows. Used by Home tasks and the capability lists on Status.
- One header rhythm: 24 px title, 16 px to the verdict or controls, 32 px to the first section, 16 px section heads with 12 px below.
- Icons: lucide stroke 1.75 everywhere (was 2).
- Empty, error and loading states unchanged from D1.1: left-aligned EmptyState, Callout errors, skeletons in the final layout.

## Gate results for this set

Run on the final D1.1 build (default household, port 18447):

- axe WCAG 2 AA: 0 violations over 32 page checks (owner and member; Home, Status, Library, viewer; light and dark; 1440 and 390). Horizontal overflow, clipped text, targets under 44 px at 390, tab stops without visible focus and console errors: 0.
- Impeccable detector: only Sonner's toast height transition (the recorded exception) on all 16 snapshots at 1280 and six at 390.
- Token drift: 0.
- lint.py on a copy of the production data (every engine blocked): Home, Library and member Status pass. Owner Status reads 69 words against a budget of 60, because a twelve-engine outage keeps its evidence (affects, engines, failed requests). Workflows, Activity and Usage are over budget and out of scope.
- Unit tests: every vitest config passes. Playwright: the 17 e2e tests D1.1 broke pass again; 37 tests in the same five files were already failing at e196d79 (D1) and still fail.
