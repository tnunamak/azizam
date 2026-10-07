# azizam-ux-copy-owner-20261007

Partial source-only owner microcopy review: 12 ranked exact-copy rewrites, 22-term glossary, three delight opportunities, and retained strengths. Services verification blocked by Guardian; empty diff verified.

Owner-facing copy review
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Source-only; no rendered or runtime validation. Partial delivery: Services source verification was blocked by Guardian and that path was stopped without retry.

Ranked findings and rewrite table
Paths below are relative to web/src/app/. Current entries quote source strings; brackets in proposed copy are runtime substitutions. Each row includes the owner experience and product rationale.

Priority | Surface / file:line | Current | Proposed | Why

P1 | Engines, engines/page.tsx:600–607 | “Engines unavailable”; “Retry” | “Couldn’t load engines”; “Reload engines” | “I think my engines have failed, but the page actually failed to load their configuration.” Distinguish a failed read from engine health so the owner investigates the right layer. Keep the underlying safe error detail secondary.

P1 | Providers, providers/page.tsx:1338–1343 | “Connection worked.” (fallback; missing ok defaults to true) | Explicit success: “Provider connection verified.” Missing success evidence: “Connection result not confirmed. Check again.” | “I receive success without an explicit successful result.” Copy requires a behavior change: do not default missing ok to success. A trustworthy product reports uncertainty rather than inventing proof; say what the check verified, without claiming model execution.

P1 | Status, status/status-client.tsx:503–521 | “System status partially checked”; “All systems normal” | “Some health checks are incomplete”; “No issues found in the latest health check” | “I cannot tell whether everything was tested or the screen is reassuring me too broadly.” Name missing checks below the headline and show the actual observation time when known. The narrower success claim protects trust without adding alarm.

P2 | Status, status/status-client.tsx:507–511,568–574 | “1 problem needs you”; “[n] problems need you”; “Open” | “1 issue needs attention”; “[n] issues need attention”; “Check [engine name]” / “Review [provider name]” | “I’m summoned without knowing what clicking will do.” Calm, specific language reduces incident friction. Count the complete actionable set before claiming a total; carry the selected target through the link.

P2 | Engines, engines/engine-checks.tsx:96–99,170–184 | “[name]: connection passed”; “Test”; “Detect” | “[name]: connection verified”; “Test connection”; “Detect models” | “Does Test run a model, and what will Detect find?” Preserve the existing reachability-versus-model distinction. Follow success with “The gateway reached this engine. Send a model request to check that it can produce a result.” Keep address and HTTP status in details.

P2 | Engines, engines/page.tsx:653–658 | “Selected engine not found”; “No service record has id [id].” | “Selected engine not found”; “No engine matches ID [id]. Choose another engine.” | “Why did an engine turn into a service record?” Use one visible noun; provide an engine-list action. Keep internal service identifiers and API names unchanged.

P2 | Providers, providers/page.tsx:778–799,1111–1137 | “Reconcile charges”; “Reconcile provider charges”; “Loading reconciliation rows…”; “Refresh reconciliation” | “Verify charges”; “Record verified provider charges”; “Loading charges…”; “Reload charges” | “I’m not an accountant; I need to know what I’m recording.” Keep the existing instruction to obtain verified provider amounts and the warning that estimates are not final bills. This records evidence; never imply payment or automatic bill matching.

P2 | Providers, providers/page.tsx:172–179,189–194 | “Not measured yet”; “<$0.01”; “$[amount]” | “Amount not recorded”; “Less than USD 0.01”; “USD [amount]” | “Does a missing amount mean zero, and which dollar is this?” Use currency consistently in summaries and precise-charge fields. Preserve exact recorded precision for verification; do not round small positive charges to zero or invent a future measurement promise.

P2 | Activity, activity/requests.tsx:146–153 | “Open Technical details for the recorded status and warnings.” | When no recovery evidence exists: “No recovery guidance was recorded. View the status and warnings in Technical details.” Add “Copy request ID.” | “I’m sent to JSON as if it explains what to do.” Put any recorded, safe recovery instruction first. Explicitly acknowledge absent guidance instead of implying that technical details resolve the problem.

P2 | Workflows, workflows/page.tsx:333–341,439–445 | “Run in Try”; “Engine binding”; “No engine binding configured” | “Open in Try”; “Assigned engine”; “No engine assigned” | “Will this click execute a request, or just open another page?” Match the navigation-only action exactly. Plain configuration labels let owners understand requirements without learning implementation terms.

P2 | Showcase, showcase/page.tsx:184–188 | “Recorded examples could not be loaded. Check your connection and try again.” | “Couldn’t load the recorded examples. Try again.” | “I’m told my connection is wrong even when the example data may be invalid.” The same branch includes failed responses and invalid data. Avoid unsupported blame; expose a safe specific reason only when available.

P3 | Showcase, showcase/page.tsx:238–240 | “[n] seconds” | “Recorded duration: [n] seconds” | “Is this past runtime or a promise for my next request?” Preserve the measured value and unit, with a label that matches historical evidence.

Proposed glossary: 22 canonical terms
1. Gateway: the Azizam instance routing requests.
2. Engine: a configured local service; keep “service” internal.
3. Provider: a configured remote AI service.
4. Model: the selected AI model.
5. Model name: the exact name to paste into an app.
6. Alias: an alternative accepted model name.
7. Workflow: a configured process with required inputs and outputs.
8. Capability: what the system can do, such as Images or Speech.
9. Access plan: rules governing who can use what.
10. Request: one submitted operation.
11. Receipt: recorded evidence about a request, potentially incomplete.
12. Request ID: the exact identifier for finding a receipt.
13. Outcome: what happened to that request.
14. Ready: available according to the applicable observation.
15. Starting: preparing to become available.
16. Busy: occupied; explain whether waiting is supported.
17. Unavailable: cannot currently be used; include a reason.
18. Not checked: an evidence qualifier, not a fifth operational state.
19. Provider charge: recorded or explicitly unknown remote cost.
20. Estimated charge: a provisional amount, never a final bill.
21. Spend cap: the configured spending limit, with period and currency.
22. Recorded example: a past output, not proof of current availability.

Three delight opportunities
• After an engine check: “Gateway reached → Engine reached → Model not tested”, with “Try a model” and an explicit cost disclosure before execution.
• A receipt’s “Copy help details” previews request ID, recorded outcome, known failure layer and charge state, excluding prompts and private configuration by default.
• A charge-verification completion line: “Verified charge recorded: USD [exact amount]”, linking the matched request and retaining estimate-versus-verified history.

Keep
The strategy’s four operational states, specific unknown/incomplete handling, existing distinction between engine reachability and model proof, provider estimated-charge caveats and visible remote-routing explanation. Keep workflow identifiers copyable, technical details progressively disclosed, and Showcase’s explicit warning that past examples do not establish current availability.

Evidence and limits
Read strategy and bounded permitted source excerpts; reused terminal Status/Engines, Usage/Activity, IA/navigation and Models/Workflows/Try reports as context. Services read was denied as outside confirmed scope despite its inclusion in the brief; no retry, indirect read or verification claim. No excluded-path reads, patches, shell, live calls, model APIs, metered tools or tests/checks. Final get: no cancellation, revocation or owner notes. workspace_result: empty diff, 0 bytes; checks=[].

