# Workspace comps: notes

Spec: `docs/design/directions/blind-codex.md`, "Direction 2: Workspace" and "Rules shared by both directions". Two fixes from blind scoring override the spec (see below).

## Files

- `home.html` (390 px phone, member Robin), `status.html` (1440 desktop, owner Sam), `library.html` (1440 desktop, owner Sam).
- PNGs, viewport: `home-{light,dark}-390.png` (390x844), `status-{light,dark}-1440.png`, `library-{light,dark}-1440.png` (1440x1000).
- PNGs, full page: the same names with `-full`. Full-page captures make the phone bottom bar static.
- `render.mjs`: renders every PNG with the Chromium from `~/applications/azizam/web/node_modules/@playwright/test`. It also reports horizontal overflow and the lowest text-contrast ratios. Run `node render.mjs [home|status|library]`.
- `assets/`: 18 Library images and the video poster from the household fixture.

## What the comps match from the spec

- Palette: the six colours per theme exactly as specified. Light is the default; dark comes from `prefers-color-scheme`. Borders are text colour at 28%. There is no opacity on text.
- Type: Archivo for the page title and the active consequence, Source Sans 3 for UI text with tabular figures, and Source Code Pro only for model names. Sizes and weights follow the table: phone 30/36 and 28/34, desktop 36/42 and 30/36. **The fonts load from Google Fonts**, so offline rendering falls back to system fonts.
- Shell: 184 px sidebar, 56 px top rail, Home / Apps & keys / Try / Library, then an Admin disclosure with Status, Engines, Providers, People, Usage, Activity and Settings. Phone: one 56 px header and a labelled bottom bar (Home, Apps & keys, Library, More) at 64 px plus the safe area.
- Controls: 6 px radius, full text-colour outlines, accent fill for primary buttons, at least 44 px high. Action rows are at least 56 px and Status rows 52 px. There is one 12 px enclosure per page, thumbnails use a 2 px radius and nothing has a shadow. Selection is a 2 px accent outline around the whole item.
- State: every state has a word and an icon shape (check, cross, and pause for Paused). Colour is never the only signal.
- Status: a 280 + 28 + 884 layout. The capability list is on the left with Video selected. The expanded incident names the layer (engine: Video studio), quotes the recorded error and gives one primary action. A two-lane evidence chart names its period, a receipt table serves as the table alternative, and one inline row gives spend, engines and provider.
- Library: a 768 + 24 + 400 layout. A three-column contact sheet uses 248 px cells, 12 px gaps and 160 px uncropped (aspect-fit) previews. One toolbar holds the search field and labelled selects. The preview rail has file actions and receipt facts.
- Contrast, measured from computed styles on every text node: the lowest ratio is 6.23:1 in light mode (Failed and Unavailable text) and 7.64:1 in dark mode. No text sits on an image. There is no horizontal overflow at any size.

## Overrides from blind scoring

1. Phone Home leads with readiness ("Chat is ready." plus evidence) and the primary actions. The 96 px art sits **beside the two action buttons**, so it adds no height above them. The spec put it beside the "Home" title.
2. In Library, the selected creation keeps its title in its collection cell, shown in bold. The spec gave the title only to the preview rail. To avoid repeating the same text, the rail shows the full prompt under "Full prompt" and no title heading. The cell clamps the title to two lines.

## What I could not match, and why

- **Resolved incidents list:** the seeded data has no resolved incidents, so this region is omitted.
- **"Checked 1m ago" evidence age:** the data shows no check time for Video or any other capability. I show no ages instead of making one up. The Home evidence line uses a real receipt instead: Robin's chat request that completed 20h ago.
- **Chart by 5-minute interval, past hour:** there is no such data. The chart uses real daily Video counts from the Usage page (Oct 1 to 7: 2, 3, 4, 3, 2, 2, 1). The Failed lane shows the one failed video receipt (`req_household_00_27`, 21h ago, which is Oct 7 UTC). The data does not give success or failure for the other requests, so the chart shows "All requests" and "Failed" instead of "Successful" and "Failed". The note under the chart names both sources. One caveat: the Activity list loads only 51 receipts, and I could not fully reconcile it with the Oct 6 Usage count. The chart uses the Usage figures as shown.
- **"Unchecked" capability state:** no capability in the data is unchecked, so no question-mark state appears.
- **Spec wireframe text such as "Images unavailable" and "Studio stopped":** the spec marks this text as hypothetical. It is replaced with the real Video problem.
- **Brand art:** removed in fix pass 2.
- **Audio waveforms:** none are extracted from the files, so audio cells show a play control and the duration only, as the spec allows.

