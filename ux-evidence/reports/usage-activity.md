# azizam-ux-usage-activity-20261007

Source-only Usage/Activity UX audit: seven ranked findings, three delight opportunities and strengths to retain. Priorities: explicit unknown costs, useful failure recovery, and current receipt outcomes. Empty diff; tests not run.

Usage and Activity: source-only heuristic UX review
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Verdict: Usage gives a useful monthly overview, but answering “what did this request cost?” and “what should I do about its failure?” still requires too much interpretation. These are source-derived risks, not observed user-test results.

Ranked findings

1. P1 — An unknown price looks like no price.
Usage: web/src/app/usage/page.tsx:780–783, 832–842, 1291–1294. Activity: web/src/app/activity/requests.tsx:439–462.
“As a member, I open a receipt to check whether the failed call cost money, but the Cost field is absent. As an owner, the Activity receipt offers no cost field at all.”
Honest money language is central to trust; absence cannot distinguish a free local request from an unmeasured paid request.
Change: Always show “Provider charge”: “Local · no provider charge” only with recorded local routing evidence; a measured amount with currency when available; otherwise “Cost not recorded · this does not mean free.” Use the same presentation in model totals, Usage receipts and Activity receipts. Do not infer local/free from a missing amount.

2. P1 — Failure explanation stops before the recovery step.
Usage: web/src/app/usage/page.tsx:945–973. Activity: web/src/app/activity/requests.tsx:133–157.
“As a household member, I learn that my request failed and am told to open Technical details. I still cannot tell whether to change my app, wait, or ask the owner.”
This repeats the vision’s specific failure: making users diagnose the boundary between app, gateway and model.
Change: First show “Where it stopped,” “What happened,” and one actionable control based only on recorded evidence. If attribution is absent, say “The receipt does not identify where this failed.” For unknown recovery, offer “Copy help details” with request ID, time and recorded outcome, plus “Share this with the person who runs your gateway.” For recorded retry guidance, expose that guidance plainly without automatically repeating a possibly paid request. Keep raw JSON secondary.

3. P1 — An open receipt can stay stuck on an old outcome.
Activity: web/src/app/activity/requests.tsx:174–183, 204–209, 326–330, 403–437. Usage: web/src/app/usage/page.tsx:173–179, 293–299, 731–767.
“As a builder, I leave an in-progress receipt open and return later. Even if the list refreshes, the drawer can still show the object I opened, leaving me unsure whether the request finished.”
A monitoring surface must distinguish a historical snapshot from current status. Both drawers store the selected record object rather than derive it from refreshed records.
Change: Store the selected request ID and reconcile the drawer against current data. Provide “Check latest outcome,” “Last checked [time],” and a visible refresh error while preserving the previous result. If only a snapshot is available, label it “Recorded outcome at [time]” and say that no newer outcome is available. Never equate accepted with completed.

4. P2 — Finding one troublesome request requires serial browsing.
Activity: web/src/app/activity/requests.tsx:181–195, 216–268, 374–397. Usage: web/src/app/usage/page.tsx:635–720, 987–998.
“As an owner with a request ID from someone’s app, I cannot search for it here; I can only filter by person/source and load more. As a member, I get a recent list without a way to narrow it to the failed call.”
A receipt is useful only if it is easy to retrieve; repeated scrolling spends the user’s attention poorly.
Change: Add “Find a request ID,” an outcome filter (“All outcomes,” “Needs attention,” “Completed,” “Outcome not recorded”), and a date range. Preserve filters and selected receipt in the URL for direct return. Implement retrieval against the full retained history, or label any client-only search “Search loaded requests.” Add a clear boundary: “Showing recent receipts” with a supported path to older ones.

5. P2 — Coverage caveats are hidden behind a complete-sounding month.
Usage: web/src/app/usage/page.tsx:190, 372–376, 404–418.
“As an owner, I read ‘Current month usage’ and ‘This month’ as a full accounting period, while the coverage start or truncation notice is collapsed.”
The page already has useful coverage facts, but users should not need to discover a disclosure to know the scope of the numbers.
Change: Promote active limitations beside the summary: “Receipt coverage: [start]–[end]” and, when truncated, “Some receipts are outside this view.” Explicitly distinguish whether a limitation affects totals, the chart, or only the recent receipt list; do not imply totals are incomplete without that evidence. Keep retention policy details in the disclosure. Add the calendar month and accounting timezone rather than only “Current month.”

6. P2 — Phone users lose the price while long status text competes with request identity.
Usage: web/src/app/usage/page.tsx:672–701. Activity: web/src/app/activity/requests.tsx:337–366.
“As a member on my phone, I cannot scan receipt costs because they are hidden below the small breakpoint. Long outcomes compete with truncated model/source text, so similar requests are harder to distinguish.”
Phone-first membership should preserve the information needed to answer the page’s core questions.
Change: Use two-line mobile cards: capability and time first; outcome plus provider charge second; source and model on a wrapping secondary line. Preserve the same information at every width. Let long outcomes wrap below the title instead of taking a fixed right-hand strip.

7. P2 — Internal executions look like separate user actions.
Activity: web/src/app/activity/requests.tsx:335–341, 439–462.
“As a builder inspecting one workflow, I see ‘Internal execution,’ but the primary details give me no way to jump to its parent. I must inspect JSON to understand which requests belong together.”
A dependable gateway should make its recorded workflow relationships legible without overstating what it knows.
Change: For a recorded parent_request_id, show “Part of request [short ID]” with an open-parent action. Group children under a collapsed “Execution steps (n)” when relationships are available. Do not imply complete workflow coverage or aggregate costs twice.

Three delight opportunities

• A privacy-conscious “Copy help details” preview: one compact, readable incident summary including known cost state and request lineage, explicitly excluding request bodies. Useful to send to the household operator without teaching JSON.
• Tap a day or model in Usage to open its matching receipts, with a breadcrumb such as “October › Oct 7 › Images.” Make the chart an investigation shortcut rather than a dead end.
• A receipt journey: “Requested → Served by → Outcome,” using recorded model/routing facts, elapsed time and charge state. Expand the same strip to show internal steps; mark missing evidence honestly.

Keep these strengths

• Plain monthly summary, five-model progressive disclosure and the optional recent-request list.
• Explicit partial/unknown spend coverage in SummaryRow and spendCoverageDetail; extend this honesty to individual receipts.
• Requested, served and declared-fallback distinctions; copyable request IDs; raw evidence behind Technical details.
• Clear incomplete/accepted/running distinctions, separate refresh versus pagination failures, retry controls, and retained rows on Activity refresh errors.
• Teaching empty states, named filters, keyboard focus styling, non-animated chart bars and the chart’s screen-reader data table.
• Changes and Requests separation; automated/access activity can be disclosed rather than flooding the default feed.

Limits and verification: Read PRODUCT_STRATEGY.md and the three scoped page/request source files only; did not inspect excluded paths or backend contracts. No browser, live API calls, runtime, screenshots, accessibility measurements or user validation. Tests/checks not run. No writes or patches. workspace_result confirms empty diff (0 bytes) at the specified base; checks array empty. Pre-finish state: no cancellation, no authority revocation, no owner notes.
