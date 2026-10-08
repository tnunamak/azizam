# Benchmark board: notes

Companion to `BOARD.md`. This file records what was read, what failed, where sources disagree, and why each product was chosen. Captures were made 2026-10-08 UTC (the evening of 2026-10-07 US time).

## Method

- Context read first: `~/Documents/azizam/vision-from-logs.md` (personas, recurring themes, delight and cognitive cost, Oct 7 ambition), `~/Documents/azizam/ux-playbook.md` §1 (roles and rules) and §6 (gates G0–G6).
- Capture tool: `capture.cjs` in this folder, adapted from `../references/capture.cjs`. It launches a private headless Chromium and makes a fresh context per shot, with no profile and no login. Modes:
  - `live`: 1440 × 900 desktop and 390 × 844 phone, both DPR 1.
  - `image`: an official image at native size, capped at 2400 px wide.
  - `elem`: one element screenshot.
  - `list`: discovery only. It scrolls to load lazy images and prints their URLs.
- Each capture appends its URL, final URL, HTTP status, title and date to `capture-log/<surface>.jsonl`. Four files were made by a helper script or derived by hand, so they have no log line:
  - `keys/vercel-aigw-key-and-recipe.webp`: live, https://vercel.com/docs/ai-gateway/getting-started, scrolled about 1700 px.
  - `empty-error/openrouter-chat-empty-{desktop,phone}.webp`: live, https://openrouter.ai/chat.
  - `people/clerk-members-crop.webp`: a crop of `people/clerk-organization-profile-desktop.webp` (830 × 408 at +305+492), cut out to isolate the component from the marketing hero.
- The work ran in two rounds of parallel capture workers, each covering 3 or 4 surfaces. The director then spot-checked 17 images against the traits the workers claimed, and the captures matched. One capture was replaced: Clerk was cropped, because a scrolled recapture showed the component cycling through tabs.
- Every kept image was viewed after capture. Captures were deleted if they showed a login wall, a cookie wall, a blank page, an illustration, or something too small to judge.

## Sources read (divan; no research redone)

- `product-design/opus-5-5-ux-quality-comes-from-references-…`: reference-first process; Mobbin/Refero as precedent sources; the "gauntlet" blind comparison against a named real equivalent. This board feeds that comparison.
- `product-design/delight-attention/company-practice.md`: public design principles of Apple, Stripe, Linear, Vercel, Plaid and GOV.UK. This is why those brands anchor the board.
- `product-design/delight-attention/*` (hub, one-primary-action, microcopy, visual-hierarchy, progressive-disclosure): these shaped the trait types (one primary action, word budgets, verdict first).
- `product-design/things3-partitions-time-into-named-zones-…`: Things 3 has two Apple Design Awards, calm through colour restraint, and metadata on demand. Used for 13a.
- `product-design/multi-object-admin-consoles-feel-like-one-product-…`: Stripe Home, Vercel deployments, Tailscale Machines, Raycast, Plaid Portal. Used to pick products for the owner surfaces.
- `product-design/operator-dashboards-favor-density-with-purpose-and-token-elevation.md`: tabular numbers, right-aligned figures, density, Linear's redesign. Used for the Usage, Catalog and Engines traits.
- `product-design/stripe-lets-you-re-reveal-only-keys-it-generated-…` and `auto-generated-owner-credentials-need-an-explicit-reveal-moment-…`: the reveal-once rules. Used for the Keys traits.

## Why each product (and over what)

