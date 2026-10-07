# azizam-ux-docs-vs-ui-20261007

Seven source-backed docs/UI findings, three delight opportunities, preserved strengths and explicit coverage limits. All seven permitted files covered; verified empty diff; no checks or writes.

Docs/UI consistency audit — tnunamak/azizam @ 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

P1 — “Rotate a key” can retire every key for the app.
Surface/evidence: docs/guides/FOR_MEMBERS.md:58–73; web/src/app/apps/page.tsx:179–200,233–255,422–438.
As a member, I follow the guide to rotate one key, but the app gathers every credential ID and revokes all previous keys after confirmation. Any other client sharing this App loses access.
Why: a family-ready product must state the disruption precisely.
Change the guide to: “Apps & keys → [app] → Actions → Rotate keys creates one replacement. Copy it into every client using this app before confirming; confirmation revokes all previous keys for that app. Revoke keys stops every client using that app.” Add the affected key count and this instruction to the rotation dialog, and label its final confirmation “Updated my clients; revoke N old keys.” Preserve the replacement-first sequence.

P1 — Home’s advertised SDK recipes cannot all be selected.
Surface/evidence: README.md:54; docs/guides/FOR_MEMBERS.md:11–12,28–34; web/src/app/home/page.tsx:67–74,236–238,365–373,600–604; contrast web/src/app/apps/page.tsx:49–59.
As a JavaScript or curl user following the Home instructions, I find “Other,” but selecting it sets Python and there is no subsequent client selector anywhere in the complete Home file. I cannot reach my expected recipe.
Why: builders should not translate another SDK’s example to complete onboarding.
Add a labeled “App or SDK” selector under Other, with Python, JavaScript and curl, wired to client state. Change the member step to “Choose your app; for Python, JavaScript or curl, choose Other, then your SDK.” Reuse the selected recipe for the address, key label, snippet and documentation link.

P1 — The first-run guide and entry-point guides prescribe different onboarding paths.
Surface/evidence: docs/FIRST_RUN_SETUP.md:5–14,23–24,110–130,248–260; README.md:30–32; docs/guides/OPERATOR.md:21; Home key creation in web/src/app/home/page.tsx:286–312,474–493.
As a new owner, the README sends me to Home, but the detailed guide tells me to mint the key on the setup page and then administer everything at /admin. I cannot tell which instructions describe the product I installed.
Why: contradictory first-run steps undermine the five-minute promise.
Make the documented primary sequence identical: “Create the owner account. Open Gateway → Engines and connect an existing engine with a model mapping. Open Home, choose your app, create and save its key, copy the displayed address and model, then choose Check connection.” Keep any legacy /setup key flow in a clearly labeled compatibility section only after separate verification.
Limit: this proves contradictory docs and a working source-level Home path, not that the excluded setup route lacks the legacy capability.

P2 — “Gateway check” is not the Home button, and reachability is a different test.
Surface/evidence: README.md:32; docs/guides/FOR_MEMBERS.md:34,52; web/src/app/home/page.tsx:495–517,546–560,588–590; web/src/components/shell/app-shell.tsx:562–578,709–714.
As a member, I look for Gateway check, see Check connection, and may instead find Check gateway reachability in Search. That action only calls /health, so its success does not establish that my model request works.
Why: the product must help separate gateway, model and client failures.
Replace Home-specific guide mentions with: “Choose Check connection on Home. This checks the selected key through the gateway; then send a message from your app to verify its settings.” Put the same explanation beside the Home button instead of only in Setup notes. Explain the Search action separately: “Check gateway reachability only checks that the gateway responds.”

P2 — “Select a key” omits the step required to use it.
Surface/evidence: docs/guides/FOR_MEMBERS.md:30–34; web/src/app/home/page.tsx:378–399,429–455,503–507.
As an invited member, I select the key I saved earlier, but Check connection remains disabled until I paste its full value. The guide also omits the Model field that Home presents.
Why: secure one-time display should not feel like a broken connection wizard.
Replace steps 3–5 with: “Create a key and save it, or select an existing key and paste the saved value. Copy Base URL, Model (when shown) and the key into your app. Choose Check connection, then send a message from your app.” Add: “Selecting a key does not recover its secret; it is shown only when created.”

P2 — Address copying is promised as an ongoing Apps task, but lives in a temporary dialog.
Surface/evidence: docs/guides/FOR_MEMBERS.md:58–68; web/src/app/apps/page.tsx:333–450,253,600–616.
As a returning member, I open Apps & keys to copy the base URL, but existing app cards have only key actions. The copy field appears while a newly minted key is being revealed and disappears after saving.
Why: routine reconnection should not encourage unnecessary key creation.
Add a persistent “Connection settings” action on each app card, opening address, model guidance and a link to Home without minting a key. Until then, change the guide to: “To copy connection settings again, open Home and choose your app.”

P2 — Phone navigation and page names are missing from the guide.
Surface/evidence: docs/guides/FOR_MEMBERS.md:60,84–86,94; docs/guides/OPERATOR.md:79,130; web/src/components/shell/app-shell.tsx:1010–1050,1073–1086; web/src/app/apps/page.tsx:284–293.
As a phone user, instructions name URLs or Gateway without explaining that most pages are behind More. The visible key page is Apps & keys, not simply apps.
Why: household members should navigate by visible controls rather than edit URLs.
Use “Apps & keys” consistently. Add: “On phones, Home, Apps & keys and Status are in the bottom bar. Tap More for Library, Usage and the rest of navigation. Owners find the Gateway section there.” Retain direct URLs as optional links.

Three delight opportunities
1. Home, home/page.tsx:529–568: after success, show a small two-step checklist: “Gateway responded” and “Now send a message from your app,” with View request receipt. Match these exact steps in the member guide.
2. Apps, apps/page.tsx:274–280,600–616: a rotation checklist saying “Replacement copied → Clients updated → Old keys retired,” with actual affected counts and recoverable failure status.
3. Shell, app-shell.tsx:709–714,783–790: add a compact guide tip, “Search → Copy gateway address; desktop shortcut ⌘K / Ctrl+K.” Mention the manual-copy fallback already implemented at 719–725.

Keep
The one-time key warnings; honest distinction between gateway reachability and engine success; optional receipts; per-key model scoping; explicit loopback/device warning in the member guide; README’s tested-versus-untested qualification; safe replacement-before-revocation design; accessible phone navigation.

Evidence and limits
All seven listed files read completely, each call at most 200 lines. No imports followed, excluded paths read, source changes, shell, live requests, checks, Work/Codex/model API or metered tools. No read denied or cancelled. Backend, setup, invitations, navigation destination definitions and recipe implementations were not inspected; their behavior is not asserted. Final get: no cancellation or owner notes. workspace_result: diff_bytes=0; empty-diff SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; checks=[], artifacts=[].
