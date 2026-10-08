# D1 contact sheet: Home, Status, Library

Captures: `~/.tmp/azizam-redesign/d1-3/` (made by `scripts/ui/d1-states-capture`, branch `design/full-surface`). One folder per household scenario. File names are `<scenario>_<role>_<surface>_<state>_<theme>_<width>.png`, with the page text beside each PNG and a `manifest.json` per folder (console errors and forced states are recorded there). Every state exists for owner (Sam) and member (Robin), at 390 and 1440 px, in light and dark. Loading, error, empty and expired are forced by request interception and labelled so in the manifest.

Benchmark ids refer to `benchmarks/BOARD.md`.

## Home

| State | How it meets the benchmark traits |
|---|---|
| populated | Verdict first, one sentence of five words, with "Checked just now" (Linear 2a.1, Apple 2d.1). Tiles: icon, one-word name, state as icon + colour + word (Apple Home 1a.4). "Connect an app" holds the address, your apps with last use (Resend 6b.3) and the app choices (Tailscale 6d.2). Recent media without per-tile text (Midjourney 3a.2). Owner: "Inspect Video studio" beside the verdict (judge-s5-r2). |
| loading | Skeletons in the final layout, no layout shift; `aria-busy` region. |
| error | A panel with a tinted icon, a heading and one sentence plus "Try again" (Geist 12c.1). |
| empty | "Nothing is ready yet." Owners get "Add an engine"; members get the ask-the-owner sentence (Geist 12c.2: first run has an inline action). |
| signed-out | "Sign in to use your AI" with one filled button (Linear 11a.2). |
| restricted (Alex, Teen plan) | The same Home scoped to the plan; unavailable tiles are not links and carry the reason. |
| budget-exhausted | A Notice under the verdict quotes the gateway's own refusal ("The Everyone plan has used its daily and monthly remote spending allowance."), the last refusal time and its receipt; it names who can fix it (incident.io 2c.1: the reader's next action). |
| new-member | Persian name renders in the person mark; on phones "Connect an app" comes before the tiles because no app has made a request yet. |
| running, provider-down, stale-health | Verdict and tiles follow current checks; a check time is shown per tile only where it differs from the verdict's. |

## Status

| State | How it meets the benchmark traits |
|---|---|
| populated, owner | Verdict "Video is degraded." with check time (Linear 2a.1). One problem card: Where (layer), Affects (who), Last error, Last 24 hours as an uptime bar with the gateway's percentage (Linear 2a.3), Recent failures with the person and receipt, one plain fix and "Inspect Video studio" (incident.io 2c.1–2c.3, Linear 2b.3). Every capability with a labelled state icon. Healthy engines fold behind "Show all" (Apple 2d.2). |
| populated, member | Same verdict sentence as Home; "Your video request failed 1h ago." with the layer it reached and what to do; capabilities and own receipts; model names one tap away. |
| loading | "Checking…" verdict with the unknown icon; skeleton cards. |
| error | Owner: "Couldn't check status." with a card naming the gateway as the layer. Member: "Couldn't check your status" with the HTTP status and no blame on the reader's connection (VOICE rule 4). |
| empty | "No engines yet." with "Add an engine". |
| signed-out | Sign-in prompt. |
| budget-exhausted | Owner: two cards, the engine and an "Access plan" card ("Paid requests are being refused.", who was refused, how often, "Open access plans"); only the first card's action is primary. |
| provider-down | Owner: a provider card "The last connection check failed." with the layer "Provider · Remote backup". |
| stale-health | Hours without a check stay empty in the uptime bar instead of shifting the history. |
| running | Member: requests in progress read "In progress". |

## Library

| State | How it meets the benchmark traits |
|---|---|
| populated | One grid of square tiles with ~6 px gutters and no card chrome (Midjourney 3a.1–3a.2), grouped into this week, last week and months. Kind is one segmented control and the owner's person filter sits beside it (Midjourney 3a.3); model, date and app are behind "Filters". Each tile shows its maker's person mark (Suno 3b.1). Phone: two columns, grid within the first 300 px, four-tab bar (Midjourney 3a.4). |
| library-detail | Viewer: media on the deep-water stage, short title, maker and age (Suno 3b.1–3b.2), Download, Copy prompt, Receipt, then facts (model, created, size, made in, charge) and the prompt once (Replicate 4b.1, 4b.5: charge shown). Close top-left on phones (Apple Photos 3c.1). |
| library-video | Playable video with native controls, frame preloaded; length in the facts. |
| library-audio | Audio tiles play in place (Apple Music 3c.3–3c.4); the viewer has one large play control, a seek bar and both times. |
| loading | Square skeleton grid. |
| error | Error panel with "Try again". |
| empty | "No creations yet" with "Make an image"; filtered empty says "No matching creations" with "Clear filters" (OpenRouter 12d.3 anti-trait fixed). |
| expired | Files the gateway no longer stores fold into one line with "Show them" and "Refresh library"; owners are told the retention period. |
| signed-out, restricted | Sign-in prompt; the restricted member sees only their own creations. |

## Gate results for this capture set

See the report in the D1 thread: token drift 0; axe WCAG 2 AA 0 violations over 32 checks; overflow, clipped text, 44 px targets, focus visibility and console errors 0; Impeccable detector clean apart from Sonner's height transition; lint.py word budgets pass for owner and member Home, Status and Library on the production copy.
