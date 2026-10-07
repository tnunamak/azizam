# azizam-ux-engines-deep-20261007

Completed bounded static engine UX audit: 8 ranked findings, 3 delight opportunities, preserved strengths; all four permitted files covered, empty diff verified, no checks or live calls.

Engine setup and recovery UX audit

Evidence and limits
Reviewed all 1,332 lines of the four permitted files at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. References below use paths under web/src/app/engines/. Static review only: no imported components, backend implementation, excluded paths, browser, live requests, or checks were inspected/executed. Add-dialog internals and shared-component accessibility are unverified. The workspace receipt confirms diff_bytes: 0 and no checks. Final assignment state had no cancellation or owner notes.

Ranked findings

1. P1 — Verification stops before the question owners actually need answered.
Surface: Test / applied models; engine-checks.tsx:84–105, 149–158, 162–189.
“I passed Test and applied my models, but I still cannot tell whether my family's app will work.” The code correctly describes reachability, yet ends with an instruction to send a request elsewhere, leaving gateway-versus-model-versus-client isolation unfinished.
Change: label the button “Check connection”; show separate stages “Gateway can reach engine”, “Model names saved”, “Model response not checked”. After applying, offer an explicit “Verify a model response” next step using a selected public name, explaining any request cost before execution. Report the failing stage independently; never promote connection success to model readiness. A real model-test implementation requires work beyond these files.

2. P1 — HTTP-only serialization blocks unrelated edits to an HTTPS-backed editable engine.
Surface: Edit engine; page.tsx:113–135, 170–189, 240–248.
“My engine already has an HTTPS address, but changing its name, note, or enabled switch cannot be saved.” Every editable save reparses the endpoint and rejects HTTPS, even when the endpoint was unchanged. This conditional path is a concrete task blocker, not evidence that such engines currently exist.
Change: patch only changed fields; preserve an unchanged endpoint representation. Until HTTPS editing is supported, display “This endpoint is read-only here. You can still edit the name, note, and enabled state.” Do not silently substitute HTTP or discard the existing endpoint.

3. P1 — Recovery targets can disappear, and the empty-state action sends owners toward duplication.
Surface: List / disabled and incomplete engines; engine-groups.ts:62–68; page.tsx:623–635, 660–677.
“I disabled an engine and now cannot find it. When nothing is in use, the large action tells me to add another.” Disabled engines and engines explicitly reported unconfigured are hidden by default; this also hides setup work needing attention. That makes recovery harder precisely when trust is fragile.
Change: separate “Needs setup” from “Disabled”. Keep enabled-but-unconfigured engines visible in “Needs attention”; keep a just-disabled row visible until the owner leaves, with “Disabled · Enable again”. For the existing-but-hidden empty state, use “Show N disabled engines” as the primary action and “Add engine” as secondary.

4. P2 — Add and save terminate the setup flow instead of advancing it.
Surface: Add completion / editor checks; page.tsx:283–297, 730–753.
“I add an engine, get a toast, then have to find it and reopen it to continue. If I repair the address, Save closes the editor before I can retry.” This consumes attention without delivering a verified outcome.
Change: after add, select and open the new engine with “Engine added. Check its connection next.” After connection changes, provide “Save and check connection” and a secondary “Save and close”; keep the editor open for the result. Keep testing disabled against unsaved endpoint/model changes.

5. P2 — Model preview asks for decisions without showing the actual proposed changes.
Surface: Detect / Apply; engine-checks.tsx:118–125, 220–270, 310–319; detected-models.ts:55–88.
“I see every discovered model selected, including ones already mapped. I only discover duplicate or invalid public names after Apply.” Existing mappings are protected, but validation and outcome classification arrive too late for a confident review.
Change: compare the preview with current mappings and mark each row “Already configured”, “New public name”, or “Name conflict”. Display “Adds 2 names · Keeps 5 existing mappings”; disable submission for inline errors and focus the first failing name. Use “Apply 2 new names” and “Nothing new to apply” rather than claiming an unchanged configuration gained models. Retain the fresh-snapshot conflict check at submission.

6. P2 — Managed engines expose recovery controls that cannot complete.
Surface: Managed engine checks; page.tsx:304–309, 329–339; engine-checks.tsx:207, 229–305.
“I am offered Edit endpoint, but there is no editable endpoint field. Detection lets me rename and confirm models before telling me they cannot be applied.” The layout promises a repair path it does not provide.
Change: for managed engines, show “Managed by the gateway” above checks; make detection a read-only inventory without editable names or the access acknowledgment. Replace “Edit endpoint” with an accurate instruction such as “Update this engine in its deployment configuration, then retry.” Do not invent a destination.

7. P2 — Visual fallback nesting can imply an unverified routing relationship.
Surface: Standby and fallback grouping; engine-groups.ts:85–103; page.tsx:691–710.
“I see a fallback nested under a primary and assume it will rescue that engine.” Without an explicit parent, the UI chooses the first enabled primary/required engine in the capability group; these files do not establish that routing does the same.
Change: nest only when an explicit relationship is known. Otherwise use a capability-level “Other standby and fallback engines” section with “Routing relationship not shown”. Mark role and priority separately from observed health.

8. P3 — Menu actions lose their stated intent.
Surface: Engine overflow menu; page.tsx:552–561.
“I choose Detect models and arrive in the same generic editor as Edit engine, with another choice to make.” Change openEditor to accept an intent, focus the relevant check, and title the sheet “Check connection” or “Detect models”. Use full button labels instead of “Test” and “Detect”; do not automatically issue requests just because the menu opened.

Three delight opportunities

• A compact setup receipt: “Connected → 3 names saved → Model response verified”, with timestamps and an explicit “Not checked” stage. It gives an owner something understandable to trust.
• A before/after model-name preview: show “App sends family-chat → engine receives model-x”, alongside “Existing routing kept”. This turns alias configuration into a concrete mental model.
• A calm recovery strip pinned to the selected engine: last failed stage, one next action, and the result of the retry. Preserve it across sheet close/reopen and distinguish outdated observations from new checks.

Already good; preserve

• Connection success explicitly says it proves reachability only (engine-checks.tsx:96–99).
• Applying detection fetches a fresh registry and pins its write to that ETag; it does not blindly overwrite concurrent changes (engine-checks.tsx:32–59).
• Append-only model merging protects aliases and policies and refuses conflicting names (detected-models.ts:38–90).
• Preview makes possible immediate access under existing rules explicit, and requires acknowledgment (engine-checks.tsx:273–295).
• Unsaved connection changes disable checks; editor close restores invoking focus (page.tsx:263–266, 283–289).
• Status-read failure has its own retry banner, distinct from registry failure; a selected hidden engine is still discoverable (page.tsx:593–607, 638–649; engine-groups.ts:65–68).
