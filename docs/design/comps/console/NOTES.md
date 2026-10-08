# Console comps: notes

These comps build Direction 1, Console, from `docs/design/directions/blind-codex.md`. They also apply two required fixes from blind scoring:

1. On the phone, Home leads with readiness and the primary action. The art sits beside the readiness line.
2. In Library, the selected creation keeps its title in its row.

All data comes from the seeded household. The sources are `~/.tmp/azizam-household-shots/*.txt` and the fixture `manifest.json`.

## Files

- `home.html`: phone, 390 px, member view (Robin).
- `status.html`: desktop, 1440 px, owner view (Sam), with the Video incident.
- `library.html`: desktop, 1440 px, owner view (Sam), with `kitchen-morning` selected.
- PNGs follow the pattern `<surface>-<light|dark>-<width>.png`, and `-full.png` for full-page captures. Viewports: home 390×844, status and library 1440×1000.
- `assets/`: 18 creation images and the video poster.
- `build.py`: generates the HTML. `render.mjs`: renders the PNGs with Playwright Chromium from `azizam/web`.

## Fix pass 2 (judge and text review)

1. **Home art:** the fin-art thumbnail is deleted. Open chat now spans the full width.
2. **Home readiness:** "Chat is ready." rests on current engine state; the owner Status capture shows both chat engines as Ready. The receipt is now only secondary evidence, labelled "Last used 20h ago".
3. **What else works:** a list directly under Open chat. Each row pairs a capability with a task-named action: Images "Make an image", Speech "Read aloud", Transcription "Transcribe audio", Music "Make music", Sound effects "Make a sound". The member Status capture shows all five as Ready. The Video exception and the Embeddings pause close the list. All of this comes before apps and the creation, in both DOM and visual order.
4. **Member video line:** now "Video isn't working right now. Ask Sam if you need it." It gives no diagnosis.
5. **Status:** the request-count matrix is removed, using the spec's fallback. The scoped verdict is the only bold element. Then come the layer, who is affected and Check engine. The last failed request sits directly under the action. Current states for each capability and path follow, and a line links to Usage for request volume.
6. **Library:** the left column is now a 4-column grid of square frames, cropped with `object-fit: cover`, so nothing is letterboxed. The 768x512 video poster is cropped too. Every cell keeps its title (the prompt, two lines). The selected cell has a 2 px accent outline and a heavier title. A detail panel on the right shows a 350 px preview, the full prompt, file facts, Download, Copy prompt, Open receipt and "Cost not recorded".
7. **Accessibility:** Type and Person are native `<select>` elements with associated `<label>`s. Person options are the real household members: Everyone, Sam, Robin, Alex. Search fields show focus on their wrapper through `:focus-within`. A tab-through script found every stop on all three pages has a solid 2 px outline with a 2 px offset (14, 18 and 37 stops).

## What matches the spec

- **Tokens:** six UI colours per theme. Dark is the default; light comes from `prefers-color-scheme`.
- **Fonts:** IBM Plex Sans and Sans Condensed, loaded from Google Fonts. They are also installed locally.
- **Sizes:** the type scale follows the spec. Controls have a 4 px radius and regions 8 px. Borders only. Lines are 24% and separators 16%. Controls and focus use the accent.
- **Shell:** sidebar 208 px, top rail 56 px, 24 px gutters, Admin disclosure. The phone has a 56 px header and a 64 px labelled bottom bar plus the safe area. Full-page captures make the bar static.
- **States:** every state has words and a distinct icon: check, cross, question mark (accent colour), and pause for Paused.
- **Status:** the incident's engine does not repeat in the paths table, and resolved history is separate.

## What I could not match, and why

- **Home art:** removed on instruction, so Home no longer has the spec's single art element.
- **Status memorable element:** with the matrix removed, the verdict is the only bold element. This is the spec's own fallback.
- **Paths table:** 7 rows, not 8 or more, because the data has only 7. There are no per-row check times, because the data has none.
- **Resolved incidents:** none exist. I added three real Activity changes so the column is not empty.
- **Creation titles:** creations have no titles, so the prompt is used.
- **Library layout:** it no longer follows the spec's 224/632/264 viewer layout; it follows fix 6 instead.
- **Library full-page captures:** these show an empty area under the detail panel, because the panel is not sticky.

## Choices where the spec is silent

- **Selected items:** a 1 px accent outline for nav items and a 2 px accent outline for Library rows. The phone tab bar uses accent text and a heavier stroke.
- **Secondary buttons:** a transparent fill, an accent border and text-colour labels. Primary buttons are filled with the accent.
- **Navigation:** I followed the spec's lists. The real nav also has Models and Workflows, and its phone bar has Status, not Library. The Try icon is a speech bubble. Engines is a server stack, Providers a cloud, People two figures, and Settings sliders.
- **Wording:**
  - Engine states use the data's word "Ready", not the wireframe's "Working".
  - The gateway row uses "Reachable", from "Gateway reachable".
  - The unchecked provider reads "We haven't checked yet".
  - Money uses VOICE format: "USD 0.45 of USD 8.00".
- **Inferences (please confirm):**
  - Remote backup "Serves: Chat" comes from the Engines page entry "Backup (chat)".
  - Telegram helper reads "Not used yet" because the real UI shows no "Last used" value for it.
  - "Last used 20h ago" is Robin's real completed chat receipt.
- **Library:** I used the owner view, because only the owner has a detail capture with Download, Copy prompt and View receipt. I added a "Person" select next to "Type". The detail rail shows WebP and 768 × 768 from the fixture. I left out the "Video studio unavailable" banner, because Status owns that fact.
- **Contrast** (WCAG, computed): text 13.98/12.50, accent 7.76/6.39, OK 8.15/6.51, failed 7.07/6.22, and primary-button text 7.76/6.39 (dark/light). All exceed 4.5:1. I changed control borders, including the search fields, from the 24% line (1.58:1 in light) to the accent, so every control outline is at least 3:1.
