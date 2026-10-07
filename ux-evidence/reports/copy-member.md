# azizam-ux-copy-member-20261007

Source-only member microcopy report with prioritized exact rewrites across seven surfaces, terminology inventory, three delights and strengths. Empty diff verified; no checks or live calls.

Member-facing copy review
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Source-only; no rendered or user-tested claims.

Verdict: the strongest copy names an action and limits its claim. The weakest asks members to understand infrastructure before connecting, creating or recovering. Adopt second-person guidance, verb-plus-object buttons, and evidence-qualified success. Keep exact external-app field names alongside plain explanations.

Paths: H = web/src/app/home/page.tsx; A = web/src/app/apps/page.tsx; T = web/src/app/try/page.tsx; L = web/src/app/library/page.tsx; U = web/src/app/usage/page.tsx; M = web/src/app/models/models-client.tsx; G = web/src/app/gallery/page.tsx.

Ranked findings and rewrite table
Each row gives file:line | exact current copy | proposed copy | why, including the member experience.

P1 — Checks assign blame or imply broader proof.
H:328 | “The gateway check could not finish. Check your key and try again.” | “We couldn’t complete the gateway check. Your key may still be valid. Try again.” | “I’m told to replace a key without knowing what failed.” Avoid unsupported blame.
H:517 | “Check connection” | “Check gateway with this key” | “I think this checks my app.” Put “Checks the gateway and key. It cannot test your app.” beside the action, rather than only in Setup notes. This directly addresses the owner’s trust problem.

P1 — Failure recovery requires technical interpretation.
U:965 | “Open Technical details for the recorded status and warnings.” | “For help, share this request ID with the person who runs your gateway. Technical details may contain more information.” | “I know it failed, but not what to do.” Add “Copy help details” with a preview excluding prompts and private values. Prefer a specific recovery action when recorded evidence supports one.

P2 — Setup names internal objects rather than the member’s task.
A:465 | “New app” | “Connect an app” | “Am I installing or making an app?” Align with Home’s clear action.
A:473,488 | “App name”; “App recipe” | “Name this connection”; “Which app are you connecting?” | “Why am I naming an app twice?” Put the chooser first and prefill the connection name.
A:527 | “Create app and key” | “Get setup instructions” | “I want to connect.” Keep a nearby factual sentence: “This creates a connection and an access key.”
A:620 | “Connection examples” | “Developer examples” | “I expect steps for my phone app, then get code.” Keep code secondary to verified app-specific instructions.
H:520,584 | “Setup notes” | “Set up [app]” | “Notes sound optional.” Use the selected app’s name and place essential steps inline.

P2 — Try obscures the consequence and uses operator vocabulary.
T:1533 | “Try” | Keep title; add “Send a real request and see its result. Paid models may incur charges.” | “Does this run anything or only preview it?” Show recorded model/location/price immediately before submission; never imply a free test.
T:1823 | “Run” | By tab: “Send message,” “Generate image,” “Generate speech,” “Transcribe audio,” “Choose an option,” “Create embedding,” “Run workflow” | “What will this button do?” Use matching progress verbs; never execute automatically.
T:1615,1658 | “Model name”; “State” | “Model” (or “Workflow” on that tab); “Background information” | “I don’t know which name or state is required.” Show the exact model identifier as selectable secondary text.
T:137,141 | “The gateway is ready.”; “Which option should run first for a smoke test?” | “Hello! This is a sample voice.”; “Which of these tasks should I do first?” | “A spoken sample sounds like a health verdict.” Avoid unsupported readiness and testing jargon.
T:1994 | “Check job” | “Check for results” | “What job?” Describe the member’s objective without claiming completion.

P2 — Library promises an action it does not perform.
L:1247 | “Open in ComfyUI” | “Download workflow” plus “Import this file into ComfyUI.” | “I expect an app to open, but receive a file.” Match the download behavior.
L:1195 | “Download ready.” | “Download requested. Check your browser’s downloads.” | “Did it save?” Describe dispatch, not unverified device saving.
L:1080 | “Images, videos and audio you generate will appear here with the prompt, model, workflow and download links.” | “Find your saved images, videos and audio here. Open an item to download it or see how it was made.” | “I’m reading a feature inventory before I’ve made anything.” Split the 22-word sentence; qualify storage instead of promising every output appears.