| Surface | Chosen | Over | Reason |
|---|---|---|---|
| Home | Apple Home (iPhone), Stripe Dashboard iOS | Home Assistant demo, Tailscale, Vercel dashboard | Apple Home has the same household audience and a status summary first. Stripe answers "how is today" in numbers with "+" one tap away. Home Assistant is on the old board and is the wrong league. Tailscale and Vercel have no public home-screen pixels. |
| Status | Linear status (incident.io), incident.io incident, Apple System Status | GitHub, OpenAI, Vercel, Tailscale status (old board) | incident.io pages are the current best-designed status product and show affected vs unaffected users and fault origin. Apple is the consumer phone verdict. The old pages repeat "operational" and never say who is affected. |
| Library | Midjourney Explore, Suno song detail, Apple Photos and Music | Krea, Google Photos, Plex (old board) | Midjourney is the reference generated-image grid and is public. Suno is the only public generated-item detail with provenance. Krea and Google Photos showed only marketing heroes. |
| Try | Replicate playground and run, Vercel AI SDK playground, OpenRouter chat | fal.ai, Anthropic Workbench, OpenAI Playground | Replicate's schema-driven form is the pattern others copy. Vercel is a named bar. fal was blocked by a pop-up. The Anthropic and OpenAI consoles need login and their docs have no screenshots. |
| Catalog | Vercel AI Gateway models, OpenRouter models and detail | Replicate explore, Raycast Store | Both are the same category as Azizam. Replicate explore is marketing-led. The Raycast Store search could not be driven by URL. |
| Keys | Cloudflare token created, Resend keys, Vercel AI Gateway recipe, Tailscale download | Stripe, OpenAI, Clerk, GitHub | Cloudflare is the only public pixels of a shown-once secret with a test command. Stripe's docs images are 512 px icons. The GitHub PAT image was a 720 × 129 crop. |
| Usage | Vercel AI Gateway observability, Stripe Workbench, Resend log, OpenRouter activity | Stripe payments, Mercury, Anthropic and OpenAI usage | Same job (requests, cost per key) in a gateway. Workbench is Stripe's request receipt. The Stripe payment-detail docs had no images. |
| Engines | Raycast AI providers, OpenRouter providers table, Vercel Spend Management | Cloudflare AI Gateway, Vercel BYOK, Tailscale Machines, Supabase auth providers | Raycast mixes local backends, subscriptions and keys exactly as Azizam does. The other docs have no screenshots, or only an og-image. |
| People | Clerk OrganizationProfile, Vercel invite, Linear permissions | Tailscale users, 1Password Families, Apple Family Sharing, Notion | Clerk is a real live component with members, invitations and inline roles. Tailscale's KB has no static images. 1Password returned 403. Apple showed the inviter side only. |
| Settings | Linear settings, Raycast settings | iOS Settings, Stripe, Vercel | Linear's settings layout is widely copied. Apple's pages show only hero art. Stripe's docs images are icons. |
| Sign-in / invite / first-run | Linear and Vercel login, Discord invite, Home Assistant first-run (floor) | Clerk components, Resend login, Tailscale welcome | Linear and Vercel are named bars and are live. Discord is the only public invitee view found. Home Assistant is used only as the minimum to match. |
| Empty / error | Stripe 404, Vercel build error, Geist rules, OpenRouter empty and no-results | Linear and Vercel 404, GitHub, Resend, Next.js 404 | The Linear and Vercel 404 pages redirect to login. GitHub and Resend use an illustration with one button. Next.js has no exit. |
| Phone | Things 3, Stripe iOS, plus live phone captures | Linear mobile | The linear.app/mobile images are tilted, hand-held photos and are unusable for traits. |

Evidence of best-in-class status is recorded where we checked it: Things 3's Apple Design Awards (divan entry above), Linear's dedicated redesign essay (divan operator-dashboards entry), and Vercel's Geist and Stripe's principles (divan company-practice). For Raycast, Resend, Clerk, Midjourney and incident.io, the claim rests on general designer consensus. We did not verify it with a cited award in this pass. Treat it as **inferred**.

## Dead ends

**Accounts or login walls**
- Mobbin MCP (OAuth not authorised in this session).
- Anthropic Console Workbench and OpenAI Playground (login; their docs pages have no UI images).
- The Linear and Vercel 404 URLs (they redirect to login).
- Midjourney job pages (no public job hrefs found).

**Blocked or broken captures**
- support.1password.com family-invite returned 403.
- The fal.ai "What's new" pop-up persisted even after `--click` on `aria-label="Dismiss announcement"`.
- The Suno phone view showed a "Get the full app" wall.
- The Discord invite phone render was blank.
- The Raycast Store `?query=` parameter was ignored.
- The Linear docs `?search=` parameter was ignored.

**No usable product pixels**

| Group | Pages tried | What we found |
|---|---|---|
| Stripe docs | keys, dashboard/basics, payment-intents, receipts, refunds | 512 px icons and diagrams only |
| Vercel docs | ai-gateway overview, BYOK, authentication | No screenshots |
| Vercel docs | dashboard-features | 404 |
| Cloudflare AI Gateway docs | 6 pages | og.png only |
| Tailscale KB | 1085, 1017, 1371, machines, ACLs, device-tags | No static images. App Store images are JS placeholders. |
| Others | Supabase auth and Connect docs; Clerk invitations and social-connections docs; Resend docs for emails, domains, webhooks and onboarding | No images, or images 575 px wide and too small |
| Apple | macos/photos, iOS Settings | Hero art only |
| Apple | renovated_iphone | A black frame |
| Marketing heroes | Google Photos, Krea, Replicate explore | Hero or promo only, no product UI |
| Tailscale homepage | "status popup" | An SVG illustration |

**Rejected after capture**
- Linear onboarding: the start-guide image is a video title card.
- Linear invite docs: show the admin side, not the invitee.
- Tailscale start and welcome: generic sign-in pages.
- Geist empty-state below the fold: placeholder "Title" demo cards.
- Linear docs inbox: populated, not empty.

## Disagreements and caveats

