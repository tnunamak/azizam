# D1.1 contact sheet: Home, Status, Library

Captures: `~/.tmp/azizam-redesign/d1-7/` (made by `scripts/ui/d1-states-capture`, branch `design/full-surface`). One folder per household scenario. File names are `<scenario>_<role>_<surface>_<state>_<theme>_<width>.png`; each has a `_viewport.png` twin (1440×900 or 390×844, not full page, which is what the benchmarks show) and the page text in a `.txt`. A `manifest.json` per folder records console errors and forced states. Every state exists for owner (Sam) and member (Robin), at 390 and 1440 px, in light and dark. Loading, error, empty and expired are forced by request interception and labelled so in the manifest. The fixture's public address is `https://ai.household.example`.

Benchmark ids refer to `benchmarks/BOARD.md`. State words and rules: DESIGN.md, "Status model".

## Home

| State | What it shows, and the trait it meets |
|---|---|
| populated, member | Title, then the verdict label "Video degraded" with "Checked just now" (Linear 2a.1). "Make something": Make a picture, Read text aloud, Chat as three task cards into Try, then music, sound effect, video and transcription as rows; only Video carries a state word (Apple Home 1a.4: exceptions only). "Recent" media (Midjourney 3a.2). "Connect an app" is a secondary panel on the right, below everything on phones. |
| populated, owner | The same Home with "Open Status" beside the verdict. |
| new-member | No app has made a request, so the panel opens on the app chooser with real marks (Open WebUI, LibreChat, SillyTavern, Cursor, Codex, Claude Code, Another app). On a phone it still comes after the tasks. Persian name in the person mark. |
| budget-exhausted, member | "Paid models were paused for you": the household reached its monthly limit, when it resets, ask the gateway owner. Chat reads Degraded: "Some models are paused: a spending limit was reached." |
| budget-exhausted, owner | "A spending limit refused paid requests" with the gateway's sentence, the reset and "Open Status". |
| stale-health | Voice, Transcription, Music and Sound effects read Unknown; the verdict counts them ("5 of 7 capabilities need attention"); nothing says Ready on old evidence. |
| loading / error / empty / signed-out | Skeletons in the final layout; a critical Callout with "Try again"; "Nothing is set up yet"; "Sign in to use your AI" as an info Callout with one button. |

## Status

| State | What it shows, and the trait it meets |
|---|---|
| populated, owner | Verdict "Video degraded". One full-width card "Video studio is degraded": left, "Video studio is answering with errors.", "Check the engine's log and restart it.", "Inspect Video studio"; right, Affects, Failing since, "16 of 24 checked hours up, last 24 h" with the hourly bar, and "1 of the latest 50 requests, from Robin" with its receipt (incident.io 2c.1–2c.3, Linear 2a.3). While the card is open, the capabilities fold to "1 degraded, 6 ready, 1 off" and the engines and providers to "4 other engines ready, 1 off, 1 provider", each with one control to open them (Apple 2d.2). |
| populated, member | The same verdict as Home; "Your video request failed" with what state Video is in now; "Your capabilities"; "Model names"; "Your recent requests" with Failed and In progress. |
| running, owner | "Running now" lists the two requests in progress, with their people. |
| budget-exhausted, owner | Two cards: Video studio, and "The Everyone plan reached its spending limit" with limits (USD 1.00 a day, USD 8.00 a month), spend not reported by the gateway, the gateway's sentence, both resets, who was refused, and "Edit the Everyone plan". |
| provider-down, owner | "Remote backup is down": the last connection check failed, Chat is still served by an engine on this computer, 7 of the latest 50 requests failed from Robin, Zoë and Sam. |
| stale-health, owner | "Home Assistant voice is not answering checks", last check 7 h ago; behind "Show capabilities", Music and Sound effects read Unknown, "Last checked 7 h ago; no answer since." (brief item 2, proven; the d1-6 set shows the list open). Member: Voice, Transcription, Music and Sound effects read Unknown, "No answer to the latest check." |
| error | Owner: "Couldn't check status" and a card naming the gateway. Member: "The gateway returned an error. Try again." with the HTTP code under Technical details; a fetch that never reached the gateway names no layer. |
| loading / empty | "Checking" with skeletons; "No engines yet" with "Add an engine". |

## Library

| State | What it shows, and the trait it meets |
|---|---|
| populated | Title with count, search on the right; kind (segmented), person and "Filters" at one height. "This week", "Last week", "Week of …" groups. Square tiles with the maker's person mark (Midjourney 3a.1–3a.2, Suno 3b.1). The video's poster is folded into the video, so the count is 24. |
| library-audio | Audio tiles are full squares on the media stage with the prompt, duration and a centred play button. |
| library-video / library-detail | The viewer: media on the stage, designed play, position, times and mute; title, maker and age; Download, Copy prompt, Receipt; facts with display names (LTX Video), size, length, made in, charge. |
| expired | Gone files fold into one line; owners are warned before pictures expire. |
| loading / error / empty / filtered empty | Square skeletons; a critical Callout with "Try again"; "No creations yet" with "Make an image"; "Clear filters to see more." |
| running | Calm placeholders for work in progress. |

## Gate results for this set

Run on the final D1.1 build (default household, port 18447):

- axe WCAG 2 AA: 0 violations over 32 page checks (owner and member; Home, Status, Library, viewer; light and dark; 1440 and 390). Horizontal overflow, clipped text, targets under 44 px at 390, tab stops without visible focus and console errors: 0.
- Impeccable detector: only Sonner's toast height transition (the recorded exception) on all 16 snapshots at 1280 and six at 390.
- Token drift: 0.
- lint.py on a copy of the production data (every engine blocked): Home, Library and member Status pass. Owner Status reads 69 words against a budget of 60, because a twelve-engine outage keeps its evidence (affects, engines, failed requests). Workflows, Activity and Usage are over budget and out of scope.
- Unit tests: every vitest config passes. Playwright: the 17 e2e tests D1.1 broke pass again; 37 tests in the same five files were already failing at e196d79 (D1) and still fail.