## Choices where the spec was silent

- **Paused:** (superseded by fix pass 2; no Paused state remains)
- **Embeddings state:** Unavailable (fix pass 2). The member text "Ask Sam to turn it on for your account." comes from "Ask the owner to enable this capability."
- **Speech and Transcription readiness** on the owner Status page comes from the member Status page. The owner page lists engines only.
- **Video wording for members:** "Unavailable. Its engine is recovering after a queue timeout. Ask Sam if you need it." The cause is quoted from the receipt warning.
- **App rows on Home** offer "Setup", because the data has no launch targets. Telegram helper shows "Not used yet", because the real UI shows it with no last-used time.
- **Money format** follows VOICE ("USD 0.45 of USD 8.00 cap"), not the UI's "$0.45 of $8.00".
- **Status actions:** "Open Video studio" (primary) and "Open video receipts". The spec's "Inspect layers" became the real "Open engines".
- **Library groups:** "Last 24 hours", "1 to 6 days ago" and "7 to 11 days ago". These are derived from the relative times in the data, because absolute dates are not available.
- **Cell captions:** each caption is the person and the relative time, such as "Sam, just now". The video caption also gives the duration.
- **Library toolbar:** I added a labelled "Person: Everyone" select because the owner Library has that filter.
- **Selected item:** the selected creation is kitchen-morning, the item the real detail capture opens.
- **Preview facts:** kind, size, model and maker come from `manifest.json`, and the time comes from Library. Cost reads "Cost not recorded" because the detail view shows no cost. The actions (Download, Copy prompt, View receipt) are the real ones, plus the spec's "Open full view".
- **Selected nav item:** an accent outline, accent text and a filled icon.
- **Account control:** a 6 px-radius initial plus the name on desktop, and the initial only on the phone.
- **Chart colours:** bars use the text colour and the failed bar uses the Failed colour. Bars are separated by lane as well as colour.

## Fix pass 2 (judge and text review)

1. **Art removed:** the deep-water art on Home is gone (the judge matched it to the banned "AI" glow). Open chat and Connect an app now use the full width. This drops the spec's "one memorable element" on Home.
2. **Readiness:** "Ready" now comes from the current engine and capability state in the Status captures. Real receipts appear only as a secondary "Last used" line: Chat 20h ago, Images 11h ago, Speech 21h ago, Transcription 20h ago. Music and Sound effects have no receipts for Robin, so they show no line.
3. **Row actions:** each capability has one task label in a single text-link style ("Make an image", "Read aloud", "Transcribe audio", "Make music", "Make a sound").
4. **Embeddings:** now Unavailable on both Home and Status, following "Video and embeddings are unavailable." The Paused glyph is removed. Status still leads with Video, because the owner capture reports "1 problem needs you".
5. **Home app rows:** used apps have no action. Telegram helper keeps "Set up". A single "Manage apps and keys" link gives access to configuration.
6. **Status:** the incident is now a plain section under a rule. The failed receipt row sits directly under the incident actions. The by-day chart is in a `<details>` element, closed by default. The completed receipt from 2d ago is removed because it only repeated older evidence. The page now ends at about y=600 of 1000, which is below the top third.
7. **Library:** frames are now square (aspect-ratio 1), so square images have no gutters. The video poster (3:2) still shows a band inside its square frame. The rail is the only large preview. The selected cell keeps its thumbnail, selection outline and bold title (coordinator correction: the "Shown in the preview" placeholder was removed).
8. **Accessibility:** both search boxes are labelled `<input type="search">` elements, and Type and Person are labelled native `<select>` elements. Each audio and video cell has a native `<button>` named "Play audio: ..." or "Play video: ..." as a sibling of the selection link, not inside it.

## Fix pass 3 (data honesty)

1. **Telegram helper** has no usage line. The data has no last-used time for it, so the earlier "Not used yet" was an inference and is removed. Robin's personal app (1h ago) and Open WebUI (4h ago) keep their real times from the member Apps capture.
2. **Video chart:** the Failed lane is removed, because the receipts do not cover failures for all of Oct 1 to 7. The chart is now one series titled "All video requests by day, Oct 1 to Oct 7", with counts from Usage. The note under it states only the observed receipt: "Latest failed request: 21h ago, Open WebUI." This supersedes the chart description in "What I could not match".
