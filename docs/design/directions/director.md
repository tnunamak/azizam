# Visual directions: director's set (S5, 2026-10-07)

Written by the design director (Opus). A blind pair from a different model lives in `blind-codex.md`. The round reduces all candidates to three that differ on different axes, plus the current UI as a labelled fourth, for checkpoint T1.

Every direction keeps the same product truths: one verdict sentence first, failure names its layer, honest money, member-first shell with an owner section (P8), phone first (P4). What changes is the visual world.

Contrast ratios are computed (WCAG 2.x relative luminance) against each theme's background. All listed text and status colours pass AA (4.5:1) for body text.

---

## A. Harbour at night (faithful to the designer handoff)

- **bg / accent / type:** `#0A1420` / `#3FD0E0` / Geist (UI and display), Geist Mono (model names, request IDs, numerals in tables).
- **Rationale:** the owner named the handoff "the absolute authority on look and feel"; this direction is that grammar with the rejected marketing copy and the generic-dashboard tells removed.
- **Real-world referent:** a harbour control room at night: dark water, a few lit instruments, one clear signal per channel.
- **References:** R18/R19 (incident-first status composition and the status hero with brand art), R16/R17 (mobile quickstart order and bottom navigation), R05 (Tailscale's compact layer list, for the failure layer rows), R06 (Uptime Kuma's dark operational list density).
- **Palette dark:** bg `#0A1420`, panel `#0F1E2D`, line `#1C2E3F`, text `#E6EEF3` (15.8), muted `#8FA3B3` (7.1), accent `#3FD0E0` (10.0). Status: ok `#3DD68C` (9.9), attention `#F2B84B` (10.4), failed `#FF6B6B` (6.7), unknown `#9AA9B8` (7.7).
- **Palette light ("sea mist"):** bg `#F1F5F7`, panel `#FFFFFF`, text `#0B1A26` (16.1), muted `#4A5D6C` (6.2), accent `#0E7490` (4.9). Status: ok `#0F7A4B` (4.9), attention `#9A5B00` (5.0), failed `#C2362F` (5.0), unknown `#5B6B78` (5.0).
- **Type roles:** display 32/36 semibold −0.01em; h1 24/30 semibold; h2 17/24 semibold; body 15/22 regular; small 13/18; numerals Geist Mono 15 tabular.
- **System:** spacing 4-based (4, 8, 12, 16, 24, 32, 48); radius 10 on panels, 8 on controls; elevation by 1 px line plus a faint inner glow on the hero only (no drop shadows); icons Lucide 1.5 px stroke.
- **First viewport:** Home (phone): "Your AI is ready" with the three things you can do now, then the address and your key. Status (desktop): the verdict sentence on the water-and-fish hero band, with any failing layer named inside the band. Library (desktop): a dark gallery wall where creations glow against the water.
- **Boldness goes:** the Status hero band, with the fish art generated through Azizam itself, and real telemetry, never invented.
- **Wireframe, Home at 390 px:**
  ```
  [~ azizam ............ (S)]
  Your AI is ready
  Chat · Images · Voice  ✓   Video: starting
  ┌ Connect an app ─────────┐
  │ Open WebUI  LibreChat … │
  │ https://ai.example/v1 ⧉ │
  │ Your key: phone · 3d ago│
  └─────────────────────────┘
  Recent  [img][img][audio]
  ─────────────────────────────
  [Home] [Apps] [Library] [More]
  ```
- **Wireframe, Status at 1440 px:** a hero band across the full width, with the verdict on the left, fish art on the right, and the failing layer as a row inside the band. Below it, two columns: the capability table (Chat, Images, Video, Voice, each with status text, latency and a 7-day trend from real data) and "What changed" (incidents and recoveries).
- **Wireframe, Library at 1440 px:** a filter row (Mine / Everyone, kind, search) above a masonry wall of 4 columns, with captions on hover and focus.
- **Self-check:** "Would I produce this for any similar product?" The base grammar is common to dark ops dashboards (ban list A5). It stays specific through the harbour art, the layer rows, and the absence of KPI tiles without data. It is closest to ban A2 (near-black with one accent) and A5; it avoids them with a blue-green water ground rather than black, four semantic colours, and no gradient-glow cards.

## B. Kitchen appliance (light first, plain and physical)

