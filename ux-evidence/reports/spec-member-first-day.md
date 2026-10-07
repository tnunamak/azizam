# azizam-ux-spec-member-first-day-20261007

Completed source-only household first-day design spec: eight storyboard frames, responsive layout, state/props contracts, ranked changes, honest app-success and Library backend dependencies. Empty diff; no checks or live validation.

Household member’s first day: buildable design spec
Base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Purpose
Get a non-technical member from a signed-in invitation to a real reply in their chosen phone app, then make and rediscover an image without learning gateway administration. Invitation acceptance is an explicit assumption; invitation implementation is outside scope. Each frame has one visually primary action; secondary navigation remains available. Never equate copied settings, key creation, historical activity or a browser check with a successful app conversation.

Storyboard

1. Invite link → Home
Primary text: “Welcome. Let’s connect your chat app.”
Below: “Chat · Ready” only when available; “Images · Ready” independently.
Action: “Connect an app.”
Loading: “Checking what’s available…”; no ready badge before data arrives. Failure: “We couldn’t check availability.” Primary action becomes “Try again.” If chat is unavailable, show the supported reason and “Ask your household owner to enable chat”; action “Copy help details” copies a reviewed, non-sensitive explanation. Do not infer availability from colour or hide the reason.

2. Apps: choose
Primary text: “Which app are you connecting?”
Action: “Continue” after selection.
Show maintained phone-compatible guides first, “Another app” next, “Code or developer tool” in a disclosure. Do not invent a recommended client or app-store link. If no maintained phone guide exists, say “General setup instructions” and ask the user to choose their existing app; do not silently select Python. Installation happens outside this flow. Guide entries must be version-verified before exact menu paths ship.

3. Apps: establish connection
Primary text: “Set up [app]. Keep this page open while you switch to your app.”
Action: “Get setup instructions” using existing access-creation behaviour. Name defaults to “[app] on my phone” and is editable.
If partial creation fails, retain the created connection and retry only the failed step, preserving existing recovery. Never silently recreate access. Explain that this creates access for the chosen app.

4. Apps: add settings, one step at a time
Primary text: “Add the gateway address in your app.”
Action: “Copy gateway address”; next substep uses “Copy key”; next uses “Copy model name” where needed. Each step displays the exact client field label when verified, otherwise labels the guide generic. Copy feedback says “Copied”; advancing is explicit and never proves pasting.
Clipboard failure reveals selectable text and “Copy this value manually.” Keep the existing protected reveal behaviour; no secret enters browser storage, URLs, help summaries or progress metadata. Persist only selected guide ID and step.
For a loopback address show “This address only works on the device running Azizam. Ask your household owner for an address your phone can reach.” Do not invent a network diagnosis.

5. External phone app: first message
Primary text on the handoff screen: “Send ‘Hello’ in [app]. You should see a reply there.”
Single action in the external app: send the message. Returning to Azizam offers “I received a reply.” That records self-reported completion, labelled “You confirmed your app works.”
A separate optional troubleshooting action “Check gateway” states “Makes a real request. This does not test your app.” Show selected model and available pricing before submission. Never run automatically.
No request evidence: “We haven’t verified a request from this app.” Action “Review setup.” Verified failed request: name only the established failure boundary and one recovery action. Unknown: “We couldn’t identify where this failed.” An actual successful app-attributed receipt can replace self-report with “Your first message arrived” and its receipt; this requires additional data below.

6. Home → Try / Image
Primary text: “Make your first image.”
Action: “Generate image.”
Prompt default: “A cosy reading nook with a sleeping cat, warm afternoon light.”
Use a catalog-recommended accessible model, otherwise require choosing from available image models; never assume the alias “image” resolves. Model choice and price/locality remain visible but subordinate. Empty: “Images aren’t available for your account right now.” No unsupported paid fallback.
Running: “Creating your image…” and disabled submit; preserve the prompt. Failed: “We couldn’t finish this image.” Action “Try again” only as an explicit new request, with pricing still visible. Timeout/unknown completion: “The result isn’t confirmed. Trying again may create another image”; offer “View receipt” when available instead of silently resubmitting.

7. Try: result
Primary text: “Your image is ready.”
Show actual preview and “Provider charge: [recorded cost]”, otherwise “Cost not recorded · this does not mean free.”
Action: “Open in Library” only after an exact matching asset is found.
Until then: “Your image is ready here. We haven’t found it in Library yet.” Primary action “Download image”; secondary “Check Library again.” Never claim saved from a preview URL. Preview failure: “We couldn’t load this preview. Your file may still be available”; download remains independently usable. Missing receipt says “Receipt not available.”

8. Library: later that day
Primary text: “Your library.”
Action: open the image card. Visible “Search prompts” plus All / Images / Video / Audio chips; default scope Mine.
Empty: “No creations found.” If filtered, “Clear filters” preserves scope; genuinely empty, “Make an image.” Error: “We couldn’t load your library” with “Try again.” Refresh failure retains content with a warning; scope changes do not retain another scope’s images under the new label.
Image detail primary action: “Download image.” Two-line title, preview, compact date/model, prompt shown once behind expansion, receipt secondary.
Confirmed missing: “This file is no longer available”; retain prompt and receipt. Preview-only failure offers “Retry preview.” After initiating download say “Download requested. Check your browser’s downloads,” never “Saved.”

