# azizam-ux-providers-deep-20261007

Completed source-only paid-provider UX audit: 10 ranked findings, 3 delight opportunities, and strengths. Read only providers/page.tsx; verified empty diff; no checks or live calls.

Paid-provider UX audit

Coverage: All 2,206 lines of web/src/app/providers/page.tsx at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. File references below all refer to that path. Static UX/copy review only; no imported files, member pages, backend, browser, checks, model APIs, or live provider calls. Actual member-visible behavior and cap enforcement cannot be verified here. No secret-handling evaluation.

Ranked findings

1. P1 — Readiness card: web/src/app/providers/page.tsx:539–565, 684–685, 732–760.
“As the owner, I see Ready and tell my household it works, even when no models or access plans are enabled.” providerState evaluates connection/key/disabled status only; it can label an enabled-or-unknown provider ready without considering usable routes, plans, budgets, or observation age. This repeats the vision’s failure-attribution problem.
Change: label that signal “Connection checked” with its timestamp. Separately show “Available to members,” “Not shared: choose an access plan,” “No models enabled,” or “Availability not verified,” based on authoritative effective-access data. Never infer readiness from connectivity alone.

2. P1 — Cap entry: web/src/app/providers/page.tsx:978–1023, 1376–1379, 1664–1683, 1809–1862, 1981–2025.
“I clear a cap to remove it, but saving sends zero. I cannot tell whether zero blocks requests or removes the limit.” Blank strings become Number("") = 0; text/negative values have no local validation. First-time trust requires explicit monetary semantics.
Change: separate “Set a limit” and “No limit”; use labeled decimal inputs and inline “Enter a non-negative USD amount.” Distinguish blank from zero, validate before Save, and display the server-defined meaning of zero. Show the exact proposed result before saving. Do not silently substitute zero.

3. P1 — Effective plan access: web/src/app/providers/page.tsx:1026–1059, 1685–1721.
“I turn on my family’s plan, but the accompanying text still says ‘Local only by default.’ Will their apps actually get this model?” paidAllowed affects descriptive copy but not switch behavior; editing plans drops even that context. The owner cannot predict the member outcome.
Change: each plan row should show “Can use this provider” or “Blocked by this plan’s paid-access setting,” derived from effective policy. Offer a contextual path to resolve a blocking policy without changing it implicitly. Before Save, summarize named plans, enabled models, and remaining blockers; retain a deliberate “Save without sharing” option.

4. P1 — Missing dependency feedback: web/src/app/providers/page.tsx:1904–1913, 2033–2034, 1443–1497, 1685–1721.
“The provider catalog is blank, or no plans appear. I cannot tell whether my gateway has none or failed to load them.” Only provider-list errors are surfaced; catalog/plan errors become empty arrays. An OSS newcomer has no recovery path.
Change: separate loading, empty, failed, and successful states for both dependencies. Show “Couldn’t load providers. Retry” or “Couldn’t load access plans. Retry”; disable dependent submission while unavailable. Give an explicit “No providers match [search]” reset action.

5. P1 — Budget picture: web/src/app/providers/page.tsx:594–618, 686–702, 1809–1877.
“I see global caps, but not total global spend or which limit is stopping my household. An empty progress bar can mean unmeasured, unlimited, or zero.”
That ambiguity is costly where users expect financial honesty. Global spend fields are mapped but not displayed by GlobalCapsEditor.
Change: show “Today: estimated $X / $Y” and “This month: estimated $X / $Y” across providers; expose which known cap is binding. Use “Spend unavailable” and “No cap set” instead of a zero-percent bar in those states. Show authoritative reset time/time zone and explain what happens at the limit, without promising enforcement this file cannot establish.

6. P2 — Choosing models and prices: web/src/app/providers/page.tsx:1347–1353, 1601–1662.
“I receive a long list of technical IDs with every priced model preselected. Filled prices lose their visible units, so I have to guess what I am enabling.”
The page spends attention on raw inventory rather than a confident choice.
Change: add search, selected count, “Clear selection,” and a “Selected” filter. Display friendly name plus exact model ID; visibly label input/output prices with USD per million tokens even when populated. Explain the default: “Models with listed prices are selected.” Move price overrides behind “Edit estimated prices”; include provenance/freshness when supplied. Validate numeric prices rather than equating nonempty strings with “Priced.”

7. P2 — Wizard progression and completion: web/src/app/providers/page.tsx:1408–1440, 1387–1391, 1733–1775.
“I can jump into empty later steps, then Save closes the dialog without telling me what my family can use.”
Next requires a successful check, but clickable step headers bypass that navigation guard; canSave only checks selection plus an enabled model. This weakens the guided setup story.
Change: gate future steps on prerequisites and show “Check connection first.” End with “Review provider”: models, effective plans, cap meaning, and routing choice. After saving, show “Provider saved” with “Available to [plans]” or the exact remaining blocker and a concrete next action.

8. P2 — Saving edits: web/src/app/providers/page.tsx:653–664, 934–977, 2144–2166.
“I click Save models on one row, but it saves every row. A failure appears far below the card, and there is no local saved confirmation.”
This makes a financial configuration feel fragile. Draft state is initialized from props without visible resynchronization.
Change: one footer per editor: “Save N model changes,” “Cancel,” inline error, and “Saved.” Refresh clean drafts when server data changes; preserve dirty drafts and flag conflicts. Put cap/plan save feedback beside their controls too.

9. P2 — Member handoff and interruptions: web/src/app/providers/page.tsx:839–849, 2043–2046, 2182–2194.
“I know requests must name the provider, but I do not know the exact model string to give someone. Disabling/removing it does not explain their experience.”
Change: show a copyable configured request-model identifier and effective plan next to each enabled model. Before interruption, summarize affected model routes/plans and the known member consequence. Replace generic removal copy with “Remove [provider]? Requests using these models will no longer use this provider,” qualified by actual routing behavior. Do not claim fallback without evidence.

10. P2 — Reconciliation workload: web/src/app/providers/page.tsx:1109–1211, 1870–1875.
“I must match technical IDs and enter each charge manually, but I am not told where to find the matching charge.”
Keep financial honesty while reducing operator work.
Change: lead with provider, human-readable plan, exact timestamp, and estimate; collapse technical IDs under “Matching details.” Add provider-specific billing instructions or a supplied billing link. After recording, show updated estimated/verified totals and remaining unresolved count.

Three delight opportunities

• An owner-side “Preview access for [plan]” panel: exact available models and plain-language blocking reasons, with no paid test call.
• A calm setup receipt: “3 models shared with Family. Daily limit $5; monthly limit $50,” plus copyable request-model names and any unresolved restriction.
• A budget explanation drawer answering “Why can’t this request run?” with the specific plan/provider/global constraint, verified reset time, and one relevant next step.

Keep

Explicit remote routing and no automatic paid access; $5/day and $50/month starting limits; no plans selected by default with a clear warning; unknown-price models initially disabled; unknown spend distinguished from measured zero in money(); estimated-charge disclosures and reconciliation-before-removal; progressive provider details and reachable-check timestamps.

Evidence/limits: Report only. workspace_result verified diff_bytes=0 and empty diff SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855. No checks run. Final state check showed no cancellation, revoked authority, or owner notes. Delivered through bridge finish only; delivery_mode=none.
