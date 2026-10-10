# Craft system: measurements

Owner: the lead designer (Opus 5.5). Date: 2026-10-09. Phase 2, step 2 (`~/Documents/azizam/briefs/phase2-plan.md`).

Earlier rounds patched the weakness each judge named, page by page, and craft levelled off at about 7.0 against about 7.9 for the benchmarks. This file turns the benchmarks into numbers and measures Azizam the same way, so the next pass changes the system rather than single pages. The rules built from these numbers are in `../DESIGN.md`, "System numbers v2". The order of work is in `PLAN.md`.

## Method

- **Sources.** The benchmark board v2 and v3 (`docs/design/benchmarks/`, on branch `design/benchmarks-v3`). Only logged-in product screens are measured. Marketing pages, docs images and App Store composites are skipped.
- **Scale.** The v3 Mobbin desktop shots are 1920 px wide. They are 1440 CSS px at 1.333×: Vercel's 32 px button and 40 px input measure 31.5 and 40.5. The 2400 px Stripe Workbench and Resend shots, and the 2084 px Linear marketing shots, are 2×. The phone shots are 3×. The v2 live shots are 1×. Every number below is in CSS px.
- **Tools.** `tools/bands.py` finds the rows of ink in a crop, which gives text cap heights, control and row edges, and gaps. `tools/colorshare.py` gives the share of coloured pixels. Font size is estimated as cap height ÷ 0.72 (Inter, Geist, SF and Hanken all have a cap height of 0.70–0.73 em), or ink band ÷ 0.93 when the band includes a descender. Estimates are good to about ±1 px. Where a size sits between two steps, the step the product's own design system publishes is given (Geist: 13, 14, 16, 20, 24, 32; Linear: 13, 14, 15, 24).
- **Azizam.** The fixture gateway uses the household seed on port 18420, owner session, light theme, DPR 1, at 1440 × 900 and 390 × 844. Computed styles were read with Playwright (`azizam/*.json`, `azizam/summary.md`). The screenshots are in `azizam/`.

## 1. Benchmarks

### 1.1 Per screen (desktop)

| Screen (file) | Page title | Section title | Body / row | Secondary | Labels | Controls | Rows | Card | Notes |
|---|---|---|---|---|---|---|---|---|---|
| Vercel Members (`people/vercel-team-members-roles-desktop`) | 24 / 600 | 20 / 600 (card title) | 14 / 500 name | 14 muted | 14 | button 32, input and select 40 | 2-line member row 70 | padding 32, muted footer band 56, radius 8, 1 px border | Status: amber tag, 20 tall. Avatars 30–32 |
| Vercel Firewall (`settings/vercel-firewall-settings-desktop`) | 24 / 600 | 24 / 600 (h2 between cards) | label 14 / 500 + description 14 | 14 muted | — | select 40, button 40, danger button 40 | setting row 80 | padding 24 × 20, hairline dividers | Section gap 30; h2 to card 20 |
| Vercel Usage (`usage/vercel-usage-consumption-desktop`) | 24 / 600 | 20 / 600 | 14 | 14 muted | 14 muted (th) | filters 40, segmented 32 (segments 24) | table row 38 | KPI card 95 | Colour only on legend dots and the progress bar |
| Vercel AI Gateway models (`catalog/vercel-ai-gateway-model-list-desktop`) | 24 / 600 | — | 14 | 14 muted | 14 muted (th, muted header band 44) | 40 | row 61 (2-line cell), single-line cells 48 | table in a bordered card, radius 8 | Numbers right-aligned, tabular |
| Linear Settings › Security (`settings/linear-security-saved-desktop`) | 24 / 500 | 15 / 500 | 13 / 500 label | 13 muted | — | input 32, button 32, toggle 29 × 20 | setting row 62 (2-line) | radius 8, 1 px border | Centred column about 610 wide; title to first section 38; section gap 30; nav row 28 |
| Linear Preferences (`settings/linear-account-prefs`, 2×) | 24 / 500 | 16 / 500 | 14 / 500 | 13 muted | — | select 32–36 | row 60 | radius 8, border, one step lighter | |
| Linear Team permissions (`people/linear-members-roles`, 2×) | — | 18 / 500 + 15 muted | 14 / 500 | 13 muted | — | select 32, filled | row 60 | radius 8, border | |
| Stripe Transactions (`activity/stripe-transactions-failed-desktop`) | 28 / 700 | — | 14 | 14 muted | 13 / 600 (th) | primary 32, filter chips 24 (dashed) | table row 35 | filter tiles 66 tall | Status: tinted badge 20 tall, radius 4, 12 / 600 word + icon; nav row 28 |
| Stripe Workbench Logs (`usage/stripe-workbench-logs`, 2×) | — | — | 13–14 | 13 muted | — | filter input 27 | log row 37; detail key–value row 31 | — | Status: neutral bordered "✓ 200" chip |
| OpenAI Usage (`usage/openai-usage-spend-desktop`) | 20 / 600 (in a 54 tall header bar) | 14 / 500 (card label) | 14 | 14 muted | 13 muted | 32 | — | chart cards radius 10, border | KPI number 24–28 / 600; nav row 34 |
| Mistral API keys (`keys/mistral-org-api-keys-desktop`) | 20 / 600 | — | 14 | 14 muted | 13 / 500 muted (th) | primary 40 (black), search 40 (filled) | 2-line row 58 | radius 12, faint shadow | Tags are tinted chips 20–22 tall |
| Supabase project (`engines/supabase-project-health-desktop`) | 32 / 400 | 24 / 400 | 14 | 13 muted | tracked caps (banned for us, B6) | 32 | status row 46 (2-line) | radius 6, border | Healthy = small green check plus a muted word |
| incident.io incident (`status/incidentio-incident-detail-desktop`, 1×) | 16 / 500 (card title) | 16 / 500 | 14 / 21 | 13 muted | 13 muted | 32 | — | radius 8, border, padding 16 | Column 670; section gap 24; strip cells 16 tall |
| Google AI Studio run (`try-playground/google-ai-studio-image-run-desktop`) | 18 / 500 (header bar) | 16 / 500 | 14–15 / 20 | 13 muted | 14 / 500 field label | select 32, composer 105 | — | settings cards padding 16, radius 8, border | Composer has the only shadow |
| ElevenLabs Text to Speech (`try-create/elevenlabs-text-to-speech-desktop`) | 16 / 500 (header bar) | — | editor 17 / 22.5 | 13 muted | 14 / 500 field label | field 40, primary 36 (black), tabs 14 underline | — | — | Right settings panel about 345 wide |
| Midjourney Create (`try-create/midjourney-create-desktop`) | — | 16 / 400 ("Today") | 14 | 13 muted | — | prompt bar 48 | — | radius 8 | Grid gutter 8; nav row 48 with 20 px icons |