- **bg / accent / type:** `#FAFAF9` / `#1F5FD6` / Inter Tight (display), Inter (UI), JetBrains Mono (exact strings only).
- **Rationale:** a family member should read it like a well-made household device: big plain labels, one indicator per function, nothing decorative.
- **Real-world referent:** the front panel of a good dishwasher or a home thermostat: a single readout, a few labelled buttons, a status light that means one thing.
- **References:** R01 (Home Assistant: household intent groups, tiles that are real controls), R13 (Tailscale invites: one task, email plus role in one row), R03 (OpenAI Status: capability groups, quiet disclosure), R10 (Stripe: a number always with comparison context).
- **Palette light:** bg `#FAFAF9`, panel `#FFFFFF`, line `#E6E7EA`, text `#16181D` (17.0), muted `#5A6170` (6.0), accent `#1F5FD6` (5.5). Status: ok `#13795B` (5.1), attention `#9A5B00` (5.2), failed `#C2362F` (5.2), unknown `#636B78` (5.2).
- **Palette dark:** bg `#14161A`, text `#EDEEF0` (15.6), muted `#9AA1AD` (7.0), accent `#7AA7FF` (7.6). Status: ok `#4CC38A` (8.2), attention `#F0B24A` (9.6), failed `#FF7A70` (7.1).
- **Type roles:** display 40/44 Inter Tight semibold −0.02em (the readout); h1 26/32; h2 18/24; body 16/24; small 14/20; numerals Inter tabular.
- **System:** spacing 4-based, generous (16, 24, 32 are the common steps); radius 14 on tiles, 10 on controls; elevation by shadow only (one soft level), no borders on tiles; icons filled, 24 px, in a circle when they are a control.
- **First viewport:** Home (phone): one large readout ("Ready" / "2 things need attention") and a grid of capability tiles that are real actions (Chat, Make an image, Talk). Status (desktop): a readout plus a row of indicator lights per layer (Your apps, Gateway, Engines, Providers), each with one sentence. Library (desktop): big friendly tiles grouped by person ("Robin's", "Alex's").
- **Boldness goes:** the readout, a single big number or word set in the display face that carries the whole page's answer.
- **Wireframe, Home at 390 px:**
  ```
  Ready                      (S)
  Everything works.
  [ Chat ][ Image ][ Voice ]
  [ Video·starting ][ Music ]
  Connect an app  >
  Your creations  >  [ ][ ][ ]
  ```
- **Self-check:** the risk is the SaaS card kit (ban A4). It avoids that because the tiles are controls, not containers, there are no gradient washes, and the readout breaks the grid. It could pass for a smart-home app, which suits the household audience but weakens the "AI gateway" identity.

## C. Gallery wall (content supplies the colour)

- **bg / accent / type:** `#121214` (dark default) / no brand accent: primary actions use ink or white, and colour comes only from creations and status / Söhne-like grotesk via "Hanken Grotesk" (UI and display), "IBM Plex Mono" for exact strings.
- **Rationale:** the most human thing in Azizam is what the household makes. Let the creations be the colour and the UI be the frame around them.
- **Real-world referent:** a small gallery's white-cube room, inverted for evening: neutral walls, labels beside the work, light on the work.
- **References:** R11 (Plex: artwork-led browsing, household navigation), R12 (Apple Photos: media first, controls at the edge), R09 (Linear: a quiet dark working shell for owner pages), R08 (MCP: content priority on text pages).
- **Palette dark:** bg `#121214`, panel `#1A1A1E`, text `#F2F2F3` (16.7), muted `#A1A1AA` (7.3). Status: ok `#58C98D` (9.1), attention `#F0B24A` (10.0), failed `#FF7A70` (7.4), unknown `#A1A1AA` (7.3).
- **Palette light:** bg `#FFFFFF`, text `#111113` (18.9), muted `#5B5B66` (6.7). Status: ok `#13795B` (5.4), attention `#9A5B00` (5.4), failed `#C2362F` (5.4).
- **Type roles:** display 36/40 medium −0.015em; h1 24/30 medium; body 15/22; small 13/18 muted; captions in the "museum label" style (title, then a muted line with person and model).
- **System:** spacing 8-based; radius 6 on media, 6 on controls (one rule); elevation none, separation by space and a 1 px line; icons 1.25 px outline, small.
- **First viewport:** Home (phone): the household's latest creation full-bleed with its label, then one readiness line and the "Connect an app" card. Status (desktop): a plain text verdict, then a simple layer list in museum-label style. Library (desktop): edge-to-edge grid with labels under each piece.
- **Boldness goes:** Home's full-bleed latest creation (with a quiet fallback image when the household has made nothing yet), and the Library wall.
- **Wireframe, Home at 390 px:**
  ```
  ┌─────────────────────────┐
  │   [ latest creation ]   │
  │                         │
  └─────────────────────────┘
  "A garden at dusk" · Robin · 2h
  Your AI is ready. Video is starting.
  Connect an app  >
  ```
- **Self-check:** the risk is ban A2 (near-black with a single accent); C has no accent at all, and its colour comes from content. It could be "any gallery app" on member pages; owner pages need the Linear-like shell to carry it. It is the most distinctive for members and the weakest for an operator's at-a-glance health.

## D. Current UI, labelled (for the owner's real choice)

- **bg / accent / type:** `#F6F4EC` / deep green / Newsreader (serif display) with a sans for UI text. This is ban A1 ("warm cream with serif"), the look the playbook hypothesises is the Opus house style; the owner's "sand is claudy" points the same way. It is shown at T1 so the choice is real, rendered with the same fixtures as A to C.

---

## Reduction plan

1. Merge this set with the two blind directions from `blind-codex.md`.
2. Run dot blind scoring on the text plans: three workers, rubric only, no ledger.
3. Pick three that differ on different axes, for example colour strategy (water-dark vs light-physical vs content-colour), hierarchy (hero band vs readout vs artwork-first) and density.
4. Build the three as HTML comps of Home (390), Status (1440) and Library (1440) on the household fixture, plus D. Then T1.