P2 — Catalog and spending labels make members decode the system.
M:886 | “Pick a model name.” | “Choose a model for what you want to make.” | “Why am I choosing a name?” Start with purpose; add an exact-ID-preserving “Try this” navigation action.
M:703,708,749 | “Exact model”; “Resolves to”; “Names you can use” | “Model ID”; “Routes requests to”; “Other accepted model names” | “Which value goes in my app?” Identify one canonical value as “Use this name in your app”; keep routing advanced.
U:367–371 | “Remote spend”; “Remote spend · partial”; “Remote spend · coverage unknown” | “Provider charges”; “Provider charges · some costs missing”; “Provider charges · completeness unknown” | “Does remote mean my phone, and is this the whole bill?” Preserve uncertainty; explain these are recorded AI-service charges, not total hosting/electricity costs.
U:463 | “Stacked by capability” | “Grouped by task type” | “Capability means nothing to me here.” Keep explicit task labels in the chart.

P3 — Voice and retry labels drift.
L:976,1126 | “Everything I’ve made.”; “Load more / Retry” | “Your saved creations.”; “Retry loading more” | “Whose ‘I’ is this?” Use second person for guidance and one action per button.
G:8–12 | “Design system”; “The component gallery has moved.”; “Open the design system” | Keep unchanged | This is a developer relocation page, not a member media gallery. Do not promote it as one.

Terminology inventory and treatment
Observed source/report vocabulary needing explanation for non-technical members:
• Connection: gateway (shared AI service), Base URL/gateway address (connection address), API/OpenAI-compatible API/provider (app connection type/service), API key/key/access (permission to use the connection), app recipe (setup guide), access plan (allowed models), default model (preselected model), route/network path (how the request reaches the service), host networking/non-streaming/version caveats (advanced setup only), docs (help).
• Creation: model/model name/model ID/exact model/alias/recommended names/resolves to (distinguish display title, value to paste and routing); engine/provider/local/remote (where processing happens); capability (task type); prompt (instructions); workflow (saved sequence); inputs/outputs (what you provide/get); transcription (audio to text); embeddings/vector/dimensions/min/max/mean/probability (developer or statistical terms, retain with explanations); state (background); smoke test (quick test); job (request in progress); seed (randomness setting); generation time (time to create); px (pixels); media (images, videos, audio).
• Evidence: request/receipt/request ID (one attempt/its recorded details/reference); latency/ms (response time/milliseconds); outcome/status (recorded result); accepted (received, not completed); coverage/retention/truncated (which records are included/kept/omitted); remote spend/provider charge (recorded service cost); technical details/JSON/native request body/file inputs (advanced data).
• Named tools and actions: Python, JavaScript, curl, ComfyUI, Open WebUI and SDK-style examples need task context, not renamed brands. Revoke/rotate should explain stopping/replacing access. Component gallery/design system belongs to developers.
This inventories observed copy; dynamic catalog descriptions, error messages and uninspected shared components cannot be exhaustively certified.

Three delights
1. An earned “Your first message arrived” with app name and receipt, only after an attributable request.
2. “Copy help details” produces a short, reviewable plain-language summary a family member can send without learning JSON.
3. Carry “You chose [model]” from Models into Try and the receipt, with exact ID and honest charge state.

Keep
Preserve “Gateway responded with this key,” explicit incomplete/unknown outcomes, no-provider-charge language, manual-copy fallback, editable examples, progressive technical disclosures, real media previews and truthful Gallery relocation.

Evidence/limits: bounded reads of all seven routes plus five completed surface reports; no excluded files, live calls, shell, edits or checks. Workspace confirms diff_bytes=0 and checks=[]. Final get: no cancellation, revoked authority or owner notes.