- **Old board vs this board on Home Assistant.** The old board uses Home Assistant as a Home reference (R01). This board treats it as the wrong league, uses it only as the first-run floor (11d), and lists its dashboard as an anti-benchmark. Tim's brief decides this; it is not a judgement of Home Assistant's usefulness.
- **Replicate on phone.** It is the best desktop playground but fails the phone gate: the run action is below the fold. It is kept with an explicit anti-trait.
- **Density vs calm.** Vercel, OpenRouter and Stripe Workbench are dense owner tools. Apple Home, Things and Apple Status are calm member tools. Judges should apply the owner benchmarks (Usage, Engines, People) to owner screens, and the calm ones (Home, Library, Status on phone, Sign-in) to member screens. Tim's audience split makes this deliberate.
- **Reveal-once.** Stripe's rule ("user-created keys are shown once") and 1Password's practice (a Secret Key retrievable from three places) differ. Cloudflare's pixels follow Stripe. The right choice depends on whether Azizam generated the key. See the two divan entries above.
- **Docs screenshots are staged.** They use seeded data and are sometimes framed in a browser mock-up (Cloudflare) or composited (App Store shots). Traits are about composition and copy, not pixel sizes.
- **AI-generated content in Midjourney and Suno.** The grids show third-party user content. Judge the layout, not the images.

## Gaps still open (product pixels not found)

1. A desktop member home that combines "what you can do" with "connect your first client".
2. Owner per-backend health with an error state and a fix (Tailscale Machines would be ideal).
3. A per-request receipt that shows cost.
4. A playground that shows cost and latency after a run.
5. A mixed-media library.
6. A best-in-class first-run setup, and an invite acceptance from a B2B product on phone.
7. A real first-run empty state in a flagship product, and an error that names the failing layer.

For 2, 3 and 7, Azizam has to set the bar itself (playbook G4). If Mobbin is authorised, gaps 1, 6 and 7 are the first searches to run.

## v2 retirements (2026-10-08)

Two blind Opus judges flagged these as unusable for the bar. Files moved to `_retired/<surface>/`, not deleted.

- `engines/openrouter-model-providers-phone`: public model page squeezed to phone; not an owner console.
- `catalog/vercel-ai-gateway-models-{desktop,phone}`: public marketing page, not the dashboard.
- `catalog/openrouter-model-detail-{desktop,phone}`: public marketing-style model page with promo clutter.
- `try/replicate-flux-schnell-{desktop,phone}`: public model marketing page.
- `try/replicate-prediction-{desktop,phone}`: public share page of a run, not the logged-in app.
- `settings/linear-administration`: docs image with annotation overlay and redaction.
- `people/clerk-members-crop`, `people/clerk-organization-profile-desktop`: marketing component demo on a marketing hero.
- `keys/tailscale-download-{desktop,phone}`: a download page, a different job.
- `keys/cloudflare-token-complete`: clipped curl command and orange docs frame.
- `keys/vercel-aigw-key-and-recipe`: a docs page, not UI.
- `usage/vercel-ai-gateway-projects`, `usage/vercel-ai-gateway-apikeys`: redaction bars over the main table.
- `library/apple-photos-ios-viewer`: device frame.
- `library/suno-explore-desktop`, `library/suno-song-detail-{desktop,phone}`: public discovery and share pages, not a member's library.

### v2 replacements and method

- Three parallel capture workers searched official docs and support pages for real app screenshots; the director viewed every kept image and recropped three (Apple Photos Info panel and viewer to drop annotation leader lines; Raycast Manage Models to drop a half-visible row). Crops are logged as `"mode":"recrop"` lines.
- Added: `library/apple-photos-ios-library-phone`, `library/apple-photos-mac-viewer-desktop`, `library/apple-photos-mac-info-desktop` (Apple Support); `try/raycast-ai-chat-desktop`, `catalog/raycast-manage-models-desktop` (Raycast manual); `engines/huggingface-provider-list-desktop` (HF docs); `keys/tailscale-oauth-client-desktop`, `settings/tailscale-device-management-desktop`, `people/tailscale-invite-roles-desktop` (Tailscale KB); `people/apple-family-sharing-phone` (Apple Support); `usage/vercel-runtime-logs-desktop` (Vercel docs).
- Rejected in v2: Resend team settings (page dimmed behind a menu spotlight), Hugging Face playground (empty, no run), Vercel O11y tab (faded and cut off), Linear team-access crop, Vercel project-role form (placeholder only), Apple iOS selection grid (device frame), Apple Mac library grid (818 px).
- No screenshots served (v2): Vercel AI Gateway docs (overview, models, BYOK, auth, getting started), Vercel usage/pricing pages, Cloudflare AI Gateway and Workers AI docs, Anthropic platform docs, OpenAI playground docs (404), Replicate docs, Supabase API keys/logs, Stripe keys/teams/meters, GitHub token pages (720 px slivers), Clerk organization docs, 1Password support, Midjourney docs (Cloudflare challenge).

### v2 open gaps

- Keys: no real-app "connect a named client" screen; the shown-once dialog (Tailscale) is only 608 px wide.
- Try: no logged-in run with time and cost; no phone playground.
- Catalog: no in-app list with price and speed together.
- Usage: no unredacted per-key spend table; no request log in a failing state other than Stripe Workbench.
- Library: no logged-in generated-media library with prompt and model on the item.
- People: no desktop members list showing pending invites.
