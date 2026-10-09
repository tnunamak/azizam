# D1.2 contact sheet: Home, Status, Library

Captures: `~/.tmp/azizam-redesign/d1-9/` (made by `scripts/ui/d1-states-capture`, branch `design/d12-continue`; the earlier D1.2 set is `d1-8`). One folder per household scenario. File names are `<scenario>_<role>_<surface>_<state>_<theme>_<width>.png`; each has a `_viewport.png` twin (1440×900 or 390×844, not full page, which is what the benchmarks show) and the page text in a `.txt`. A `manifest.json` per folder records console errors and forced states. Every state exists for owner (Sam) and member (Robin), at 390 and 1440 px, in light and dark. Loading, error, empty and expired are forced by request interception and labelled so in the manifest. The fixture's public address is `https://ai.household.example`.

Benchmark ids refer to `benchmarks/BOARD.md`. State words and rules: DESIGN.md, "Status model".

## Home

| State | What it shows, and the trait it meets |
|---|---|
| populated, member | Title, then the verdict label "Video degraded" with "Checked just now" (Linear 2a.1). "Make something": every task as one 56 px row in one panel (two columns on desktop). Each row leads with a 40 px square of what this person last made with it (Robin's garden picture, the first frame of her video, a voice cover in her amber) or the task's tinted icon when there is nothing yet; the right side says "Used 39m ago" or the state (Video: Degraded). No chevrons. "Recent" media, audio as covers (Midjourney 3a.2). "Connect an app" in the header on phones, the right-hand panel on desktop. |
| populated, owner | The same Home with "Open Status" beside the verdict. |
| new-member | No app has made a request, so the panel opens on the app chooser with real marks (Open WebUI, LibreChat, SillyTavern, Cursor, Codex, Claude Code, Another app). On a phone it still comes after the tasks. Persian name in the person mark. |
| budget-exhausted, member | "Paid models were paused for you": the household reached its monthly limit, when it resets, and "Ask Sam if you need them sooner" (the owner's name now comes from the gateway; "the gateway owner" when it has none). Chat reads Degraded: "Some models are paused: a spending limit was reached." |
| budget-exhausted, owner | "A spending limit refused paid requests" with the gateway's sentence, the reset and "Open Status". |
| stale-health | Voice, Transcription, Music and Sound effects read Unknown; the verdict counts them ("5 of 7 capabilities need attention"); nothing says Ready on old evidence. |
| loading / error / empty / signed-out | The real header and seven 56 px row skeletons in the task panel (D1.2 replaced D1's tile-grid skeleton); a critical Callout with "Try again"; "Nothing is set up yet"; "Sign in to use your AI" as an info Callout with one button. |

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
| populated | Title with count, search on the right; kind (segmented), person and "Filters" at one height (on a phone the kinds fill the width and person, Filters and Clear filters share the line below). "This week", "Last week", "Week of …" groups. Square tiles with the maker's person mark (Midjourney 3a.1–3a.2, Suno 3b.1). The video's poster is folded into the video, so the count is 24. |
| library-audio | Audio tiles are covers on deep water washed with the maker's hue (olive for Robin's amber, plum for Aziz's rose, teal for Sam): "Voice", the clip's words, its real waveform in the maker's hue, its length, and a play disc in that hue with ink on it. The viewer shows the same cover at full size with the waveform as position. |
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

### D1.2 continued (`d1-8` to `d1-9`)

Read side by side at DPR 1: Azizam viewport shots against `benchmarks/home/apple-home-iphone.webp`, `phone/stripe-ios-01.webp`, `status/incidentio-incident-detail-phone.webp`, `library/midjourney-explore-phone.webp`, `library/apple-music-album-phone.webp` and the D1.1 judged pairs in `~/.tmp/azizam-judging/set-3`.

**Library (media tiles).**
| Measured in the benchmark | `d1-8` | `d1-9` change |
|---|---|---|
| Apple Music phone: the track lives on dark artwork; the play control is a light pill with dark ink | Covers were a 16% pastel tint behind dark text: in the photo grid they read as notes, far lighter than the pictures (mean luminance 0.86 against 0.42 for the photos, measured in the 390 px shots) | Covers sit on deep water (the dark theme's card, scoped with `dark`) washed 24% with the maker's hue; waveform, kind icon and a 36 px play disc (44 px on phones) in that hue with ink on it. Mean luminance 0.34, close to the photos' 0.42. Words 7.7:1, secondary 5.6:1 or better on all seven hues |
| Apple Photos / Midjourney: a video tile shows a frame | The video tile showed its poster in the fixture; real videos have none (see "Video posters" below) | Posterless tiles also ask Safari for the first frame (`#t=0.1`); Chromium was already painting it (probe: readyState 4, non-blank pixels) |
| Midjourney phone: one line of filters, aligned to the grid's edges | At 390 the kinds sat at their natural width (ragged right edge), the person picker kept a 160 px minimum, and with a filter active "Clear filters" wrapped to a third row (first group label at y = 379) | Kinds fill the width; person, Filters and Clear filters share one line, both rows aligned to the grid's edges. With a filter active the first group label is at y = 327 (52 px higher); without one it is unchanged |
| Apple Music: the scrubber shows the whole track before it plays | The viewer's unplayed bars at 25% opacity on the cover were close to invisible | Unplayed bars at 40% |

**Home (phone).**
| Measured in the benchmark | `d1-8` | `d1-9` change |
|---|---|---|
| Apple Home: the first section shows the household's live content (four camera pictures), not icons; judges scored it 9 for specificity against our 6 | Seven identical rows of tinted icon, title and chevron (judges: "generic stack of icon-title-chevron cards") | Each row leads with a 40 px square of the person's newest creation for that task (picture, video frame, audio cover in their hue), so the list shows this household's work; tasks with nothing made keep the tinted icon. Rows stay 56 px; the leading square has an 8 px inset on every side |
| Apple Home / Stripe: no chevrons on tiles; the whole tile is the control | A chevron on every row | Chevrons removed; each row is still one link named by its task, with a hover fill |
| Apple Home trait 5 and Stripe trait 2: add/create one tap from the landing | Already met in `d1-8` ("+ Connect an app" in the header) | Unchanged |
| Skeletons in the final layout (Geist, our own rule) | Home's skeleton was D1's eight-tile grid | The real header, a verdict placeholder and seven row skeletons in the task panel |
| Apple Home names the household ("My Home"); VOICE: ask a person, not a role | Members were told to "ask the gateway owner" | With `gateway.owner_name` from main, a member's notices say "Ask Sam"; "the gateway owner" only when the gateway has no name. The household shows on the first screen as its own work in its own hues (the leading squares) rather than as a title or a tile grid |

**Status (phone).**
| Measured in the benchmark | `d1-8` | `d1-9` change |
|---|---|---|
| incident.io phone: the card's body uses the card's full width; only the title has an icon beside it | Everything under the title sat in a column indented 32 px, so values wrapped early ("16 of 24 checked hours up," then "last 24 h") | On a phone, only the title sits beside the severity icon; action, fix, facts and actions use the full width (applies to every Callout, so error and notice cards match) |
| incident.io: every word in the card is about this incident | The legend named "No check" under a bar with no empty hour, and wrapped to its own line | The legend names only the colours the bar shows |
| — | "…latest 1h ago ·" then "Open receipt" on the next line: a dangling dot | On a phone the link takes its own line and the dot is not drawn |
| incident.io phone: the next card starts within the first screen | "Capabilities" began below 844 px | "Capabilities" starts at y = 748 |

**Coherence.** One leading-square size per list kind (40 px for task rows that can hold a picture, 32 px for capability rows that only hold an icon), one cover surface for audio everywhere it appears (Library tile, viewer, Home's Recent and task rows), one Callout anatomy on phones, and skeletons that match the final layout on all three surfaces.

### Video posters: root cause

The round-2 judges wrote "video tiles have no poster frames". Evidence, in order:

1. **The judged shot shows no video.** In `set-3/pair-009/B.png` (and pairs 002, 005, 011) the grey tiles with a play button are the five audio clips. The person marks under them (R, A, S, S, R) match the order of the five audio clips in `d1-7/default/default_owner_library_populated_light_1440.txt` ("The tomatoes are ready…" R, "Our solar house…" A, "On Saturday…" S, "Hi June…" S, "For the apple tart…" R). The household's one video is in "Week of Oct 12", below the 900 px fold.
2. **The UI shows the fixture's poster.** `d1-8/default/default_owner_library_library-video-grid_light_1440_viewport.png` shows the video's poster with "0:03"; the API lists `poster_asset_id` and `web/src/app/library/page.tsx` (the `Preview` poster query) loads it.
3. **Real videos have no poster.** On the live gateway, 0 of 50 video assets in `data/media_jobs.db` carry `poster_asset_id`. Only `scripts/ui/household_seed.py:1245` writes one; `gateway/video_media_job_http.py:100-120` (`completed_asset_from_stored_video`) builds the video asset without one, and `gateway/media_ingestion.py:316` only copies a poster an engine supplies. So every live video tile takes the posterless path (`page.tsx`, `<video preload="metadata">` with a seek to 0.1 s).
4. **That path paints in Chromium**, with the fixture's poster stripped by request interception: `readyState` 4, `videoWidth` 768, non-blank canvas pixels (`~/.tmp/d12c/poster-chromium-stripped.png`). Safari is known to ignore the seek until playback; `9b4eba5` adds a `#t=0.1` media fragment. WebKit could not run on this host (missing system libraries), so the Safari behaviour is unverified.

Conclusion: the judges' "no poster" was audio tiles that looked like un-postered video (a grey square with a centred play button is the video-player convention); the D1.2 covers remove that. No backend change was needed for the UI. Backend asks 8 (record a poster when a video job completes) and 14 (stop re-stripping whole videos on every Range request) are in DESIGN.md.

### Across surfaces (coherence)
- One list-row anatomy: 32 px tinted icon tile, name, right-aligned state or reason, 56 px rows. Used by Home tasks and the capability lists on Status.
- One header rhythm: 24 px title, 16 px to the verdict or controls, 32 px to the first section, 16 px section heads with 12 px below.
- Icons: lucide stroke 1.75 everywhere (was 2).
- Empty, error and loading states unchanged from D1.1: left-aligned EmptyState, Callout errors, skeletons in the final layout.

## Gate results for this set

Run on `design/d12-continue` after merging main (42c6a32), HEAD 1813605. Fixture servers on ports 18620 to 18720.

- axe WCAG 2 AA: 0 violations over 32 page checks (owner and member; Home, Status, Library, viewer; light and dark; 1440 and 390). Horizontal overflow, clipped text, targets under 44 px at 390, tab stops without visible focus and console errors: 0 (`~/.tmp/azizam-redesign/d1-9-gates`).
- Impeccable detector: only Sonner's toast height transition (the recorded exception) on all 16 snapshots (`d1-9-snap`).
- Token drift: 0.
- lint.py on a copy of the production data (every engine blocked): Home, Library and member Status pass. Owner Status reads 76 words against a budget of 60 (`d1-8`: 83). The excess is the nine capability chips, each with its state word, in an outage where all nine need attention; the "Affects" row of the card names seven of them again. Folding them is a judging question, because each chip carries a different state. Workflows, Activity and Usage are over budget and out of scope.
- Unit tests: vitest 524 in the main config plus all 11 folder configs pass.
- Playwright, main config: 597 passed, 15 failed. The 15 are the same tests (spec and title) that fail at 542c95a and on main in the 2026-10-09 audit (stale specs: Library seeded, Library filters, engines and services error copy, services route gate, Usage remote spend). Live-data config: 4 of 4 pass (the stale scoping spec is fixed).
- Hermetic Python at 1813605: 5789 passed, 0 failed.
- One regression was found and fixed during the round: the deep-water cover isolated its stacking context and hid a tile's play control under its open button (`3a65385`); `e2e/library.spec.ts` "audio plays independently" caught it.
