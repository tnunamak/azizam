# azizam-ux-home-20261007

Source-only Home UX audit: six ranked findings, three delight opportunities, and what to keep. Main issues are invisible readiness, Other hardcoding Python, and default-alias-only recipes. Empty diff verified; no tests run.

Home heuristic UX audit · base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Verdict: Home makes “connect an app” obvious, but does not reliably answer “can I use my AI now?” within five seconds. Members must interpret color or navigate elsewhere; owners receive only a conditional outage summary. This is a source-derived assessment, not a timed user test.

Ranked findings

1. P1 — Readiness is invisible in ordinary text.
Surface: Home capability chips and header. web/src/app/home/page.tsx:653–671, 989–992, 1022–1042.
Member experience: “I see Chat and Images, but cannot tell whether either works. On my phone, a gray chip gives me no explanation.”
The chip’s state is tint plus an unavailable-only title/accessible label; there is no visible Ready/Starting/Busy/Unavailable label or reason. Home’s summary is restricted to owners and unavailable state. This undermines honest health and the one-question-first principle.
Change: put a factual capability summary above Connect, such as “Chat is ready. Images are starting.” Give each chip a visible status and expand-on-tap reason: “Images · Starting” / “Starting. Try again shortly.” For unknown observations, say “Availability could not be checked” rather than imply ready. Keep owner diagnostics behind “View Status”; give members plain next steps.

2. P1 — “Other” silently means Python.
Surface: App picker. web/src/app/home/page.tsx:66–73, 365–368; web/src/app/home/recipes.ts:3–14, 157–166.
Member experience: “My phone app is not listed, so I choose Other. Now I am offered a key for Python and developer instructions.”
Only six named choices plus Other render; Other sets client to Python, with no subsequent client selector in this component. Phone, JavaScript and curl recipes exist but cannot be selected here. This blocks a central household use case and narrows the supposedly unopinionated product.
Change: make Other open “Which app are you connecting?” with Phone app, Python, JavaScript, curl and generic OpenAI-compatible options. Generic copy: “In your app, choose an OpenAI-compatible provider, then paste these settings.” Offer a configured household app link separately when one is actually available; do not assume Open WebUI exists.

3. P1 — A usable non-default chat model cannot populate a recipe.
Surface: Model selection and unavailable-key explanation. web/src/app/home/recipes.ts:22–31; web/src/app/home/page.tsx:521–535.
Builder experience: “My key can use a chat model, but Home says it does not include the default model and tells me to ask the owner.”
chatAlias returns only the alias named default, even when other scoped chat aliases exist. Without default, the UI omits model settings and the recipe code. This turns an optional naming convention into a setup dependency.
Change: prefer an available default, otherwise offer permitted chat aliases with explicit availability and local/paid labels. Require an explicit selection for a paid alternative. If none exist, say “This key has no chat models. Choose another key or ask the owner for chat access.” Never silently substitute a paid route.

4. P2 — Essential instructions are mislabeled as optional notes.
Surface: Connection settings and sheet. web/src/app/home/page.tsx:375–382, 513–515, 581–604; web/src/app/home/recipes.ts:87–120.
Member/builder experience: “I have a Base URL, model and key, but do not know where they go. ‘Setup notes’ sounds like extra information I can skip.”
The recipe’s actual instructions/config are behind a sheet described as “Version notes and network caveats”; the main fields also discard client-specific labels such as “Override OpenAI Base URL.” This spends attention on guesswork.
Change: show the selected recipe description inline, retain exact application field labels, and label the disclosure “Show [app] setup instructions.” Present three steps: “1. Open [app] settings”, “2. Paste these values”, “3. Check the gateway, then send a message in [app].” Leave version/network caveats in a secondary disclosure.

5. P2 — Connection checking obscures failure ownership.
Surface: Check action, error and success context. web/src/app/home/page.tsx:315–332, 490–512, 537–565, 587–590.
Member experience: “A failed check tells me to check my key even when the request could not finish. A successful check does not tell me, beside the result, whether my chosen app works.”
The generic catch attributes recovery to the key; the accurate explanation that this does not verify the app is buried in Setup notes. This repeats the ambiguity the product vision explicitly calls out.
Change: rename the action “Check gateway with this key.” Show inline: “Checks the gateway and key; it cannot test [app].” On an unclassified error use “We couldn’t complete the gateway check. Your key may still be valid. Try again.” Preserve structured failure categories when available, with a next action for key/access versus model availability versus network. Before any chargeable test, show the selected model, Paid badge and known price/estimate; this review does not establish whether the current backend test incurs cost.

6. P2 — Audio cards stop short of the Library detail.
Surface: Recent creations. web/src/app/home/page.tsx:875–914.
Member experience: “I can play my audio here, but unlike an image or video I cannot open that exact creation to download it or inspect details.”
Audio returns an article without the asset link used for other media; the section-level Library link makes users find it again. That breaks continuity between making and keeping work.
Change: keep the separate 44 px play button and add “Open audio in Library” linking to /library/?asset=<asset_id> on every audio card, including unavailable previews. Use the prompt as its accessible context.

Three delight opportunities

• A truthful handoff receipt after a successful check: “Your key works · 240 ms” plus “Next: send ‘Hello’ in [app].” Mark the app connected only after real observed evidence, never after copying.
• A returning-member shortcut above the full picker: “Continue in [household app]” when a verified configured launch URL exists; otherwise “Reconnect [last selected app]” with settings ready to copy. Keep other choices one tap away.
• A compact “Your last creation” card with playable audio and direct detail/download access, beside one relevant next action. Preserve the calm four-item limit instead of turning Home into a feed.

Keep

• The clear Connect an app heading, app-specific key naming, large app-choice targets and responsive grids.
• Honest “Gateway responded with this key” wording, measured latency and direct request-receipt link.
• CopyField’s manual-copy fallback and screen-reader confirmation (web/src/components/product/copy-field.tsx:72–80).
• Real media previews, explicit preview failures, partial-results notice and reduced-motion hover handling.

Scope and verification

Read PRODUCT_STRATEGY, Home/root page, Home presentation/API/recipes and selected permitted shared UI helpers. Excluded paths were not read. No live browser, user validation, backend verification, tests or repository checks were run. No writes were made. workspace_result confirms diff_bytes=0 and checks=[] at the requested base. Cancellation and owner notes were checked immediately before completion: not cancelled; no owner notes.