Layout
1440 px: preserve application shell; centre a max-1120 px content region with 32 px margins. Journey screens use a 640 px main card and 280 px “Your progress” aside separated by 32 px. One primary button per step; help below it. Image results use 2/3 preview, 1/3 details. Library uses four equal columns, 20 px gaps; detail drawer 480 px.
390 px: 16 px gutters, 358 px content, single column. Compact “Step 2 of 3” replaces the aside. App choices and fields stack; 44 px minimum controls. Primary button full width, with safe-area padding, never covering content. Forms use dynamic viewport max-height and scrolling with keyboard open; no autofocus that summons the keyboard at app choice. Library uses two 171 px cards with 16 px gap, persistent search and wrapping chips. Details occupy the full-width sheet; preview precedes the action row. Long identifiers wrap without horizontal page scrolling. At large text/zoom, switch cards to one column.

Component contracts
MemberJourney({step: choose|settings|message|image|library, completion: none|selfReported|verified, onStepChange, onSkip}).
CapabilityStatus({capability, state: loading|ready|unavailable|error, reason?, onRetry}).
AppSetupGuide({guideId, appName, step, baseUrl, modelChoices, selectedModel, onSelectModel, onContinue, onReviewSetup}); compose existing protected credential controls, not a newly persisted secret prop.
ConnectionEvidence({state: unchecked|checking|gatewayResponded|selfReported|appVerified|failed|unknown, result?, onCheck, onConfirmReply, onReviewSetup}).
FirstImage({catalogState, options, selectedId, prompt, requestState: idle|pending|succeeded|failed|unknown, result?, matchedAsset?, onGenerate, onCheckLibrary}).
CreationDetail({asset, previewState, downloadState, onRetryPreview, onDownload, onOpenReceipt}); no automatic generation or sharing.

Data contracts and limits
• Home availability: HomeCapability.available/status/reason_code/model/recommended_model/use_names, web/src/app/home/home-api.ts:18–36. Unknown reasons must remain unknown.
• Browser check: ConnectionResult.ok/latency_ms/reason/model/request_id, same file:72–79. These prove only that check.
• Setup address: discoveryData.recipes.openai.canonical_base_url, web/src/app/apps/page.tsx:99–103. Chosen client is local state at :91, not verified installed-app metadata.
• Historical activity: app.credentials[].last_used_at, Apps :331–338. Insufficient for successful first-message proof. Needs backend field first_successful_external_request with app_id, request_id, completed_at and outcome. Until available use explicit self-report; no green “Connected” from last_used_at.
• Image catalog: CatalogOption.id/label/recommended/capability/price/locality, web/src/app/try/page.tsx:82–91. Result.imageUrl/downloadUrl/cost/receiptId, :125–144; current conditional cost at :2041–2058 must become explicit unknown.
• Library: MediaAsset.asset_id/request_id/kind/prompt/model/created_at/download_url/available/unavailable_reason, web/src/app/library/page.tsx:67–90. Match receiptId to request_id exactly, displaying a choice if multiple assets match. Existing bounded listing cannot establish that an absent asset does not exist. Needs backend field Result.asset_id for deterministic direct handoff, or a documented request-ID lookup. No silent global scan.
• Reentry previews: HomeCreation.asset_id/kind/prompt/download_url, home-api.ts:42–51; link exact asset to Library.
• Verified client menu instructions/version are maintained frontend content, not existing backend data. Do not ship invented instructions.

Accessibility
Semantic ordered progress, current step aria-current, headings and visible field labels. Announce copy/progress/results politely; errors immediately with focusable summaries. Move focus to the next step heading and return focus after sheets close. Keep focus visible and keyboard dismissal/restoration intact. Readiness needs text, not colour. Image accessible label uses a concise prompt excerpt; full prompt remains readable separately. Support reduced motion; no confetti, autoplay or repeatedly announced polling.

Ranked implementation changes
P1: Replace Home “Other”→Python routing (home/page.tsx:365–368,513–515) with an honest phone/general chooser.
P1: Build resumable setup and distinct evidence states (apps/page.tsx:485–500,595–683; home/page.tsx:315–332). No unsupported connection success.
P1: Add exact image→asset handoff and honest cost/persistence states (try/page.tsx:125–144,2041–2058).
P2: Visible Library search, prompt deduplication and actionable preview failures (library/page.tsx:211–236,976–1006,1154–1212).
P2: Rewrite FOR_MEMBERS.md:27–49,82–96 around this journey; retain real-request warning and link privacy guidance.

Keep existing copy recovery, responsive controls, explicit partial failures, real previews, receipts and progressive technical disclosures. Remove first-run “recipe” vocabulary, developer-first defaults, duplicated prompts and unearned success/saved claims. Three small delights: resume precisely after app switching; earned first-message receipt; Home’s last-creation card reopening the exact image.

Evidence
Source-only specification; reused completed family/Home/Apps/Library reviews and directly inspected bounded allowed Home, Apps, Try, Library and member-guide source. No excluded reads, shell, browser, live requests, model APIs, patches or checks. No client-menu, visual or usability validation. Final workspace receipt: diff_bytes=0, checks=[]. Final cancellation/authority flags clear; owner_notes empty.