### 1.2 Phone (iOS app captures at 3×, and v2 live at 1×)

| Screen | Page padding | Title | Body | Row | Primary |
|---|---|---|---|---|---|
| Linear iOS Settings (`settings/linear-app-settings-phone`) | 20 | 17 / 600 (nav title) | 17 | 48 | — |
| Instacart team members (`people/instacart-team-members-pending-phone`) | 16 | 30 / 700 (large title) | 16–17 | 64 (3-line) | 56, full width, pinned |
| Things 3, Apple Home, Stripe iOS (`phone/*`, `home/*`) | 16–20 | 28–34 / 700 (large title) | 15–17 | 44–52 | 44–50 |

### 1.3 Ranges and consensus

| Property | Range across benchmarks | Consensus | Evidence |
|---|---|---|---|
| Page title | 20–28, weight 500–700 (Supabase 32 / 400 is the outlier) | **24 / 32, weight 600, −0.01 em** | Vercel ×4, Linear ×2 at 24; Stripe 28; OpenAI and Mistral 20 |
| Section title | 15–20, weight 500–600 | **16 / 24, weight 600**; **20 / 28** only for the title of a whole card | Linear 15–16, incident.io 16, AI Studio 16; Vercel card titles 20 |
| Body and row text | 13–14 (editors 15–17) | **14 / 20** | Every desktop benchmark |
| Secondary text | 13–14, muted ink | **13 / 18** under a row name; **14 / 20** for sentences | Linear 13, Stripe 13–14, Vercel 14 |
| Labels (table header, field label, meta) | 12–14, weight 500–600 | **13 / 18, weight 500**; muted for table headers, ink for field labels | Stripe th 13 / 600, Mistral 13 / 500, field labels 14 / 500 |
| Badge text | 12–13, weight 500–600 | **12 / 16, weight 500** | Stripe 12 / 600, Vercel tag 12 |
| Display number | 24–28, weight 600 | **24 / 32, weight 600, tabular** | OpenAI KPI, Stripe iOS |
| Distinct font sizes on one screen | 3–5 | **at most 4 on a page** (plus badge text) | Linear 3, Vercel 4, Stripe 4 |
| Weights | 400 / 500 / 600 (700 only for Stripe's title) | **400, 500, 600** | |
| Page padding beside the sidebar | 20–40 | **32** at ≥1024; **24** at 640–1023; **16** on phone (16–20) | Vercel 27, OpenAI 20, Stripe 38; iOS 16–20 |
| Content width | Lists: full width; settings and detail: a 610–720 column | **List pages up to 1200; settings, detail and status at 720** | Linear settings 610, incident.io 670 |
| Title to first content | 24–40 | **24** (with no subtitle), 8 from title to subtitle | Vercel 27, Linear 38 |
| Section gap | 24–32 | **32** | Vercel 30, Linear 30, incident.io 24 |
| Heading to its content | 12–20 | **12** | Vercel 20, incident.io 12, Linear 16 |
| One-line row (table, list) | 35–40 | **40** (36 dense, for Usage and Activity tables) | Stripe 35–37, Vercel 38 |
| Two-line row (name and secondary) | 56–70 | **60** | Mistral 58, Linear 60–62, Vercel models 61, Vercel members 70 |
| Settings row (label, description, control) | 60–80 | **64** (padding 12 × 16) | Linear 60–62, Vercel 80 |
| Card padding | 16–32 | **16** for list-in-card rows; **24** for a form or content card | incident.io 16, AI Studio 16, Vercel 24–32 |
| Table header band | 36–44, muted fill or none | **36**, 13 / 500 muted, no fill | Vercel 44 filled, Stripe 36 plain |
| Button | 28–40 | **32** for page, toolbar and row actions; **40** for the primary action of a form or creation surface | Vercel 32 / 40, Linear 32, Stripe 28–32, OpenAI 32, ElevenLabs 36, Mistral 40 |
| Input, select | 28–40 | **36** default; **32** in toolbars and rows | Linear and AI Studio 32, Vercel and ElevenLabs 40, Stripe 28 |
| A button next to an input | same height in every benchmark | **same height, always** | Vercel filter row, Linear copy field |
| Segmented control | 32 tall, segments 24–28 | **32 tall, segments 28** | Vercel Usage |
| Phone controls | 44–56 | **44**; a full-width primary **48** | iOS HIG; Instacart 56 |
| Control radius | 4–8 (pill only in OpenAI) | **6** | Vercel, Linear, Stripe |
| Card radius | 6–12 | **8** | Linear, incident.io, Vercel 8; OpenAI 10; Mistral 12 |
| Badge radius | 4 to full | **4** for status; full only for a person | Stripe 4 |
| Border vs shadow | Cards: a 1 px hairline about 1.1–1.3:1 against the page, no shadow. Shadow only on menus, popovers, toasts and the composer | **Hairline only; a shadow only for overlays** | All 14 screens |
| Field border | A light hairline (Vercel #EBEBEB, Linear about 1.3:1) that darkens on hover and focus | see DESIGN.md "Controls" (a director decision) | |
| Icons | 16 in nav and rows (glyph 14); 12–14 in badges; 20 only for large nav (Midjourney) and empty states | **16** in nav, rows and buttons; **14** in badges; **20** in empty states and on phone tabs | Vercel and Linear nav glyphs measure 14 |
| Avatars | 24–32 | **32** in member rows, **20** inline | Vercel 30–32 |
| Density (rows per 1000 px) | Tables 26–28; two-line lists 14–17; settings 12–16 | Tables **25**, people and keys **16**, settings **15** | from the row heights above |
| Colour share | Coloured pixels are 0.1–1.7 % of the screen (Linear's status strip 7.9 %); 0–19 % of the ink, median 5 % | **at most 2 % of the screen and about 5 % of the ink** outside charts and media | `tools/colorshare.py` over 14 screens |
| Accent use | One accent (Vercel uses black ink) for the primary button, links, the selected tab or filter and progress. Status hues appear only in small badges, dots and strips | same | |
| Status display | A word plus an icon in a tinted badge 20 tall (Stripe, Vercel); a dot or check plus a muted word for healthy (Supabase, Vercel); strips for history (incident.io). Healthy is the quietest state | same; Ready stays quiet | |


### 1.4 A font correction: Hanken Grotesk is small for its size

The benchmarks set text in Geist, Inter or SF, whose x-height is 0.54–0.55 em. Azizam uses Hanken Grotesk, with an x-height of 0.493 em and a cap height of 0.697 em (read from the shipped woff2 with fontTools; Geist Sans reads 0.54 and 0.71). So the lowercase letters of Hanken at 14 px are as tall as Geist at 12.8 px. Running text is read by its x-height, and headings by their cap height. So:

- **Text (body, secondary, labels):** add about 1 px to a benchmark size to reach the same visual size. The benchmarks' 14 body becomes **15**, their 13 secondary becomes **14**, and their 12–13 labels become **13**.
- **Headings:** cap heights match within 2 %, so the benchmark sizes carry over unchanged (title 24, section 16).

This explains a finding the judges kept making that page-level patches never fixed: "small, dense type" (D1.5), "secondary text small and low-contrast" (round 6). Azizam's 14 px rows read as Linear's 13 px secondary text.

## 2. Azizam (fixture, household seed, owner, light, 2026-10-09)

Screenshots: `azizam/<surface>-1440.png`, `-390.png`, plus `-full.png`. Computed styles: `azizam/<surface>-<width>.json`. Summary: `azizam/summary.md`. Token and utility inventory: `azizam/tokens-inventory.md`. Scripts: `azizam/_scripts/` (`measure.mjs`, `summarize.py`, `utility_counts.py`).

### 2.1 Per surface at 1440

| Surface | Title | Section heading | Body / row name | Secondary | Labels | Controls (px) | Rows | Page padding (left, from the sidebar) | Section gaps | Coloured pixels / coloured share of ink |
|---|---|---|---|---|---|---|---|---|---|---|
| Home | 24 / 600 / 30 | 16 / 600; plus a third level, "Your usual" and "Everything else", at 14 | 14 / 500 | 14 and 12 | — | outline 40 | task rows 45; favourite tiles 128 | 52 | 12–40 (median 20) | 6.1 % / 18 % (photos and tinted task icons) |
| Status | 24 / 600 | 16 / 600 | 14 / 500 and 15 | 14 | dt 15 / 400 | primary 40, outline 32 | capability rows 44 | 52; column 896 | 12–40 (median 14) | 3.3 % / 8 % |
| Library | 24 / 600, count 16 | — | — | — | badges 10 and 12 | input 40, select 40, chips 36 and 40 | tile 179, gutter 6 | 52 | 16–24 | 26.5 % / 43 % (media, plus 24 maker marks in 7 hues) |
| Try | 24 / 600 | none (the chip names the task) | 15 / 600 | 14 | 14 / 500 | Run 40, select 44, tabs 37 in a 44 list, textarea 64 | — | 52 | tabs to form 64 | 0.8 % / 3 %; content ends at y = 517 of 900 |
| People | 24 / 600 | — | 15 / 500 | 14 and 12 | th 12 / 400 | primary 40, ghost 40, tabs 36 in a 44 list | person row 75 (2 lines) | 52 | 32 | 1.2 % / 25 % |
| Usage | 24 / 600 | 15 / 600 (line height 15) | 14 / 500 | 14 and 12 | 14 / 500 field labels above the selects | select 44, outline 40, ghost 40 | model table row 77 | 52 | 32 | 10.6 % / 70 % (a chart stacked in 6 saturated hues) |
| Workflows | 24 / 600 | 16 / 600; card titles 15 / 600 | 14 / 400 | 14 | count 12 | **primary and outline 44 on desktop**, search 40, filter chips 44 | card 350 tall for one workflow | 52 | 20–120 | 2.1 % / 7 % |
| Settings | 24 / 600 | 15 / 600 | 14 / 400 / 24 | 14 | — | Edit 40, section links 32 | setting row 113 | 52; column 1024 | 0 between rows (rules only) | 1.0 % / 18 % |
| Shell | — | group label 12 | nav 14 | — | — | nav row 32 | — | sidebar 232 | — | — |

Across all 8 surfaces at 1440: **7 font sizes** (10, 12, 14, 15, 16, 20, 24), **3 weights**, **6 control heights** (32, 36, 37, 40, 44, 64) and **4 radii** (0, 6, 10, full).

### 2.2 Phone (390)

Page padding is 20. Every control is 44 tall, as the touch-target gate requires. Rows grow because they wrap. People rows are **165** tall (benchmark two-line phone rows: 64–72), Usage table rows 77, and Settings rows 155. The title starts at y = 85, below the 56 px header.

### 2.3 Causes found in the code

- **44 px leaks onto the desktop.** `min-h-11` appears 76 times without a `max-sm:` prefix, and `h-11` 17 times (`azizam/tokens-inventory.md`). Workflows applies `[&_button]:min-h-11` to the whole page (`app/workflows/page.tsx:881`). The select's default size is `h-11` at every width (`components/ui/select.tsx:39`). That is why one desktop screen mixes 32, 36, 37, 40 and 44.
- **The button's default size is 40** (`components/ui/button.tsx`, `size.default: h-10`). The benchmarks' page and toolbar actions are 32.
- **The type scale is renamed.** `text-base` is 15, `text-lg` is 16, and there is no 13 step (`styles/tokens.css:186–205`). Pages pick 14 or 15 for the same role: body is 14 on Home, 15 on People, and 14 / 24 on Settings.
- **The shadcn Card is still the kit default:** `rounded-xl border py-6 shadow-sm gap-6` (`components/ui/card.tsx:9`). Usage turns it off with `border-0! bg-transparent! shadow-none!` four times (`app/usage/page.tsx:543, 570, 700, 821`), so Usage's KPIs, chart and table float on the page with no container.
- **There is no list, row or settings primitive.** Each page builds its own rows, which is why row heights are 44, 45, 75, 77 and 113 for the same two-line anatomy.
- **Settings repeats its sections as a link row** (`app/settings/page.tsx:1500`, "Settings sections") above sections that are already all on screen. This is ban 22.
- **Page padding is 48** at ≥ 1024 (`app/globals.css`, `.shell-content` `lg:px-12`), and **content runs to 1200 px** on pages whose benchmarks use a 610–720 column (Settings, Try's form).

## 3. Comparison: where Azizam is off, and by how much

Benchmark sizes for running text are converted to Hanken (section 1.4).

| Property | Benchmark consensus (Hanken-corrected) | Azizam today (1440) | Off by | Where |
|---|---|---|---|---|
| Control heights in use on desktop | 2 (32 and 40) | 6 (32, 36, 37, 40, 44, 64) | +4 heights | all |
| Page, toolbar and row button | 32 | 40 (44 on Workflows) | +8 to +12 (+25 to +38 %) | all |
| Select in a toolbar | 32 | 44 | +12 (+38 %) | Usage, Try |
| Two-line row (name and secondary) | 60 | People 75, Usage 77 | +15 to +17 (+25 to +28 %) | People, Usage |
| Table row | 40 (36 dense) | Usage model table 77 | about 1.9× | Usage |
| Setting row | 64 | 113 | +49 (1.8×) | Settings |
| Two-line row on phone | 64–72 | People 165 | 2.3× | People |
| Rows per 1000 px (lists) | tables 25, people 16, settings 15 | Usage 13, People 13.4, Settings 9 | about −50 % for tables | Usage, Settings |
| Font sizes in use across the product | 6 (12, 13, 14, 15, 16, 24), at most 4 on one page | 7, with 14 and 15 used for the same role | one role, two sizes | Home, People, Settings, Status |
| Body and row name | 15 | 14 on Home, Usage, Workflows and Settings; 15 on People | −1 (reads as 12.8 Geist) | most |
| Table header, field label | 13 / 500 | 12 / 400 (People th), 14 / 500 (Usage) | −1 or +1, and weight | People, Usage |
| Section heading | 16 / 600 / 24 | 16 on 3 pages, 15 on 2 (Usage line height 15) | −1 on 2 pages | Usage, Settings |
| Heading levels on one page | 2 (title, section) | 3 on Home ("Make something", then "Your usual") | +1 | Home |
| Page padding beside the sidebar | 32 | 48 | +16 (+50 %) | all |
| Settings and form column | 610–720 | 1024 (Settings), 1200 (Try) | +300 to +480 | Settings, Try |
| Section gap | 32, fixed | 0–120 (Workflows 20–120, Home 12–40) | variance, not mean | Home, Workflows, Status |
| Card radius | 8 | 10 | +2 | all cards |
| Card edge | 1 px hairline, no shadow | kit Card with `shadow-sm`; Usage with no container at all | — | Usage, Workflows, Try |
| Coloured pixels outside media | ≤ 1.7 % of the screen, about 5 % of ink | Usage 10.6 % / 70 %; Home 6.1 % / 18 % | 6× on Usage | Usage, Home |
| Chart hues | 1 per series (Vercel green, OpenAI purple) | 6 stacked capability hues | +5 | Usage |
| Always-on cues per media tile | 0–1 (Apple Photos: the duration on video only; Midjourney: none) | up to 4 (day chip, maker mark, type and length badge, play disc); 24 maker marks in one viewport | +3 | Library |
| Media gutter | 2 (Apple Photos) to 8 (Midjourney) | 6 (4 on phone) | in range | Library |
| Status badge | 20 tall, radius 4, 12 / 500, tinted | pill radius full, text 12, 20–24 tall | radius | People, Workflows |
| Content height used at 1440 (Try) | a form and a result filling the view | ends at 517 of 900 px; the right pane is a dashed "Run chat" placeholder | 43 % empty | Try |
