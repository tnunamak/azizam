This direction treats Azizam as a household utility, with the intimacy of a kitchen noticeboard and the precision of a service receipt. Deep petrol (#123F4A), cool porcelain (#EDF3F3), sea glass (#BBD9D4), and muted coral (#A1392F) replace decorative glow; dark mode uses #102F38 beneath #EAF3F1 text. Lato supplies open, humanist lettering, with weight and size doing the organizing. The topology is horizontal: a small household masthead, an uninterrupted working surface, and navigation expressed as ordinary words. Home puts a usable chat entrance before occasional tasks. Status follows Robin’s failed request into the unavailable engine. Library gives each person a row of square creations with a See all control. The memorable element is the interrupted connection line, which makes a household member’s problem traceable without turning the page into a dashboard. Would I produce this unchanged for any similar product? No: named household authorship and the member-to-engine relationship determine the composition, rather than decorate a reusable software shell.

## Direction contract

- Operate on Home and Status; Experience on Library. Static design studies, not a production implementation.
- R01: tasks beside current state, touch targets at least 44 px. R16: phone sequence and bottom navigation. R18: incident and action before healthy engine inventory. R12: media occupies its own square, never letterboxed.
- Preserve handoff atmosphere through saturated petrol surfaces and strong hierarchy, not blue glows, slogans or invented telemetry.
- Prior directions were inspected in both themes. This direction removes their sidebars, global search, initial avatars, readiness cards, alert boxes and chart modules.
- The real-world referent informs hierarchy, not decoration: no paper textures, pins, handwritten notes or pretend physical controls.
- The Impeccable context command ran successfully. Its concept script was attempted inside the authorized output folder and refused because PRODUCT.md is at the repository root. The user's explicit one-direction, output-folder-only brief supersedes the skill's broader direction tournament and documentation writes.

## Sources and content boundaries

- Product truth: PRODUCT.md, PRINCIPLES.md, VOICE.md, ban-list.md, taste-ledger.md and references/board.md in the design worktree.
- All public-board and seven handoff images inspected via evidence contact sheets retained here. Prior console, workspace and gallery viewport PNGs inspected in light and dark.
- Household text: ~/.tmp/azizam-household-shots/, member Home, owner Status, Library, Engines, Activity failure receipt, Usage.
- Media, makers, exact prompts and durations: household fixture manifest.json. Captions are full source prompts or contiguous excerpts ending with an ellipsis; there are no invented creation titles.
- Readiness comes from member_status_-ui-status-_page_light_390_settled.txt: Chat, Images, Speech, Transcription, Music and Sound effects are Ready. Successful historical requests are not used as proof of health.
- Status ties the unavailable Video studio to req_household_00_27. The recorded queue-timeout note is shown as recorded context, not an independently established root cause.

## Gaps

These are static comps: destination app URLs, live health polling, remediation, authentication and access enforcement are not implemented. The available evidence does not establish whether members can inspect owner Status. Owner Library is shown with the supplied everyone scope; this does not grant members that scope. Owner taste remains unvalidated.

## Files

- `home.html`, `status.html`, `library.html`: standalone documents referencing Lato and fixture media files in assets/; no inline data URIs. Theme follows the system or `?theme=light|dark`; each has a theme switch.
- Twelve PNGs: `{home,status,library}-{light,dark}-{390,1440}.png` and corresponding `-full.png` files (Home is 390, desktop surfaces are 1440).
- `build.py`: generates the three documents from the fixture manifest and embedded CSS, reusing external assets by content hash. `render.cjs`: renders with the requested Playwright package, audits text contrast and checks interactions.
- `verification.json`: final computed results. `detector.json`: first mechanical design scan. `FONT-LICENSE.txt`: bundled Lato licensing. `evidence-*.jpg`: reference contact sheets used during orientation.

## Verification

- All twelve requested PNGs inspected. Final Home captures are 390×844; Status and Library viewport captures are 1440×1000. Full heights: Home 1095, Status 1160, Library 1897. Full captures make the bottom navigation static.
- Computed visible authored text contrast: Home light 5.46:1 minimum, Home dark 6.80:1, Status and Library light 4.95:1, dark 5.30:1. All pass WCAG AA text thresholds, including smaller secondary copy. This is a text contrast check, not a complete accessibility certification; browser-native media control internals are excluded.
- No horizontal overflow, broken loaded images or page JavaScript errors. All square image thumbnails remain square. Theme switch, prompt search, audio filter and full-image viewer checks pass in both themes.
- Removed duplicate owner footer copy and a hidden viewer's initially empty image. The video now starts from its actual poster and a named Play video button; native controls mount visually after activation, avoiding Chromium's loading spinner in static captures.
- The mechanical detector reported Lato as an overused font. Kept deliberately for readable humanist forms: the proposed identity rests on authorship and the request relationship. Its hidden-image warning is fixed. The scan is retained as the original record, not presented as a clean final scan.
- Confidence is high in screenshot completeness, source content and computed contrast. Whether this direction feels sufficiently distinctive needs the owner's judgment. Library rows keep their width as household membership grows. A temporary six-person DOM fixture passes the row-stacking and document-overflow checks in both themes; the saved content remains the supplied three-person household.
- No commits. All authored files are inside `docs/design/comps/astra/`. Other changes visible in the worktree belong to concurrent work and were not edited.


## Round 2 — one fix pass

1. Status now says “The Video studio engine is unavailable.” Open engines remains the action. The queue-timeout statement remains only in Recorded context; no connection fault is asserted.
2. Restored “Remote spend · partial” and “Cost measured for 249 of 265 requests.” next to the monthly amount, verified against owner_usage_-ui-usage-_page_light_1440_settled.txt.
3. Focus uses coral tint #BF7567 in light mode: masthead #123F4A 3.245:1; page #EDF3F3 3.142:1; white surface 3.526:1. On pale inset surfaces, the original #A1392F is used instead. Dark mode uses #FFB8A7: masthead 6.900:1; page #102F38 8.525:1; surface #193E48 6.944:1; inset #254C55 5.645:1. Ratios use sRGB relative luminance, with the 3:1 gate in render.cjs. Keyboard focus is checked in both themes.
4. Home keeps chat first and exposes all five other Ready capabilities as task rows: Make an image, Read aloud, Transcribe audio, Make music and Make a sound. Each says Ready. Video remains plainly unavailable; the redundant More tasks disclosure is removed.
5. Library uses one row per person, with a horizontal strip of their creations in fixture order, newest first. See all expands that person's strip into a wrapping grid; Show latest restores the strip. Names remain visible above their own work. This preserves grouping for 6+ members without compressing columns or imposing a fixed person count. Search and kind filters still work across every row, including creations beyond the visible strip.

All twelve PNGs were regenerated by render.cjs and individually inspected: each light/dark viewport and full capture for Home, Status and Library. No authored text-contrast failures, document overflow, broken images or JavaScript errors were found. Theme switching, keyboard focus, readiness copy, partial-spend copy, See all, six-person stacking, search, audio filtering and image viewing pass. Horizontal clipping at a Library strip's edge is intentional; See all exposes all items without horizontal scrolling. Home's Recent section is now below the initial viewport because all Ready tasks are visible first.

The round-2 mechanical scan is detector-round2.json; it reports only the existing Lato preference warning. T3 preview_open reported that the host blocks its browser sandbox, so the requested existing render script provided the screenshots and interaction checks. No live service was changed. No commit was made.
