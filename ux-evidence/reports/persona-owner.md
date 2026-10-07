# azizam-ux-persona-owner-20261007

Completed source-based owner day-one/day-thirty journey: six ranked cross-surface findings, three delight opportunities, retained strengths and precise coverage. Empty diff; no execution or user-validation claims.

Owner/operator journey: day one and day thirty

Method: Source-based heuristic walkthrough at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba, not an observed user session. Reuses completed Status/Engines/Services, Usage/Activity, and Models/Workflows/Try reports; adds direct reading of the full operator guide and bounded Providers source. This synthesis prioritizes handoffs rather than repeating every surface finding.

Day one
“I connect an existing local engine using the guide’s endpoint, health check and model mapping steps. The connection test distinguishes reachability from model execution, but I must stitch together Models, Try and an external app to prove that my family can actually use it. For a paid provider, the staged setup makes models, caps and allowed plans explicit. Saving closes the dialog without a next-step verification journey. I have configured a gateway, but have no single evidence trail that tells me what is ready to share.”

Day thirty
“My family member says ‘it doesn’t work.’ Status can tell me about current component health, but that is not the history of their failed request. I need its ID, yet Activity offers browsing rather than direct lookup; Try failures can lose the receipt context; workflow steps and provider charge records do not carry me to the parent request. If the request may have cost money, Usage and Providers use different detail surfaces and unknown cost can disappear. The guide asks me to choose a component-specific troubleshooting branch before I know which component failed.”

Ranked findings

1. P1 — No end-to-end incident path from “it doesn’t work.”
Sources: web/src/app/activity/requests.tsx:133–157,181–195,216–268; web/src/app/try/page.tsx:245–270,1300–1337; docs/guides/OPERATOR.md:227–267.
“As the operator, I must guess whether to investigate the app, gateway, engine, model or workflow before the product helps me narrow it down.”
Why: This is the exact family-support burden the product vision promises to remove.
Change: Introduce “Investigate a request” accepting an ID, or person/app/time when the ID is unavailable. Preserve verified failure response context in Try. The incident view should show “Where it stopped,” “What the gateway recorded,” and one next action. Show App → Gateway → Engine/provider → Model/workflow with each step independently observed, failed or unknown. Missing receipt means “No matching receipt found,” not “Your app failed.” Start guide troubleshooting with this symptom-first path; never infer the failing layer from an HTTP code alone.

2. P1 — Day-one setup has no bounded proof of readiness.
Sources: docs/guides/OPERATOR.md:51–70,78–84,218–219; web/src/app/engines/engine-checks.tsx:87–110,151–184; web/src/app/providers/page.tsx:1384–1387; web/src/app/models/models-client.tsx:379–432,564–643.
“I can save and check a connection, but still have to discover how to verify a model and the family’s actual app.”
Why: A confident operator needs evidence of the supported journey, not reassurance from configuration success.
Change: After save, show “Configuration saved. Model request not tested.” Offer “Choose a model to test,” preserving engine/provider, public model name and capability into Try. Show location and known price before a deliberate request. After success: “This model request worked. Your external app has not been tested.” Offer the exact model name and a representative app-check checklist. Keep the operator guide’s post-upgrade app request requirement and reuse it for first-run readiness.

3. P1 — Current health can misdirect a request investigation.
Sources: web/src/app/status/status-client.tsx:399–417,450–466,570–581; web/src/app/engines/page.tsx:401–420,541–578.
“I follow an issue to Engines and lose which engine I was fixing; an API signal can appear as hardware trouble.”
Why: Misattribution wastes attention and can prompt unnecessary changes during an incident.
Change: Preserve source and affected IDs. Label “Gateway needs attention,” “Storage needs attention,” or “Engine unavailable” only when supported. Link “Check [engine]” to the selected engine sheet without running anything. Beside an incident’s historical outcome show “Current health, checked [time]” separately; current reachability cannot establish what failed earlier. Count all actionable issues and distinguish incomplete checks.

4. P1 — Spend cannot be followed reliably from a failed request to its charge.
Sources: web/src/app/usage/page.tsx:780–783,832–842; web/src/app/activity/requests.tsx:439–462; web/src/app/providers/page.tsx:1113–1118,1163–1170,1204–1211.
“I may see no receipt price, then find a provider reconciliation entry whose request ID is plain text. I cannot easily establish whether these refer to the same failed attempt.”
Why: Household trust requires an honest answer to ‘Did that cost us money?’ before retrying.
Change: Use a common “Provider charge” row: recorded amount; “Estimate, awaiting verified charge”; or “Cost not recorded; this does not mean free.” Say “Local · no provider charge” only with routing evidence. Add verified reciprocal links between receipt and reconciliation record. Keep estimates distinct from bills and require verified actual charges; do not sum workflow children twice or suggest retries are free.

5. P2 — Workflow troubleshooting loses lineage and current outcome.
Sources: web/src/app/activity/requests.tsx:174–183,204–209,335–341,403–462; web/src/app/usage/page.tsx:173–179,293–299,731–767.
“I find an internal execution but cannot jump to the family’s parent request; an open receipt may remain on its original running state.”
Why: Long-running workflows make apparently contradictory status especially costly to explain.
Change: Store selected request ID and reconcile against refreshed data. Add “Check latest outcome,” last-checked time and explicit stale-data errors. Show “Part of request [ID]” and collapsed “Execution steps” from recorded lineage only. Label accepted, running and completed distinctly; never claim a whole workflow succeeded from one child.

6. P2 — Day-thirty money review hides scope where the owner first looks.
Sources: web/src/app/usage/page.tsx:190,372–376,404–418; web/src/app/providers/page.tsx:687–698,722–726,781–790.
“I see ‘This month’ and a provider spend bar before discovering coverage limits or estimated-charge caveats deeper down.”
Why: Calm progressive disclosure should hide technical detail, not the qualifications that change a financial interpretation.
Change: Place calendar month/timezone and applicable coverage beside Usage totals. Put “Includes estimated charges” beside the collapsed provider amount whenever reconciliation is pending, with “Review [n] charges.” State exactly which totals or lists the coverage limitation affects.

Three delight opportunities
• A private, previewable “Help my household” summary: request ID, app, time, known failure layer and charge state; omit prompts, keys and private endpoints by default.
• A quiet recovery comparison: “Previously failed at engine connection; model test succeeded at 14:32.” Keep app verification separate and never infer recovery from saving.
• A saved investigation link carrying the request, parent/child relationship and selected engine, so returning tomorrow resumes the same evidence rather than another search.

Keep
• Honest unknown/partial states, reachability-versus-model distinction, requested-versus-served evidence, and raw details behind disclosure.
• Provider setup’s model prices, caps and explicit plan selection; no-plan warning; estimates distinguished from verified bills.
• Capability-first catalogs, editable test inputs, existing Workflow→Try handoff, and no automatic execution on navigation.
• Operator guide’s Docker reachability advice, safe restart, backup cautions and representative post-upgrade app verification.

Coverage and completion
Direct reads: OPERATOR.md:1–284; providers/page.tsx:1–200,601–1400,1601–1800. Other UI evidence is attributed to the three completed reports above, not independently re-audited. Strategy read was interrupted; owner explicitly said to skip it and continue, which was followed without retry. No excluded paths read, browser, live calls, model APIs, shell, writes, checks or user validation. Rendering, backend behavior and actual support outcomes remain unverified. Final state: active authorization, no cancellation, owner note addressed. Workspace evidence: empty diff, 0 bytes, no checks.
