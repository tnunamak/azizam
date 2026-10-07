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
- `assets/`: 18 creation images, the video poster, and `fin-art-crop.png`.
- `build.py`: generates the HTML. `render.mjs`: renders the PNGs with Playwright Chromium from `azizam/web`.

## What matches the spec

- **Tokens:** exactly six UI colours per theme. Dark is the default. Light comes from `prefers-color-scheme`.
- **Fonts:** IBM Plex Sans (UI and tabular numerals) and IBM Plex Sans Condensed (verdicts only). The comps load them from Google Fonts. The fonts are also installed locally, and the renders confirm they loaded.
- **Sizes:** the type scale follows the spec. Controls have a 4 px radius and enclosing regions 8 px. Borders only, no shadows. Structural lines use 24% text opacity and row separators 16%. Controls and focus use the full accent.
- **Shell:** sidebar 208 px, top rail 56 px, 24 px gutters. Admin is a disclosure. The phone has a 56 px header and a 64 px labelled bottom bar plus the safe area. Full-page captures make the bar static.
- **States:** every state has words and its own icon: check (OK), cross (failed), question mark (unknown, in the accent colour), and pause (Paused).
- **Home:** an 80 px-high fin-art crop sits inside the opening task region, then the actions, apps, recent creation and restrictions. No KPI band. The disabled embeddings engine reads "Paused".
- **Status:** the verdict comes first. The incident shows its layer (the engine), who is affected, its evidence and one action. The incident engine does not appear again in the paths table. A separate history column follows.
- **Library:** three columns of 224, 632 and 264 px. The viewport is 0 px radius inside an 8 px viewer. File and receipt facts sit in the slim right rail. The missing cost reads "Cost not recorded". Filters sit in one toolbar.

## What I could not match, and why

- **History matrix:** the seed has no saved capability checks. A 7-day check matrix would be invented data. The spec's fallback is "No history saved". Instead, I kept the matrix form but filled it with real request counts (owner Usage, Oct 1–7, by capability; total 267). Bars are in the neutral text colour, and the legend says outcomes are on receipts. The matrix is about 290 px tall, not 128 px, because there are 6 real capability rows, not 4. **Reviewers should decide** whether this matrix or the spec fallback is right.
- **Paths table:** it has 7 rows, not 8 or more. The data has only that many paths outside the incident.
- **Evidence and check times:** there are no per-row evidence times and no "Checked 1m ago", because the data has no check times. The incident cites the last failed video request (Open WebUI, 21h ago) instead.
- **Resolved incidents:** none exist, so the column says "No resolved incidents saved." To avoid an empty column, I added three real change events from Activity under "Recent changes". This region is not in the spec.
- **Creation titles:** creations have no titles. The prompt is the title: two lines, cut short, in the row, and in full under the viewer. Fix 2 makes the selected row repeat what the viewer shows. This overrides the spec's timestamp-only rule.
- **Home order:** the art is beside "Chat is ready." (fix 1), not above it as in the wireframe. The art is 96×80 px. "Chat is ready." stays on one line.
- **"Inspect selected path" and video/audio transport:** not shown. No path is selected, and the selected creation is an image.
- **Library full-page captures:** the browse list scrolls on its own in the viewport captures. In full-page captures it expands, which leaves empty space under the viewer. That gap comes from the capture, not the layout.

## Choices where the spec is silent

- **Fin art:** a crop of the fish from `reference-ui/portal-overview-mobile-welcome.png`, the only copy of the approved art. It is a dark navy block in both themes.
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
  - The member video sentence ("The problem is in Sam's video engine, not your app.") rests on Video studio being the only video engine and being Unavailable.
  - "Your last chat request worked 20h ago" is Robin's real completed chat receipt.
- **Library:** I used the owner view, because only the owner has a detail capture with Download, Copy prompt and View receipt. I added a "Person" select next to "Type". The detail rail shows WebP and 768 × 768 from the fixture. I left out the "Video studio unavailable" banner, because Status owns that fact.
- **Contrast** (WCAG, computed): text 13.98/12.50, accent 7.76/6.39, OK 8.15/6.51, failed 7.07/6.22, and primary-button text 7.76/6.39 (dark/light). All exceed 4.5:1. I changed control borders, including the search fields, from the 24% line (1.58:1 in light) to the accent, so every control outline is at least 3:1.
