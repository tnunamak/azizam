# azizam-ux-status-member-20261007

Completed bounded static member-health review of all three allowed files: 5 ranked findings, 3 delight opportunities, and keep list. Verified empty diff; no checks or extra files.

Member health review — static evidence only

Scope and evidence
Reviewed all of web/src/app/status/page.tsx (22 lines), web/src/app/status/status-view.tsx (283), and web/src/app/home/presentation.ts (258), at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. All reads were at most 200 lines. No other files, imports, directories, live calls, screenshots, patches, or checks were used. Shared copy helpers, API contracts, and Home rendering were not inspected; findings about their behavior are explicitly bounded below.

Ranked findings

1. P1 — Status: a monitoring failure sends the member toward their own connection.
web/src/app/status/status-view.tsx:163–179
Member experience: “The page says to check my connection, so I start changing my app even though the service might be the problem.”
Why it matters: Every non-401 query error receives the same connection-oriented instruction, including server errors. This repeats the product’s central failure-attribution problem and can spend a household member’s attention on the wrong repair.
Change: Use “We couldn’t check AI availability. This doesn’t tell us whether AI requests are working.” Label the existing retry action “Check again”. If a previous snapshot exists, retain it with “Last known status” and its check time rather than replacing all capability information. Keep the explicit sign-in recovery for 401.

2. P1 — Status: freshness has no explicit place beside the headline.
web/src/app/status/status-view.tsx:128–138, 180–186
Member experience: “I see a health badge, but I can’t tell when it was checked or whether it means my next request will work.”
Why it matters: The page polls every 30 seconds and passes state/copy into a pill, but does not itself render a successful-check time or an observation-versus-guarantee explanation. Trustworthy family-facing status needs that distinction. The unread statusSummaryCopy helper may add context; its exact output is unverified.
Change: Give the top summary three explicit lines: the current capability-availability answer, “Status updated [relative time]”, and “Availability can change. A test request checks whether AI can respond now.” Track successful snapshot retrieval separately from any backend observation time; label each honestly. A refresh must not advance “last successful” time until it succeeds.

3. P2 — Home: the summary silently drops health states outside its five-name allowlist.
web/src/app/home/presentation.ts:158–187
Member experience: “Some tasks appear in my account but disappear from the sentence explaining what works.” If every tile has an unrecognized/missing status, the function returns an empty string.
Why it matters: The empty-account fallback only covers zero tiles. Unknown state should produce uncertainty, not silence or a falsely complete account summary. This is a defensive rendering gap; the permitted files do not establish whether current API responses can trigger it.
Change: Preserve the recognized groups, collect every remaining tile, and append “[Tasks]: status not available”. If no state is understood, return “We can’t confirm availability for your tasks yet.” Never map unknown to ready. Lead with available tasks, then a short exception sentence, rather than an increasingly long dot-separated inventory.

4. P2 — Status: receipts make the member translate implementation language.
web/src/app/status/status-view.tsx:243–258; web/src/app/home/presentation.ts:190–231
Member experience: “The history tells me an operation code and status value; I still don’t know whether that was my chat or what happened.”
Why it matters: Status concatenates raw receipt.intent and receipt.status. Home already contains a human-request-label approach, so the two surfaces can impose different literacy requirements during the same troubleshooting journey.
Change: Render a human type and outcome, for example “Chat request · Completed” or “Image request · Failed”, with time beneath. Share the mapping approach already demonstrated by requestLabel; do not guess a failure’s cause from an opaque status. Put raw intent and request ID in receipt detail. Give each link a meaningful accessible name such as “View failed image request from [time]” instead of an opaque ID.

5. P2 — Status: connection setup is the strongest troubleshooting action, before the member sees affected tasks.
web/src/app/status/status-view.tsx:187–210
Member experience: “Before I know what is unavailable, the page nudges me to connection settings and asks me to choose among three routes.”
Why it matters: The primary filled button targets Home, while the request test is secondary, and this help block precedes the capability list. On a phone, support navigation consumes attention before the page answers which task works. The Home landing content itself is outside the allowed rendering scope.
Change: Order the page as summary → task availability → optional help → receipts. Rename “Your capabilities” to “What you can do”. Make help contextual: for ready tasks, offer “Test AI here” followed by “Check app settings”; for an unavailable task, show the supported reason and owner-help instruction next to that task. Use “Open Home for connection settings” unless a verified direct settings anchor exists.

Three delight opportunities

• A small “What still works” sentence during partial trouble: “Chat is ready. Images are temporarily unavailable.” Preserve the user’s options without a large incident dashboard.
• After an intentional test, offer “Compare with my app”: show the test’s timestamp and result and explain that a successful test only verifies that tested path. This gives the member a concrete next step without claiming their app is at fault.
• Offer a user-initiated “Copy help summary” containing task, time, observed state and receipt reference, excluding prompts and credentials. Preview the summary before copying so asking the household owner for help feels safe and easy.

Keep

• Progressive disclosure for technical model-use names, with a keyboard-focusable, minimum-height Details control (status-view.tsx:105–122).
• The explicit warning that a past receipt does not establish present availability (232–235).
• Account-specific capabilities and receipts, the clear owner-help empty state, and the dedicated expired-sign-in recovery.
• Automatic refresh, a disabled-during-fetch refresh button, loading feedback, wrap-safe content, and a narrow-screen single-column capability list.
• Home’s distinction between idle (“ready to start”) and ready, its human request-label function, and representative generation-alias selection instead of incidental workflows.

Verification and limits
workspace_result reports diff_bytes: 0, an empty diff preview, and no check receipts. No runtime or visual behavior was verified. Pre-finish get reports no cancellation, no authority revocation, no stopped compute, and no owner notes. An interrupted finish was reconciled with get: it remained claimed with no terminal report, so only report submission was retried.
