# azizam-ux-apps-20261007

Source-only Apps UX audit: two P1 connection-guidance/proof gaps, four P2 usability findings, three delights, and strengths. Empty diff; no tests or live calls.

Apps connection UX review
Snapshot: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Source-only heuristic, not a rendered or live-client test.

Verdict: A household member who already understands OpenAI-compatible settings may succeed. The flow does not yet give a non-technical phone user enough guidance or evidence to connect confidently without help. It explains creating access more completely than using an app.

Ranked findings
Paths below beginning page.tsx refer to web/src/app/apps/page.tsx.

1. P1 — Client selection promises a recipe but delivers generic instructions.
Surface/source: client picker, page.tsx:49–58, 485–500; connection instructions, 606–660.
User experience: “I chose Open WebUI, but I still have to work out where its settings are and what ‘OpenAI-compatible API’ means. Expanding ‘Connection examples’ gives me code, not steps for my app.”
Why it matters: The main household task remains dependent on technical help. The strategy promises exact client settings and calm, phone-first guidance.
Change: Render client-specific, version-verified numbered instructions after selection, with the exact destination field names beside each copy action. Header: “Set up Open WebUI”; helper: “Keep this page open while you switch to your app.” Include a supported model selection step where that client needs one, populated from available models rather than a guessed default. For apps without a maintained guide, say “General setup instructions” and “In your app’s connection settings, look for an OpenAI-compatible connection.” Rename the disclosure “Developer examples” and keep it secondary. Do not claim exact client menu paths until verified.

2. P1 — There is no proof that the connection works or explanation of where a failure lies.
Surface/source: connection panel, page.tsx:595–683; app summary, 356–365.
User experience: “I have copied settings, but I don’t know whether my app can actually use them. If it fails, I don’t know whether to change my app settings or ask the household owner.”
Why it matters: This is the precise trust problem in the brief; creating access is not evidence of a working app.
Change: Add a “Check gateway” action with explicit scope: “Checks this gateway and the selected model. It does not test your app.” Before a real request, show the model and local/no-provider-charge or known paid cost. Show separate, evidence-backed results for gateway reachability and model response; do not label the app connected from this check. Next instruction: “Send a short message in your app.” Once an actual request attributable to that app is observed, show “First request received” with time and receipt. If none is observed, say “No request received yet”; offer “Review setup steps.” Map verified failures to one next action, without inferring the cause from a generic exception.

3. P2 — Returning users cannot readily reopen their setup guide.
Surface/source: app card and menu, page.tsx:351–451; connection guidance exists only inside the reveal dialog, 595–683.
User experience: “After switching between my browser and app, I want to check the address or instructions again. My app card gives me key actions instead of a way back to setup.”
Why it matters: Phone setup is an interruption-heavy task; users should not have to recreate access simply to recover instructions.
Change: Add a visible “Setup instructions” action to each card that reopens the client guide and gateway address independently of key operations. Keep the selected client as non-sensitive setup metadata. Use “Continue setup” when no request has been observed, without implying this proves a failed connection. Preserve existing disclosure and credential behavior; this finding concerns navigation only.

4. P2 — The first decision uses the product’s vocabulary instead of the user’s.
Surface/source: page header/empty state, page.tsx:284–328; creation form, 465–500, 520–528.
User experience: “‘New app’ sounds like I’m making or installing an app. ‘App name’ asks for ‘My laptop,’ then ‘App recipe’ asks which app I meant.”
Why it matters: Two overlapping naming decisions consume attention before the user gets useful instructions; the default “Other app” hides the value of named guides.
Change: Primary and empty-state CTA: “Connect an app.” First field: “Which app are you connecting?” with named choices and “Another app.” Put SDKs and curl in a separate “Code or developer tool” group. Second field: “Name this connection,” prefilled from the chosen client, with helper “For example, ‘Chat on my phone.’” Submit: “Get setup instructions.” Keep a factual sentence explaining that this also creates access. Do not require users to understand “recipe” or “gateway” to start.

5. P2 — A missing last-used timestamp leaves a new connection’s state unexplained.
Surface/source: page.tsx:331–365.
User experience: “My new app appears with a key count, but there is no message telling me whether I still have something to do.”
Why it matters: The page’s opening question should be whether an app has been used successfully, not how many technical objects it owns.
Change: When no request is recorded, show “No requests received yet” and the “Continue setup” action. When activity exists, show “Last request received · [time]”; use a separate, verified result if distinguishing success from failure. Keep counts and the access-plan name in secondary details. Avoid a green ‘Connected’ badge based solely on key existence or historical activity.

6. P2 — The creation dialog is not given the phone-height safeguards used by the next dialog.
Surface/source: page.tsx:462–473 versus 592–593; web/src/components/ui/dialog.tsx:63–67.
User experience, inferred from source: “Opening setup immediately brings up my keyboard; on a short phone screen, the later fields or action may be hard to reach.”
Why it matters: The principal household surface must survive keyboard-reduced height, not only fit a desktop viewport.
Change: Give the creation dialog the same dynamic-viewport maximum height and vertical scrolling as the connection dialog. Prefer the app chooser as the initial step and focus the name only when needed. Keep the primary action reachable while scrolling. Verify at narrow widths, landscape, large text, and with the software keyboard; this is a source-based risk, not a reproduced overflow.

Three delight opportunities
• A compact, resumable “1 Choose app · 2 Add settings · 3 Try it” guide that remembers the non-sensitive step across app switching.
• A genuinely earned “Your first message arrived” moment, with the chosen app name and a discreet receipt link. No confetti or success claim before evidence.
• A “Need help?” summary users can review and copy: selected client, guide step, model and verified check result, with sensitive values omitted. Make it useful to the household owner without turning the member into a debugger.

Keep
• Friendly app names, wrapping card titles, and contextual accessible action labels (page.tsx:331–391).
• Clear loading, retry and empty states rather than blank panels (294–328), plus the explicit partial-creation explanation and retry path (502–527).
• Developer examples behind a disclosure and language preference sorting (612–660).
• Real clipboard feedback, screen-reader announcements and manual-copy recovery in web/src/components/product/copy-field.tsx:34–82.
• The connection dialog’s dynamic-height scrolling (page.tsx:592–593), 44-pixel button targets and visible keyboard focus styles (web/src/components/ui/button.tsx:7–27).

Limits and completion
Reviewed only the permitted Apps source, product strategy and relevant shared UI. No excluded-path reads, key-handling audit, browser rendering, live gateway/client/model calls, shell commands, mutations or checks. Tests not run. Workspace receipt confirms diff_bytes: 0 and checks: []. Cancellation and owner notes checked immediately before submission: no cancellation, revoked authority, or owner notes.
