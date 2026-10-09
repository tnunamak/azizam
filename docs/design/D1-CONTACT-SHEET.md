# D1 contact sheet: Home, Status, Library

Captures: `~/.tmp/azizam-redesign/d1-14/` for D1.6, round 7 (made by `scripts/ui/d1-states-capture`, branch `design/d12-continue`; earlier sets: `d1-13` for D1.5b, `d1-12` for D1.5 before the coordinator's decisions, `d1-11` for D1.4, `d1-10` for D1.3, `d1-8` and `d1-9` for D1.2). `try-image` and `try-speech` are populated captures of the Try page; `d1-14/task-flows/` holds six household jobs step by step (`web/e2e/d1-task-flows.mjs`); in D1.5b the picture and speech runs finish in the fixture. One folder per household scenario. File names are `<scenario>_<role>_<surface>_<state>_<theme>_<width>.png`; each has a `_viewport.png` twin (1440×900 or 390×844, not full page, which is what the benchmarks show) and the page text in a `.txt`. A `manifest.json` per folder records console errors and forced states. Every state exists for owner (Sam) and member (Robin), at 390 and 1440 px, in light and dark. Loading, error, empty and expired are forced by request interception and labelled so in the manifest. The fixture's public address is `https://ai.household.example`.

Benchmark ids refer to `benchmarks/BOARD.md`. State words and rules: DESIGN.md, "Status model".

## Home

| State | What it shows, and the trait it meets |
|---|---|
| populated, member | Title, then close under it one 14 px line: "Video degraded", "Checked just now" and "See what is happening" (to Status). Apple Home's order: "Recent", four of Robin's creations (video and audio with a play badge); then "Make something": her two most-used tasks as 128 px tiles ("Chat, Last used 22m ago", "Make a picture, Last used 39m ago"), then the other tasks as 44 px rows in one card with hairlines; only Video carries a state ("Degraded"). "Connect an app" in the header on phones, the right-hand panel on desktop. |
| populated, owner | The same Home with "Open Status" beside the verdict. |
| running | "Making your video" with "Video degraded: it may not finish" (wide screens). |
| new-member | No app has made a request, so the panel opens on the app chooser with real marks. Favourites fall back to pictures and chat. |
| budget-exhausted, member | "Paid models are paused": the gateway reported the monthly limit for paid AI services was reached, when it resets, and "Ask Sam if you need paid models sooner". |
| budget-exhausted, owner | "A spending limit refused paid requests" with the gateway's sentence, the reset and "Open Status". |
| stale-health | The verdict counts what it cannot vouch for: "5 of 7 capabilities need attention", "Checked just now · 4 unknown". |
| loading / error / empty / signed-out | Skeletons in the layout; "Couldn't load Home" with "Try again"; "Nothing is turned on yet" with "Ask Sam to turn something on." (member) or "Add an engine" (owner), and no Recent; "Sign in to use your AI". |

## Status

| State | What it shows, and the trait it meets |
|---|---|
| populated, owner | Verdict "Video degraded". One 896 px column at every width: the card, Capabilities as two columns of rows in one card, then "Engines and providers" folded to one line ("Other engines: 4 ready, 1 turned off." with "Show all"; opened, each engine's name sits over what it serves, with a wide strip, "24/24 h up" and its state). The card, in one order at every width (D1.6): "Video requests can fail right now"; "What's wrong: Video studio is answering with errors."; "Affects: Video: no other engine serves it." with "A request from Robin's personal app failed 1h ago."; the fix "Check the engine's log and restart it." in 16 px full weight with "Inspect Video studio" under it; then "Last 24 hours": "Degraded since about 1:30 PM", "Down for 4 hours and degraded for 2 in the last 24 hours, in 3 separate stretches. Fully down at times (red); partly working now (amber).", the legend, the 32 px strip and clock times; then "Show details" (failed requests). On a phone each label sits above its value; no header pill. |
| populated, member | The same verdict as Home; "Your video request failed"; "Your capabilities"; "Model names"; "Your latest requests" (the gateway sends five); the next step follows the failure ("Ask Sam if it does not recover.", not "Check your connection"). |
| new-member / budget-exhausted, member | Video reads Degraded, "Answering with errors. Requests may fail." Since main 00f10bc the gateway reports "degraded" itself (backend ask 16, done), so member and owner tell one story. |
| running, owner | "Running now" lists the two requests in progress, with their people. |
| budget-exhausted, owner | Two cards: Video studio, and "A spending limit on the Everyone plan refused paid requests": Affects, Spent ("Unavailable" with each limit, or the plan's spend per period when the gateway records it), "Gateway said" quoting the refusal, Resets, Refused. |
| provider-down, owner | "Remote backup is down": the last connection check failed, Chat is still served by an engine on this computer, 7 of the latest 50 requests failed from Robin, Zoë and Sam. |
| stale-health, owner | "Checked just now · 4 unknown". "Voice and sound has not answered a check in 7 h" (Affects: Music, Sound effects), and "Voice and Transcription are not confirmed" for the two the gateway does not attribute to an engine, with "Open Engines". |
| error | Owner: "Couldn't check status", a card naming the gateway, and the badge reads "Status unknown". Member: "The gateway returned an error. Try again." |
| loading / empty | "Checking" with no check time; "No engines yet" with "Add an engine". |

## Library

| State | What it shows, and the trait it meets |
|---|---|
| populated | Title with the count the grid shows; one control row: search, kind (chips with icons, the chosen one tinted), person (the household's marks for "Everyone") and "Filters". One continuous grid; the day rides on the first tile of each day ("Today", "Yesterday", "Monday", "Oct 21"). Media only, no captions (Apple Photos, Midjourney 3a.1–3a.2); video and audio carry their type glyph and length bottom left and a play disc in the middle; with Everyone, each tile carries its maker's mark; hover or focus shows a two-line title of whole words, the maker and the day. Phone: one row of kind chips, a Search button and an icon Filters button, three columns. |
| library-audio | Every clip has the same calm face: the sunken surface and its waveform under the play disc (which plays it in place on wider screens; a phone opens it), the sound glyph and "0:04" bottom left. No per-person colour, no words on the tile. The viewer shows the clip on a plain card: "Voice", a primary play button and the waveform as position; its words are in the details, once. |
| library-video / library-detail | The viewer: media on the stage, designed play, position, times and mute; Share first where the browser has a share sheet, then Download, Copy prompt, Receipt; facts with display names. |
| expired | "20" in the title and "4 no longer stored" beside it, which opens the list; "Their prompts and receipts remain." for owners and members. |
| loading / error / empty / filtered empty | Square skeletons; a critical Callout with "Try again"; "No creations yet" with "Make an image"; "Clear filters to see more." |
| running | Calm placeholders for work in progress. |

## Craft diff (D1.6, round 6 judges and the benchmarks)

Round 6 (set-7): win or tie 82 / 71 / 82 / 82%, Status 100% for every judge, Library 71 / 43 / 57 / 57%, craft 6.5 to 7.0 against about 7.9, coherence failed for 3 of 4. "Before" is `d1-13`, "after" is `d1-14`. Each row was checked side by side with the benchmark named in its heading.

### Library: Apple Photos (iOS), Midjourney explore
| Measured in the benchmark, or judged | `d1-13` | `d1-14` change |
|---|---|---|
| Apple Photos' All Photos: one continuous grid, days labelled without breaking rows. All judges: Today and Yesterday rows mostly blank | Date groups; on a 1440 screen Today held 2 tiles and Yesterday 1 in a 6-column grid | One continuous grid; the day sits on the first tile of each day as a small chip ("Today", "Yesterday", "Monday", "Oct 21"). A 390 phone shows 15 tiles in its first screen (d1-13: about 9) |
| p1, p3, x4: no type, maker or play cue; video and audio look like stills | A duration badge bottom right on video and audio; no maker | Type glyph and length bottom left, a 40 px play disc in the middle of video and audio; with Everyone shown, the maker's initial in their colour top right of every tile |
| p2, p3: finding one item means opening each; items made by apps read "Image" | "Image" when there is no prompt | Named by type and time: "Picture, Oct 20 at 7:45 PM" (apps' requests never store prompts, a privacy contract) |

### Status: Linear status, incident.io incident
| Measured in the benchmark, or judged | `d1-13` | `d1-14` change |
|---|---|---|
| incident.io: the incident's title, then what to do, then the timeline. p1: headline and card title repeat; the what-to-do line is small grey text under the chart | Verdict "Video degraded", card "Video studio is answering with errors", the fix in 13 px under the title, the chart in the middle of the facts | Verdict names the state; the card names the consequence ("Video requests can fail right now"); "What's wrong" and "Affects" first; then the fix in 16 px full-weight text with its primary button right under it; then "Last 24 hours"; then "Show details". The same order at every width |
| p2: red Down bars read more severe than "degraded" | No link between red hours and the verdict | "Fully down at times (red); partly working now (amber)." beside the day's story |
| p3, x4: the timeline is cramped and tiny | 24 px cells, 12 px clock labels | 32 px down and 24 px up cells, 14 px clock labels, the strip under the fix instead of in the middle of the facts |

### Shell, Home and type: Linear, Apple Home
| Judged | `d1-13` | `d1-14` change |
|---|---|---|
| p2: the sidebar uses two nav type sizes | Gateway rows 28 px, 12 px, subtle ink | Every nav row 32 px and 14 px in one ink; the owner's group is set apart by space and its label only |
| p1, p2, p3, x4: two big cards then a list, for no clear reason | Unlabelled tiles, then a card of rows | "Your usual" over the two tiles ("Start here" for someone new), "Everything else" over the rows |
| p2: "Connect an app" competes with the page title on a phone | Outline button beside the title | A quiet link ("+ Connect an app") |
| p3, x4: secondary text small and low-contrast | 13 px; #4F6268 (5.9:1 on the page); subtle #5E7076 (4.8:1) | 14 px (text-sm) and 15 px body; #3D4F55 (7.9:1); subtle #4A5C62 (6.4:1); nav ink 7.6:1 on the chrome. Dark: 9.6:1, 7.7:1, 10.3:1 |

## D1.5b: the coordinator's decisions

| Item | Decision | Done |
|---|---|---|
| Owner Status | Fold healthy engines during an incident; fix the empty space with layout | One 896 px column at every width; engines fold to one line whenever a card is open, and open with nothing to attend to. Owner Status reads 86 words on the production copy again (D1.5: 212) |
| Phone tiles with date headers | Accepted | |
| Phone audio opens on tap | Accepted | |
| Task flows | Make the fixture's runs finish; say what failed; Run primary; prompt once | Both runs finish (fixture only: the image engine's id is `comfyui`, the backend the gateway's image route always uses; the fake engine answers ComfyUI and speech; the fixture guard allows the speech route's two ffmpeg calls by exact shape). A failed run reads "Image request failed". Run is the primary button. The audio viewer shows the words once, in the details |
| Merge main 00f10bc | Re-check colours and copy against the new `degraded` member state | Merged cleanly. The state model had no branch for the gateway's "degraded" (marked available), so it would have read Ready; it reads Degraded now, and a bare "busy" is Busy again. Status pills already use the warning colour, which is the D1.5 amber |

## Craft diff (D1.5, round 5 judges and the benchmarks)

Round 5 (set-6): win or tie 82 / 88 / 88 / 88% (flat), Library 71% for every judge, craft 7.2 / 6.6 / 7.5 / 7.0 against about 8.1, coherence failed for 3 of 4 judges. The brief's diagnosis: the gap is in the system, not in single items. "Before" is `d1-11`, "after" is `d1-12`. Each row was checked side by side with the benchmark image named in its heading.

### Library: Apple Photos (iOS), Midjourney explore, Apple Music
| Measured in the benchmark, or judged | `d1-11` | `d1-12` change |
|---|---|---|
| Apple Photos: date groups ("Jul 13, 2025") over the grid; a part-filled row ends a group. q2, q3, x3: no date, owner or type cue per tile | One grid, no dates; media only | Date groups with 14 px muted headers: Today, Yesterday, This week, then the month; part-filled last rows (`groupByDate`) |
| Apple Photos: a duration badge on video; no captions. q3, x3: cannot tell a video or audio clip from a picture | Video: a play chip; audio: a different cover | One badge for both kinds: a play glyph and "0:04" on a dark scrim, bottom right |
| q1, q2, q3: multi-coloured voice cards are the loudest thing on the page and break the rhythm; "Alexandria · Oct …" truncates | Deep-water covers washed with seven person hues, words and a top line | One neutral face for every clip: sunken surface, waveform, the badge; no hue, no words (the detector's text-overflow exception is gone) |
| q1: search, filters and tabs take a third of the phone screen | Search and Filters, then a full-width kind row; first tile at y = 264 | One row: kind chips, a Search button, an icon Filters button; the row ends at y = 180 (was 240) and the first date header sits at y = 213 of 844; title and controls take about 120 px, 14% of the screen. The cost: group headers and part-filled rows show about 9 tiles on the first phone screen where the single grid showed 12 |
| Midjourney: Images / Videos as quiet chips, the chosen one tinted. q3: "a component kit" | A sunken track with a raised white segment | Chips with no track; the chosen one in the accent tint and semibold, the sidebar's own "you are here" |

### Status: Linear status, incident.io incident
| Measured in the benchmark, or judged | `d1-11` | `d1-12` change |
|---|---|---|
| q1: Degraded is a muddy brown close to Down's red | Brown text (#8F5300) and a brown fill | Clear amber (#B45309 text, #D97706 fill; dark #F5B544 / #F2A93B) that parts from Down's red by lightness: ΔE 25 to 46 under every colour-vision simulation (`scripts/ui/state_palette.py`, DESIGN.md "State palette checks") |
| q3: the strip has no clock labels; the legend sits under the chart | "24 h ago … Now"; legend under the strip | Legend above the strip; 3:30 PM, 9:30 PM, 3:30 AM, 9:30 AM and Now under it |
| q2, q3: "3 spells: down 4 h, degraded 2 h" is jargon and wraps mid-phrase | One run-on line | Two lines: "Degraded since about 1:30 PM" (semibold), then "Down for 4 hours and degraded for 2 in the last 24 hours, in 3 separate stretches." at 16 px on wide screens, 14 px on a phone |
| q3, x3: on a phone the evidence is squeezed into a narrow right column | Label column beside values at 390 | One column on a phone: each label in 12 px medium above its value |
| Linear: one wide strip per row under the name, inside one card. q3: the lower half is empty beside a plain "Engines and providers" box | Folded summary box under the card; right column ends at y ≈ 580 | D1.5b: no side column at any width; the card, Capabilities (two columns of rows) and the folded engines line share one 896 px reading column, as on Linear's and incident.io's incident pages, so nothing stands empty beside them. Opened, each row is name over "Serves", a strip that takes the free width, hours up and state, one height (no per-row "Hour by hour"). (D1.5 opened the table at 1280 px instead; owner Status went from 86 to 212 words and the coordinator reverted it.) |
| q3: "the degraded warning is repeated three times" (header pill, status row, video row) | Header pill on every phone page | No header pill on Home and Status, which open with their own verdict |

### Home: Apple Home (iPhone)
| Measured in the benchmark, or judged | `d1-11` | `d1-12` change |
|---|---|---|
| Apple Home: a section title sits about 16 px over its content, sections about 32 px apart. q1, q2, q3: a dead gap above Recent | On a phone the status line sits 58 px under the title and "Recent" 77 px under it | Status line 47 px under the title (`PageHeader tight`) and "Recent" 68 px under it, close to the 45 px between Recent's tiles and "Make something". A modest change: the 44 px tap height of "Open Status" keeps a floor under the line |
| Apple Home: scenes as tiles, then accessories as tiles of one family. q1, q2, x3: two big cards then a bare list | Two 128 px tiles, then bare rows | The rows sit in one card under the tiles, with hairlines between them (two columns on a wide card) |

### Across surfaces (coherence)
| Judged | `d1-11` | `d1-12` change |
|---|---|---|
| q3: the 13-item sidebar gives Engines and Providers the weight of Home and Library | Two groups of equal weight | The owner's Gateway group sits under a hairline with 28 px rows, 12 px text and subtle ink |
| Three judges failed coherence | Three row styles (task rows, capability rows with 32 px tinted tiles, capability chips); the browser's disclosure triangle; a sunken tab track on Try | One row family (icon in its colour, name, state word, hairlines inside one card) on Home, Status owner and member; one chevron for every disclosure (including "Show evidence"); tabs and segments as chips |
| Blocked shared Busy's blue family (ΔE 4 to 6 for protan and deutan readers) | Violet #8B6FD1 fill | Deep violet in light theme (#5B3A9E), pale lavender in dark (#D6C4FF): ΔE 19 to 27 from Busy |

## Craft diff (D1.4, round 4 judges and the benchmarks)

Round 4 (set-5): win or tie 88 / 82 / 82 / 94%, Status 100% for all four, craft 7.0 to 7.2 against 7.8 to 8.1 (gap about 0.9). "Before" is `d1-10`, "after" is `d1-11`.

### Library: Midjourney, iOS Photos, Apple Music
| Measured in the benchmark, or judged | `d1-10` | `d1-11` change |
|---|---|---|
| All four judges: captions cut after two or three words read as noise; r3, x2: a badge on every tile | A 12 px caption under every tile; a maker mark on every tile in "Everyone" | Media only. Hover or focus shows a short title of whole words on up to two lines, the maker and the day; audio covers carry "Robin · Oct 20" |
| iOS Photos phone: 8 to 10 items per screen; r1, r3: only six | Two columns of captioned tiles | Three columns, 4 px gaps, no captions: about 13 items on a 390 × 844 screen (all 11 of Robin's) |
| r2, x2: no quick type switch on a phone; audio buried | Kind inside Filters | The kind switch has its own full-width line under search and Filters |
| r3: dark voice cards break the rhythm; x2: audio has low visibility | Covers with a caption under them | Same covers without the caption, so every tile is the same square; on a phone a cover is the maker's hue and a play disc |

### Home: Apple Home (iPhone)
| Measured in the benchmark, or judged | `d1-10` | `d1-11` change |
|---|---|---|
| Apple Home: the home's pictures, then favourites, then the rest; all judges: seven equal rows never show the next step | Seven equal rows with pastel chips, Recent last | Recent first; the person's two most-used tasks as 128 px tiles; the rest as quiet rows without chips |
| r1: Recent below the fold | Recent at y = 692 | Recent at y = 262 on a 390 × 844 screen |

### Status: Linear status, incident.io incident
| Measured in the benchmark, or judged | `d1-10` | `d1-11` change |
|---|---|---|
| incident.io desktop: the main column holds the incident and its context; r1, r3: an empty area under the card | Card alone in the left column; engines in the side column | Engines and providers under the card; Capabilities alone in the side column, so the columns end within about 150 px of each other |
| r3, x2: the 24-hour summary is a run-on sentence; r1: strip and legend small | One 20-word sentence; 16 px cells, 12 px legend | "Degraded since about 1:30 PM · 3 spells: down 4 h, degraded 2 h"; 24 px cells (16 px up), 14 px legend |
| r2: the engines line reads as prose | Two sentences | "Other engines: 4 ready, 1 turned off." "Remote providers: 1 set up." |
| r1, r2, r3: phone pills wrap and the tab bar cuts them | Wrapping pills | One column of 44 px rows in a card |
| x2, r1: a narrow wall of text on a phone | Instruction, button, then facts | Title, Affects (one line), Last 24 hours, instruction, button, "Show evidence" |

### Red team round 2: status of every P0 and P1
| Finding | Status |
|---|---|
| P0: member told "Busy, working on other requests" while the owner sees errors | Fixed in the UI (`3e004f8`): a gateway "busy" without an engine check reads Degraded, "May be slow or fail right now." Root cause is the gateway's projection (DESIGN backend ask 16) |
| P0: owner card names Robin's failed video, Robin's list lacks it | Explained, not a contradiction: members get their latest five requests (`gateway/home_api.py:354`); the list is titled "Your latest requests" (backend ask 19) |
| P0: stale health shown as fresh; Affects omits Voice and Transcription | Fixed: "Checked just now · 4 unknown"; "has not answered a check in 7 h"; a card for Unknown capabilities no engine card explains (backend ask 18) |
| P0: expired files are the newest | Capture artefact (the forced state expired the first four); fixed in the capture. One explanation for both roles, counted beside the title |
| P0: "Nothing is set up yet" beside recent pictures; member Status Ready in the same state | Capture artefact (each surface emptied one source); fixed in the capture. Copy is "Nothing is turned on yet" with who can change it |
| P1: one incident, five words | Fixed for this incident: the card's history uses the strip's own word (Degraded/Down) and members no longer get "Busy" |
| P1: issue badge during errors and loading | Fixed for an owner's failed checks ("Status unknown"); "Checked" is hidden while checking. Badge counts stay per capability (deferred: owners and members see the same count only when their evidence matches) |
| P1: budget card claims | Fixed: "refused paid requests", spend per period or "Unavailable", the refusal quoted, Affects, local reset times when known |
| P1: member warning with no route to Status | Fixed: "See what is happening" |
| P1: expired files below the grid | Fixed: beside the count |
| P1: "Check your connection" under server-side refusals | Fixed: the next step follows the failure |
| P1: "Simulated outage" fixture text | Fixed in the capture |
| P1: Home error points to Status without a link | Fixed: the retry is the next step |
| P1: running work on a failing engine | Fixed: "Video degraded: it may not finish" on the running tile |
| P1: Home Assistant voice is an app and an engine | Fixed in the fixture: the engine is "Voice and sound" |
| P1: no Share on the phone | Fixed: Share sends the file where the browser can |
| P1: Try flow not captured | Fixed: the capture takes Try for a picture and for reading aloud |
| P1: Home elevates the budget card, not the video fault | Deferred: Home shows one verdict line for every fault; a compact top card is a later Home change |
| P1: failed jobs never appear in Library | Deferred: `/me/api/media` lists succeeded jobs only (backend ask 17) |
| P1: provider down has no "since" | Deferred: providers have only a last check (backend ask 7) |
| P1: "Restart it" needs a terminal | Deferred: no restart or log API in the product yet |
| P1: member jargon (Degraded, Embeddings, model ids, Receipt) | Partly: limit and failure copy reworded; a member vocabulary layer is a later round |
| P1: signed-out screen looks alarming | Deferred to the next Home pass |

## Craft diff (D1.3, round 3 judges and the benchmarks)

Round 3 (set-4): win or tie 82 / 65 / 82 / 71%, AI-made 0 / 3 / 0 / 0 of 17, craft 6.8 to 7.0 against 7.7 to 8.4 (still failing for every judge). Each change below answers a weakness at least two judges named (`~/.tmp/azizam-judging/set-4-our-weaknesses.txt`), checked side by side with the benchmark at DPR 1. "Before" is `d1-9`, "after" is `d1-10`.

### Status: Linear status, incident.io incident (phone and desktop)
| Measured in the benchmark, or judged | `d1-9` | `d1-10` change |
|---|---|---|
| Three judges: "Started between 1:30 and 2:30 PM" contradicts red Down hours across the strip | "Started" came from the latest run of bad hours only | "Last 24 hours: Down 4 and degraded 2 of the last 24 hours, in 3 spells; failing again since about 1:30 PM", counted from the strip's own hourly slots. The fixture was inconsistent as well (its probe said degraded, its history said down) and is fixed |
| n1: "Affects: Video only repeats the title" | "Affects: Video" | "Video: no other engine serves it." plus who hit it: "A request from Robin's personal app failed 1h ago." with her mark |
| incident.io phone: instruction, then action; 3 short facts on the first screen | Button above its instruction; six facts with a bar and legend | Instruction, then button; Affects and Last 24 hours; the bar, legend and failed-request count fold behind "Show evidence" on a phone. "Capabilities" starts at y = 671 instead of 749 |
| incident.io desktop: a main column and a properties column; nothing stands empty | Card in a 896 px column, right third empty; "4 other engines ready, 1 off, 1 provider" | 1152 px page with an 18rem column: Capabilities as rows with their states, and "4 other engines are ready and 1 is turned off. 1 remote provider is set up." |

### Library: Midjourney, Apple Photos, Apple Music
| Measured in the benchmark, or judged | `d1-9` | `d1-10` change |
|---|---|---|
| Midjourney phone: one line of controls, grid at about y = 72 under the app bar; all four judges: controls take 40 to 50% of the phone | Grid at y = 346 (41% of 844) under search, kinds and person plus Filters | Search and one Filters control on one line (kind and person inside, counted in its badge). Grid at y = 204 (24%) |
| Midjourney desktop: controls in one row with the search | Search alone at the far right | One row from the left: search (256 px), kinds with icons, person with the household's marks, Filters; all 40 px |
| Apple Music: a title under every piece of art; judges: no titles or dates | No text on picture tiles | A 12 px caption: the subject from the prompt without its framing, and the day |
| n3: half-empty first row | Week groups; "This week" held 5 of 6 | One grid, newest first; dates live in captions |
| n2: voice cards cut mid-sentence | CSS clamp at four lines | Whole sentences, else whole words to a clause end, never a trailing "a" or "and"; 14 px at every width |

### Home: Apple Home (iPhone), Stripe iOS
| Measured in the benchmark, or judged | `d1-9` | `d1-10` change |
|---|---|---|
| Apple Home: the home's state is a row of small chips; the screen leads with the things you use. n2: the large verdict and its gap take the prime space | 20 px verdict and its age on two lines, "Make something" at y = 239 | One 14 px verdict line; "Make something" at y = 207 |
| Apple Home: one tile grammar. n3: photos and flat icons read as two systems, and "Used Xm ago" is noise | Photos in three rows, icons in four; "Used 39m ago" on two rows | One system: a 32 px tinted icon on every row; a state only when not Ready |
| Apple Home: cameras (live content) on the first screen. n1: Recent below the fold | Recent started below 844 px | Recent is a strip of four under the tasks, on the first phone screen (y = 692 to 775); 52 px rows on a phone, 28 px section gaps |

### Coherence
- One Callout anatomy on phones everywhere (instruction before action, full-width body); one caption line under every Library tile, in-progress tiles included; one row grammar for tasks; skeletons follow each new layout.
- Not changed, and why: Status keeps its 20 px verdict (it is that page's answer); the state vocabulary and freshness rules are unchanged; the Video verdict word stays "degraded" because the latest checks are degraded, and the card now says the day's history beside it.

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

D1.6 on `design/d12-continue` (includes a5c31a0, the dot's Activity states). Fixture servers on ports 18940 to 18990; nothing on 5000.

- axe WCAG 2 AA: 0 violations over 32 page checks (owner and member; Home, Status, Library, viewer; light and dark; 1440 and 390). Horizontal overflow, clipped text, targets under 44 px at 390, tab stops without visible focus and console errors: 0 (`~/.tmp/azizam-redesign/d1-14-gates`).
- Impeccable detector on 16 snapshots (`d1-14-snap`): only Sonner's toast height transition (recorded exception).
- Token drift: 0. State palette (`scripts/ui/state_palette.py --fail`): passes; Unknown's text is now 7.0:1 on cards (was 5.2).
- lint.py on a copy of the production data (eleven engines blocked): Home, Library and member Status pass. Owner Status reads 80 words against 60 (D1.5b: 86). The first crawl flagged "evidence" as a system word once the control showed at every width; it now reads "Show details".
- Unit tests: vitest 536 in the main config plus all 11 folder configs pass. `tsc` and ESLint: clean.
- Playwright, main config: the full run passed 601 of 612. Ten failures came from a5c31a0, which renamed Activity's error titles ("Couldn't load requests") without updating its e2e specs; one was a race in a Status spec's helper. Both fixed in 7ea2841; those three spec files then passed 46 of 46. Live-data config: 4 of 4. The task flows caught one regression of mine before the run (audio tiles could not be opened; fixed in 0c5df68 with a unit test).
- Hermetic Python at b858bbe: 5794 passed, 0 failed. D1.6 changes `web/` and docs only.
