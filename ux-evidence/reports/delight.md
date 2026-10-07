# azizam-ux-delight-20261007

Design-director brief: five earned moments, six ranked findings, three additional delight opportunities, and strengths to preserve. Evidence-backed, quiet and local-first; empty diff verified; no runtime validation.

Earned delight: confidence, continuity, and permission to stop looking

Design direction: make the result memorable because the user knows what happened, can keep it, and can continue without help. A restrained completion line is earned; a celebration of a copied setting is not. All proposed moments are silent by default, with no automatic sound. Optional motion is nonessential, once-only, and disabled under reduced motion.

Evidence: base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. This synthesizes the completed Home, Apps, Library, Status/Engines, Usage/Activity, Models/Workflows/Try, and IA reviews. Home:730–819,850–1048, Try:1840–1989, and Status:475–574 were additionally inspected directly. References elsewhere are inherited from those source-based reports, not independently re-tested.

Ranked findings and five earned moments

1. P1 — A family member's first use needs a reliable handoff.
Surface: Home and Apps. web/src/app/home/page.tsx:365–368,513–515; web/src/app/apps/page.tsx:485–500,595–683.
“As a family member on my phone, I copied the settings but cannot tell what to do next or whether anything worked.”
The existing generic app guidance and absent connection proof spend attention on uncertainty before the product delivers value.
Proposed moment: a compact “1 Choose app · 2 Add settings · 3 Send a message” strip. Preserve the selected client and non-sensitive guide step across app switching. After step two say “Settings copied. Next, send a short message in [app].” Only after an attributable successful request, replace that instruction with “Your message from [app] worked” and a quiet receipt link. If only receipt arrival is known, say “Request received” instead. Keep “Review setup steps” nearby; never mark a client connected from copying or a gateway-only check. No modal, confetti, or automatic navigation. Existing access behavior remains outside this proposal.

2. P1 — Recovery cannot feel reassuring until the failing layer is known.
Surface: Try failures, receipts, Status. web/src/app/try/page.tsx:245–270,1300–1337,1835–1840; web/src/app/usage/page.tsx:945–973; web/src/app/status/status-client.tsx:410–417,459–465.
“As a member, Retry gives me another attempt, but I still don't know whether my app, gateway, model, or workflow was responsible.”
A decorative success state after an unverified fix would amplify the trust problem.
Proposed moment: preserve submitted input, safe failure context, and the original receipt. Show “Where it stopped: [verified layer]” or “The failing layer could not be identified,” followed by one relevant action. After a deliberate retry succeeds, show “This request worked · [time]” with “View receipt” in the same panel, leaving the earlier failure under “Previous attempt.” Only a fresh capability check justifies “Images available again · verified [time]”; saving configuration does not. Do not auto-retry a possibly paid request. The relief comes from recovered work and evidence, not fanfare.

3. P2 — The first successful request should close the loop.
Surface: Try result and Home gateway check. web/src/app/try/page.tsx:1864–1899; web/src/app/home/page.tsx:490–512,537–565.
“As a first-time builder, I see output, but need to assemble what this proves and how to use the same request elsewhere.”
The product already has real output and verified receipt links; a small completion summary can convert technical success into confidence.
Proposed moment: above the existing output, “Your first successful request” only when account-scoped history establishes that fact; otherwise “[Capability] request completed.” Beneath: “[model] · [elapsed time] · [recorded charge state]” and “Use this request in your app.” The latter reveals the exact successful request with sensitive values omitted, without sending anything. Use “Local · no provider charge” only with routing evidence; otherwise show measured currency or “Cost not recorded.” A static checkmark and one polite screen-reader announcement suffice. Keep the claim scoped to this request, never the whole system or client.

4. P2 — The first image should become something the user can keep.
Surface: Try image output and Library. web/src/app/try/page.tsx:1886–1911,1950–1981; web/src/app/library/page.tsx:742–760,797–817; web/src/app/home/page.tsx:875–914.
“As a family member, I see my image, but want to know where it lives and how to get back to it.”
The image itself is the reward. Ownership and retrieval are more valuable than celebratory UI.
Proposed moment: reserve the image's aspect-ratio area before loading, then reveal the actual image with at most a 150 ms opacity fade, not zoom or sparkle. Under it: “Your image is ready,” with “Download image.” Add “View in your Library” only after persistence and the exact asset ID are verified. Otherwise say “Download a copy to keep it”; do not imply storage. Library should open that item in Mine, not Everyone's. Show provenance under “Made with…” without exposing adjacent household creations. Never announce “first image” without history evidence or “saved on your device” from a download click.

5. P2 — A quiet healthy system should let the owner leave.
Surface: Status summary and Home readiness. web/src/app/status/status-client.tsx:475–574; web/src/app/home/page.tsx:989–992,1022–1042.
“As an owner, ‘All systems normal’ reassures me, but I cannot see how current that evidence is. As a member, Home lacks a plain readiness sentence.”
Trustworthy calm requires bounded certainty, not more dashboard activity.
Proposed moment: with complete current evidence, show a static “No action needed” summary, followed by “All checked services are healthy · observed [time].” Keep service details collapsed and “Check again” visible. Use actual observation time; if absent, say “Observation time unavailable.” Incomplete evidence remains “Some checks are incomplete,” naming them. Home gets factual capability copy such as “Chat is ready. Images start when you use them,” only when those states are known. No celebratory toast, pulsing healthy dot, uptime streak, or repeated announcement on unchanged refreshes.

6. P3 — Remove an existing flourish that does not convey state.
Surface: Home media thumbnails. web/src/app/home/page.tsx:730–760.
“As I move across my creations, each image or video poster grows, although I have not selected it.”
The 1.02 hover scale spends motion on ornament and slightly crops the work without adding information.
Change: remove transition-transform and group-hover scaling. Retain the existing static border change and keyboard focus ring as interaction feedback. The source already respects reduced motion; keep that discipline. Do not remove actual loading feedback or useful media-kind indicators. No broader claim of decorative excess is supported by this bounded inspection.

Three additional delight opportunities

• “Back to your request” from the exact Library item restores inputs and selected model without running again. This makes exploration reversible.
• A reviewed “Copy help details” summary carries receipt ID, verified layer, time, and charge state, excluding prompts and sensitive settings by default. A family member can ask for help without learning JSON.
• “Continue in [app]” appears on return only when a verified configured launch URL exists; otherwise “Reconnect [app]” restores the chosen guide. No invented household app or permanent success badge.

Keep

Real media, the four-item Home limit, manual audio playback, explicit preview failures, lazy loading, large targets, accessible focus and copy feedback, retained results on refresh failure, honest incomplete health states, progressive technical detail, and verified receipt links. These are already the visual and behavioral foundation of a serious product.

Limits and completion

Heuristic design proposals, not observed user validation or measured improvements. No browser, live client/model calls, shell, checks, metered execution, writes, or excluded-path reads. Backend persistence, attribution and first-use history support are unverified prerequisites. Workspace evidence confirms diff_bytes=0, checks=[]. Final get found no cancellation, revoked authority, or owner notes.
