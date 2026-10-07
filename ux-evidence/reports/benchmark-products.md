# azizam-ux-benchmark-products-20261007

Completed comparative UX review: four ranked surfaces, three adoptable patterns per surface, patterns to avoid, three delights and strengths. Official-source comparisons; 1,123 words. Empty diff; no checks or live validation.

Comparative UX review
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Recommendation: adopt recognizable task patterns rather than dashboard density. Home should answer “Can I use it?”, Usage/Activity “What happened and what did it cost?”, Status “What needs attention?”, Library “Where is what I made?”

Ranked findings and patterns

1. P1 — Usage/Activity: make every request an inspectable receipt.
Source: web/src/app/usage/page.tsx:780–783,945–973; web/src/app/activity/requests.tsx:133–157,174–209,439–462.
“As a member, a missing cost field leaves me wondering whether a failed request was free. Technical details still do not tell me what to do.”
This directly undermines the promised honesty about cost and failure ownership.

Adopt three patterns:
• Stripe associates each API call with a request identifier, includes it in individual Dashboard log URLs, and asks for it during support. Make Azizam’s request ID a first-class destination: “Find a request ID”, a copyable receipt link, and the same receipt component in both surfaces. [Stripe request IDs](https://docs.stripe.com/api/request_ids)
• OpenRouter’s Activity overview connects spend and requests to model/app breakdowns. Azizam should connect its month/model summary to the corresponding receipts. Always show “Provider charge”: a recorded amount, verified “Local · no provider charge”, or “Cost not recorded · this does not mean free.” Keep charge visible on phones. [OpenRouter Activity](https://openrouter.ai/blog/announcements/activity-dashboard/)
• Linear preserves main filters in the URL. Preserve date, outcome and selected receipt so an investigation can be resumed; label client-only retrieval “Search loaded requests” if full-history retrieval is unavailable. [Linear filters](https://linear.app/docs/filters)

Avoid importing OpenRouter’s many analytical dimensions or a developer-log-first layout into the household default. Lead each failure with “Where it stopped”, “What happened” and one evidence-based next step; put JSON behind Technical details. Reconcile open receipts by ID with refreshed data, showing “Last checked…” rather than retaining an unlabeled stale snapshot.

2. P1 — Status: separate observed layers before simplifying them.
Source: web/src/app/status/status-client.tsx:399–417,450–466,504–521,577–599.
“As an owner, an API problem can be labeled hardware trouble, while a definite issue count omits other failures. I can fix the wrong thing and still think I am finished.”
Premium trust requires correct attribution before reassuring presentation.

Adopt three patterns:
• Tailscale distinguishes currently connected devices from last-seen/time-based filtering. Give each capability an explicit observation state and age; “Not checked” and “Observed 20 seconds ago” must remain distinct. Do not substitute page-fetch time for measurement time. [Tailscale device filters](https://tailscale.com/docs/features/access-control/device-management/how-to/filter)
• Linear’s My Issues prioritizes actionable work. Build Status from every actionable signal, show the highest-impact items first, and count the complete deduplicated set: “3 issues need attention · 1 check incomplete”. Keep healthy infrastructure collapsed. [Linear My Issues](https://linear.app/docs/my-issues)
• Vercel associates deployment state, URL and logs with a concrete deployment. Keep Azizam’s evidence and remedy attached to the affected object: “Check Kitchen engine”, opening that engine’s diagnostics, rather than generic “Open”. [Vercel project overview](https://vercel.com/docs/projects)

Avoid a single global green light or an infrastructure inventory as the member’s default. Preserve separate “Gateway”, “Engine” and “Model request” observations, with “App not tested” where applicable. Render “Gateway needs attention” for API evidence; never recommend disk cleanup for an unrelated API reason.

3. P1 — Home: show usable capabilities before setup mechanics.
Source: web/src/app/home/page.tsx:365–368,513–515,653–671,989–992,1022–1042.
“As a household member, I see Chat and Images without visible readiness. Choosing Other sends me into Python setup.”
A family member should not need infrastructure knowledge to decide what to do next.

Adopt three patterns:
• Home Assistant combines at-a-glance state with direct controls and supports simpler guest views. Show “Chat is ready. Images are starting.” above Connect, then one relevant action per capability; show unknown observations explicitly. [Home Assistant dashboards](https://www.home-assistant.io/dashboards)
• Vercel puts the current production deployment and its usable URL together. Pair the ready capability with “Open [configured household app]” only when an actual configured launch URL exists; otherwise “Connect an app”. Do not assume Open WebUI is installed. [Vercel deployments](https://vercel.com/docs/deployments/overview)
• Linear’s My Issues demonstrates a personal, focused entry view. Offer “Reconnect [last selected app]” while keeping alternatives one tap away, rather than repeating the full setup picker as the only entry path. [Linear My Issues](https://linear.app/docs/my-issues)

Avoid borrowing a configurable dashboard builder, a full model marketplace, or developer syntax as the household starting point. Make Other open Phone app / Python / JavaScript / curl / OpenAI-compatible app choices. Rename the hidden disclosure “Show [app] setup instructions”, retain the app’s exact field labels, and label the test “Check gateway with this key”. State inline that it does not test the chosen app.

4. P1 — Library: keep personal scope stable; borrow media navigation, not media sprawl.
Source: web/src/app/library/page.tsx:742–760,797–817,967–1006,1154–1212; P2 provenance at 259–264,629–636.
“As the owner, I can open Everyone’s media when I meant to show my own. On my phone, searching and downloading take more navigation than enjoying the creation.”
This is a privacy-expectation and attention problem, not evidence of an access-control bypass.

Adopt three patterns:
• Linear’s distinct personal view suggests a predictable “Mine” default for every role. Put the broader owner view behind an explicit “Gateway media” switch with “Showing everyone’s media”. Clearing content filters must never broaden scope.
• Plex exposes library filters, sorting and media playback as browsing tools. Keep prompt search and media-kind chips above Azizam’s grid on phones; collapse only secondary filters. Preserve the selected scope and show “Updating results…” during filter changes. [Plex Library view](https://support.plex.tv/articles/200392126-using-the-library-view/)
• Borrow the media-first browsing principle: preview first, then a sticky “Download” / “Copy prompt” row, then expandable provenance. Display the recorded model ID when its friendly name is unavailable. Full prompt text appears once.

Avoid importing Plex’s many recommended/library/timeline/album destinations into this small personal collection. Keep one Library. Do not label a workflow download “Open in ComfyUI”; use “Download workflow” and “Import this file into ComfyUI.” Failed previews should say “We couldn’t load this preview. Your file may still be available”, with independent download/retry actions when supported.

Three delight opportunities

• A calm handoff receipt: “Your key works · 240 ms. Next: send Hello in [app].” Never infer app success from copying settings.
• A previewable “Copy help details” summary containing request ID, known failure layer, time and charge state; exclude bodies and private configuration by default.
• A personal Favourites shelf and full-screen Showcase mode, with no adjacent users’ creations or technical metadata until requested.

Keep

Preserve capability-first wording, calm collapsed details, copy fallbacks, real previews, manual audio playback, receipt deep links, explicit partial/unknown coverage, and the distinction between reachability and model proof. These already fit the desired product better than adding more charts.

Evidence and limits

Comparative recommendations are design judgments, not observed user results. Reused completed Home, Usage/Activity, Status/Engines and Library reports at the same revision; directly spot-checked allowed source ranges on all four surfaces. Public comparisons were verified using official documentation only, without accounts or live-product interaction. No excluded-path reads, shell, writes, patches, checks, model API calls or runtime tests. workspace_result confirms diff_bytes=0 and checks=[]. Final get shows no cancellation/revocation and no owner notes.
