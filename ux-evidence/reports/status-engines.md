# azizam-ux-status-engines-20261007

Completed static Status/Engines/Services audit: 7 ranked findings, 3 delight opportunities, retained strengths, and evidence limits. Empty diff verified; no live validation or checks.

Static UX audit: Status, Engines, Services
Verdict: Status has a calm, evidence-aware foundation, but owners cannot consistently identify the failing layer or reach the right recovery step. Engines organizes configuration well but does not answer “Is everything fine?” first.

Evidence: tnunamak/azizam base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba; product strategy and scoped route source. Findings below are source-level heuristics, not reproduced runtime defects. Services re-exports Engines (web/src/app/services/page.tsx:1), so Engines findings apply there too.

Ranked findings

1. P1 — Status attributes gateway/API trouble to hardware.
Source: web/src/app/status/status-client.tsx:410–417,459–465,577–581.
As an owner, I can see “Hardware needs attention” when the underlying reason came from the API signal. If a disk warning also exists, “Free disk space” can appear beside that unrelated API reason.
Why: Wrong attribution sends a household operator toward unnecessary machine maintenance and undermines trust.
Change: Preserve signal source through rendering. Use separate rows: “Gateway needs attention”, “Storage needs attention”, or “Engine unavailable”; attach each remedy only to its own signal. For an unclassified signal use “Health check needs attention” with “Cause not identified”. Never infer a hardware cause from the mere presence of a warning.

2. P1 — The problem count can conceal simultaneous failures.
Source: web/src/app/status/status-client.tsx:399–417,450–466,504–521.
As an owner with two failed providers and several host warnings, I get the first provider and one selected warning, yet the headline gives a definite count of “problems”.
Why: A count that silently omits actionable conditions makes incident resolution feel complete before it is.
Change: Build rows from every actionable provider and signal, deduplicate by source/reason, then count that complete set. Headline: “3 issues need attention · 1 check incomplete”. Show the highest-impact three initially with “Show all 5 issues”. Keep incomplete checks separate from actual failures.

3. P1 — Verification stops between engine reachability and a working model/app.
Source: web/src/app/engines/engine-checks.tsx:87–110,151–161,170–184; web/src/app/status/status-view.tsx:181–200,230–260.
As an owner, “connection passed” correctly says it did not test a model, but my next step is prose. As a member, I receive three generic help destinations rather than a diagnosis of this failed request.
Why: The original gateway-versus-model-versus-client confusion remains unresolved.
Change: Make “Test connection” and “Detect models” explicit labels. After a successful connection, offer “Try a model” carrying the selected engine/public model into Try, with any cost shown before execution. Present evidence steps: “Gateway reached”, “Engine reached”, “Model request succeeded”, and “App not tested”, each time-stamped and independently unknown until verified. On a failed receipt, show the known failure layer, request ID, and a specific next action; say “Cause not identified” when evidence is insufficient. A successful model test must never declare the client healthy.

4. P2 — Incident links lose the engine the owner was trying to fix.
Source: web/src/app/status/status-client.tsx:219–234,570–575; web/src/app/engines/page.tsx:401–420,541–578.
As an owner, I read “2 engines affected”, click “Open”, then have to rediscover their names in another screen.
Why: The UI spends my attention twice during recovery despite already knowing the affected IDs.
Change: Show affected engine names beneath each grouped reason. Give each a “Check [engine name]” link to the existing service query parameter; open the corresponding diagnostic sheet, without automatically running a request. Replace generic “Open” with “Review provider” or “Check engine”. Preserve grouped capability-level explanations.

5. P2 — Engines hides recovery risk and lacks an at-a-glance answer.
Source: web/src/app/engines/page.tsx:612–637,548–555,690–711; web/src/app/engines/engine-groups.ts:62–68,84–104.
As an owner, I see a list without a health summary; Ready labels disappear, and an unavailable standby can remain behind “Standby and fallback engines”. Enabled but unconfigured engines are also hidden by default.
Why: Calm disclosure should hide detail, not conditions requiring action.
Change: Add “4 engines ready · 1 unavailable · 1 not checked” above the list, with observation time. Automatically expose actionable enabled engines, including alternatives, while keeping intentionally disabled engines collapsed. Show a reason line for non-ready states. Collapsed groups should say “2 standby engines · 1 unavailable”. Retain compact capability grouping.

6. P2 — Status offers an uncertain conclusion without freshness or a visible remedy.
Source: web/src/app/status/status-client.tsx:349–354,482–521,543–561,592–599.
As an owner, “System status partially checked” does not tell me which check is missing. Refresh is hidden in an overflow menu and the latest observation has no visible age.
Why: “What is true now” requires distinguishing current evidence, stale evidence, and missing evidence.
Change: Below the headline show “Observed 20 seconds ago” when the source provides observation time; otherwise “Observation time unavailable”. Name missing checks: “Engine health unavailable; provider check complete”. Put “Check again” beside this notice and the headline. Do not present browser-fetch time as measurement time. Preserve existing refusal to render all-normal when checks are incomplete.

7. P2 — Saving connection changes breaks the test-and-recover flow.
Source: web/src/app/engines/page.tsx:237–249,286–309,749–755.
As an owner editing a broken endpoint, I must save before testing; saving closes the sheet, so I reopen it to find the test.
Why: The most common recovery sequence is interrupted precisely when users need confidence.
Change: After connection edits, label the primary action “Save and continue testing”; keep the sheet open after persistence, focus “Test connection”, and show “Configuration saved. Connection not tested yet.” Run tests only after a deliberate test click. Ordinary name/note saves can retain the existing close behavior.

Three delight opportunities
• A compact incident breadcrumb: “Your app → Gateway → Engine → Model”, showing only verified observations, timestamps, and an honest unknown state. Selecting a step reveals its evidence and next action.
• An opt-in, redacted “Copy diagnostic summary” containing the selected request ID, public model name, failure layer, and observation time. Preview it first; exclude keys, full configuration, and private endpoint details by default.
• After recovery, a quiet factual line: “Images available again · verified 14:32”, with the successful receipt and elapsed interruption time when known. No celebratory animation or inferred recovery from a save.

Keep
• Strategy-aligned incomplete/unknown handling; no unconditional green state.
• Capability-first names, collapsed healthy details, mobile-sized controls, and focus restoration.
• Explicit distinction between reachability and model proof; preview-before-apply for detected models; existing-access warning and confirmation.
• Member-facing capabilities and the warning that historical receipts do not establish current availability.
• Services compatibility via the shared Engines implementation.

Limits and completion: No screenshots, browser interaction, live requests, tests/checks, shell, writes, or excluded-path reads. Shared helpers/backend contracts were not inspected, so runtime data quality and contrast are unverified. Final assignment check showed no cancellation/revocation and no owner notes. Workspace receipt confirms an empty diff (0 bytes) and no check runs.
