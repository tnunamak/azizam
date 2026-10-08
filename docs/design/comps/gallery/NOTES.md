# Comp C: Gallery wall — build notes

Source spec: `docs/design/directions/director.md`, section "C. Gallery wall". Data: the household seed. Page text came from `~/.tmp/azizam-household-shots/*.txt` and media from `azizam-wt-household-seed/scripts/ui/fixtures/household/` (`manifest.json`).

## Files

- `home.html`: member Robin, 390 px phone.
- `status.html`: owner Sam, 1440 desktop, showing the real "Video is unavailable" problem.
- `library.html`: owner Sam, 1440 desktop, Everyone's, all 24 creations.
- `assets/*.webp`: 18 household images plus the video poster (`garden-breeze-poster.webp`). No audio or video files are needed because the comps are static.
- PNGs, named `<surface>-<theme>-<width>.png`, plus `-full.png` full-page versions:
  - `home-{light,dark}-390[-full].png` (viewport 390x844)
  - `status-{light,dark}-1440[-full].png` (viewport 1440x1000)
  - `library-{light,dark}-1440[-full].png` (viewport 1440x1000)
- These files are outside the output folder, so nothing else in the repo was touched:
  - Generator: `~/.tmp/gallery-build/gen.py`. It reads the manifest, so every caption, owner and model comes from the data.
  - Renderer: `~/.tmp/gallery-build/render.mjs`. It uses the Playwright chromium from `azizam/web/node_modules`. For full-page captures it changes fixed and sticky elements to static (ban 31), and it checks for overflow. No overflow was found.

## Matched from the spec

- Palette dark: bg `#121214`, panel `#1A1A1E`, text `#F2F2F3`, muted `#A1A1AA`, status ok `#58C98D`, failed `#FF7A70`. Dark is the default theme.
- Palette light: bg `#FFFFFF`, text `#111113`, muted `#5B5B66`, ok `#13795B`, failed `#C2362F`. Light is applied with `prefers-color-scheme: light`.
- No brand accent. The primary action ("Open engines") uses ink: a white button in dark, a near-black button in light. All other colour comes from the creations and from status.
- Type: Hanken Grotesk for UI and display, IBM Plex Mono for exact strings only (model names `krea2-turbo` / `ltx-2.5` and the request ID). Both load from **Google Fonts**. Plex Mono is also installed locally.
- Type sizes: display 36/40 medium −0.015em, h1 24/30 medium, body 15/22, small 13/18 muted.
- Museum labels: the title (the prompt) first, then a muted line with person, model and time.
- System: spacing on an 8 grid, radius 6 on media and controls, no elevation (separation by space and 1 px lines), outline icons at 1.25 px stroke.
- Status: a plain text verdict, then a layer list in museum-label style. The failing layer is named with one action (P3).
- Library: an edge-to-edge grid with a label under each piece.
- Home: the full-bleed latest creation with its label.

## Required fix applied (overrides the spec)

On Home, readiness ("Your AI is ready." plus the real unavailable capabilities) and the "Connect an app" card come **above** the artwork. The spec's wireframe puts the artwork first. In the 390x844 viewport, the top of the artwork still shows above the tab bar.

## Could not match, and why

- Spec wireframe copy "Video is starting." The data says video and embeddings are **unavailable** (member Status), so the comp uses the real state.
- Spec wireframe "A garden at dusk · Robin · 2h". Creations have no titles in the data, so the prompt is the title, clamped to 2 lines (full text in `title`/`alt`).
- The "Connect an app" card cannot show the gateway address. No URL exists in the captured data, so the card uses the real copy "One address. A key for each app." and the real app list.
- There is no fallback image for an empty household. The household has creations, so the fallback state is not shown.

## Choices where the spec was silent

1. **Light panel, line and ink colours.** The spec gives no light panel or line colour. I chose panel `#F4F4F5` and line `#E4E4E7`. The dark line is not given either; I chose `#2A2A2F`. The light "unknown" status colour is the light muted `#5B5B66`.
2. **Whose latest creation on Home.** The member (Robin) sees their own latest creation (the vegetable bed, 11h ago), because the current member Home and Library show only Robin's work ("Everything I've made"). The household's latest is Sam's kitchen image. The label says "You" instead of "Robin".
3. **Home readiness copy.** "Your AI is ready." (display), then "Video and embeddings are unavailable. Ask Sam if you need them." Sam is the owner in People, and the member Status copy says "Ask the owner to enable this capability."
4. **Home additions.** I added a "More of yours" row with 3 thumbnails and a "View library" link (real Robin items) so the page does not end on one image. I dropped the "What you can use" capability chips: the readiness line carries that fact (P1, P2, ban 18).
5. **Home artwork** is full-bleed with radius 0. The radius-6 rule applies to inset media only.
6. **Owner shell.** A quiet left sidebar with the real nav groups ("You" / "Gateway", in normal case, not tracked caps), and a Search ⌘K box. The Appearance control is omitted.
7. **The "Gateway reachable" shell indicator is hidden on Status only.** On Status, the Layers list already says it, so showing it twice would break P2 / ban 20.
8. **Status composition.** The page has the verdict, then a problem block (the panel colour, not a card shadow) with the layer, "What to know" (real receipt text: "Video studio is recovering after a queue timeout."), the last failed request and its request ID, and one ink action "Open engines" plus a "View receipt" link. Below that are two columns:
   - **Layers:** Gateway, Engines (4 of 5 ready, counted from the 5 real engines), Providers.
   - **Recent requests:** the first 14 real Activity rows, so the page does not end in the top third (ban 27).
9. **Spend meter.** One thin bar for the real "$0.45 of $8.00 this month" (P7). It uses the text colour, not an accent.
10. **Status dots.** An 8 px dot plus coloured text. No coloured left borders (ban 14).
11. **Library filters.** Underline text tabs for kind (the only tab pattern), a search field and 3 plain selects: Everyone's, All models, Any time. I dropped "All sources" to keep the filter row from crowding (taste ledger: chip filter bars "look like a mess").
12. **Library tiles.**
    - Grid: 5 square columns.
    - Audio tiles: the spoken text in quotes, on the panel colour, with a play control and the duration (from `duration_s`).
    - The video tile: the poster image with a "Video · 0:03" badge (white on 72% black).
    - Model display names come from the current UI: "Krea2 Turbo", "TTS 1", "LTX Video".
    - Times are the current Library's relative times, in manifest order.
13. **Header count.** "24 creations" is counted from the manifest.

## Checks

- **Contrast** (WCAG 2.x, computed). Every pair passes AA 4.5:1:
  - Dark: text 16.7 on bg and 15.5 on panel; muted 7.3 and 6.8; ok 9.1 and 8.4; failed 7.4 and 6.8; the ink button 16.7.
  - Light: text 18.9 and 17.2; muted 6.7 and 6.1; ok 5.4 and 4.9; failed 5.4 and 5.0; the ink button 18.9.
  - Video badge: white on 72% black has a worst case of 9.3 (over a white image).
- **Visual review.** I looked at all 12 PNGs and found no clipped text, overflow or empty regions. The Library's last row has one empty slot (24 items in 5 columns).
- **Not checked.** I did not run the `impeccable detect` lint, and I did not test hover or focus states in a browser.
