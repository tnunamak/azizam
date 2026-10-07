# azizam-ux-spec-home-20261007

Home design spec: two compositions and recommended readiness-first connection workbench; exact states/copy, desktop/mobile layout, source-mapped props/data and backend gaps. Empty diff. One out-of-scope read denied and stopped; no tests.

Home design spec: ready to use, honestly
Snapshot: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Review only.

Purpose
In five seconds, answer “What can I use, and how do I connect?” Keep account capability availability, key validity metadata, gateway-check evidence and external-app success separate. Never infer that an app works from copying settings or creating a key.

Two compositions
A. Readiness + connection workbench. A full-width readiness header leads into one app chooser and its inline setup. Recent creations follow. Owners get a small operations disclosure after setup. This suits first-time members and keeps the task continuous.
B. Personal launchpad. Readiness and connection-address strip above two columns: “Connect an app” on the left; “Recent creations” on the right. Key state stays inside setup; owner alerts sit below both. More immediate for returning creators, but compresses setup and divides first-time attention.
Recommend A for both roles. Do not fork the owner into a dashboard. Capability failures stay visible to everyone affected; operational diagnostics are owner-only.

Layout
1440 px viewport: retain existing shell; main max-width 1120 px with 32 px gutters and 24 px section gaps. Header spans width. Capability sentences use a wrapping two-column list. Workbench: app-choice grid (four columns), then an 8/4-column split for instructions/settings and key/check. Recent creations: four cards. Owner disclosure below workbench, above creations, never a banner above the primary task.
390 px: 16 px gutters, 16 px gaps, single column. Order: heading; readiness sentences; address; chooser (two columns, Other full width); selected guide; key; check; owner disclosure if applicable; creations (two columns, four maximum). No carousel, horizontal page scrolling, sticky footer or automatically opened keyboard. Long addresses wrap or scroll within their field, with copy always visible. Instructions expand in place; secondary caveats may use a scrolling sheet bounded by 100dvh.

Readiness states and exact copy
Visible h1:
• Loading: “Checking what you can use…” with stable skeleton space.
• Every returned capability consistently ready: “Your AI is ready”; subtitle “The gateway reports these capabilities ready. Connect an app below.”
• Mixed states: “Here’s what you can use”.
• No capabilities: “No capabilities are available to your account yet.” Member next step: “Ask the owner to connect a model or update your access.” Owner: “Review models and access.”
• Fetch failure: “We couldn’t load your AI availability.” Retry. Previously fetched settings may remain with “Saved view; availability could not be refreshed.” No green readiness treatment.
• Signed out: retain “Sign in to use your AI”.

Every capability has its own visible sentence, not a color-only chip. Use task subjects: Chat, Image creation, Video creation, Speech generation, Music creation, Sound-effect creation, Transcription, Decision requests, Embeddings, Reranking, Audio creation. Templates:
ready + available: “[Task] is ready.”
idle: “[Task] is idle and may need to start.”
starting: “[Task] is starting. Try again shortly.”
busy: “[Task] is busy. Try again shortly.”
unavailable: “[Task] is unavailable right now.”
missing, conflicting or unrecognized state: “We couldn’t determine whether [task] is available.”
Add an already-supported plain-language reason when present; unknown reason codes do not become invented diagnoses. “View models” remains a secondary link. Do not claim an ETA, successful generation, network reachability from the member’s phone, or freshness timestamp without evidence. Status means the returned snapshot, not an independent live test.

Connection workbench
Title “Connect an app”; helper “One address. A key for each app.”
Always show “OpenAI-compatible address” and Copy, using the supplied API URL exactly. Missing URL: “Connection address unavailable.” Disable copy; never synthesize a host.
Question: “Which app are you connecting?”
Named choices: Open WebUI, LibreChat, SillyTavern, Cursor, Codex, Claude Code. Other expands a real selector: Phone app; Python; JavaScript; curl; Another OpenAI-compatible app. No default Python selection.
Phone/generic helper: “In your app’s connection settings, choose an OpenAI-compatible provider and paste these values. The field names may differ.” Do not presume household Open WebUI is installed.
After selection: “Set up [app]”, the existing recipe description, client-specific field labels, model, key instruction, then “Try a message in [app]”. Preserve special recipe URLs: Claude Code uses public_base_url; curl uses its full endpoint. Label these as the selected recipe’s fields, not all “Base URL”. Named recipes remain general guides unless their menu steps/version are verified; do not invent menus.
Python/JavaScript/curl show existing code with environment-variable placeholders, never interpolate the user’s key. JavaScript retains “Run this server-side.”
Scoped chat-model picker: show only scoped chat aliases. Prefer an available default; otherwise require a choice. Never substitute a different route silently. If cost classification is unavailable, say “Pricing is not shown here”; no Free/Local/Paid invention. No aliases: “This key has no chat models. Choose another key or ask the owner for chat access.” Do not confuse absent default with absent access.

Your key
Always show a compact metadata state, even before recipe selection:
• No unexpired, unrevoked keys: “You don’t have a current app key.”
• At least one: “You have [n] current app key(s). Choose one or create a key for this app.” “Current” means expiry/revocation metadata only.
• Creation unavailable: “Your access does not allow new keys. Ask the owner for access.”
• Creating: “Creating your app key…”
• Newly returned: “Save this key now. You can’t view it again.”
• Existing selected: “Paste your saved key to check the gateway.” Keep masked input and page-only handling.
• Partial app creation: “The app was created, but its key wasn’t. Retry key creation.” Reuse pending app rather than duplicate it.
• Generic failure: “We couldn’t create the key. Try again.”
Key creation and copying never create a Connected badge. Retain existing reveal/save behavior; this spec changes presentation, not key handling.

Honest check
Button: “Check gateway with this key”.
Always adjacent: “This checks the gateway with your key; it cannot test your app.”
Idle: “Not checked on this page.” Pending: “Checking the gateway…”
Success: “The gateway responded with this key.” Show returned model/path, latency and request receipt when present. Next: “Now send a message in [app] to check the app itself.”
Returned failure: “The gateway check failed.” Display returned reason without attributing it to the app.
Unclassified exception: “We couldn’t complete the gateway check. Your key may still be valid. Try again.”
Reset evidence when app, key or model changes. Never reinterpret an older result as evidence for the newly selected configuration.
The current test accepts key and key ID, not selected model. It must not claim to test the chosen model. Until a preflight contract describes test target and possible charges, do not market this as free or run automatically. A charge-aware test needs backend fields check_target, billable and estimated_cost/currency (or explicit cost_unknown); this is a release dependency for charge-consent UX.

Recent creations and owner alerts
Title “Recent creations”; “View library”. Show up to four existing returned assets; do not promise strict latest-first order because the current fetch favors previewable assets. Every image, video and audio has “Open in Library” to its asset detail. Separate audio play from navigation.
Empty: “Your images, videos and audio will appear here.” Failure: “Recent creations couldn’t be loaded.” Retry. Partial: “Some recent creations may be missing.” Missing preview: “Preview unavailable”; retain detail link.
Owner-only collapsed “Owner attention” row uses existing unavailable status summary and “View Status”. No invented alert count or severity. Deduplicate existing shell alerts. Member receives neither owner controls nor diagnostics; affected capability sentence remains visible. No automatic expansion or layout takeover.

Build contracts and evidence
Paths below are web/src/app/:
• ReadinessHeader({capabilities, loading, error, stale, onRetry}); HomeCapability fields capability/available/status/reason_code/title plus optional availability are evidenced in home/home-api.ts:18–40; mapping in home/presentation.ts:88–191. stale is local query state, not backend age.
• ConnectionWorkbench({apiBaseUrl, publicBaseUrl, apps, newAppAliases, credentialAliases, onCreateKey, onCheck}); supplied URLs/scopes and key metadata consumption: home/page.tsx:252–286. App can_mint_keys, key id/display_name/revoked_at/expires_at and user email are consumed there and :401–419. Owner role: :987.
• AppChooser({value,onChange}); RecipeGuide({client,fields,description,code,docs}); recipe fields/keyLabel/code/URL distinctions: home/recipes.ts:3–169. Generic option is a new static recipe, no backend requirement. Align Apps’ presently generic connection panel (apps/page.tsx:606–660) to the same recipe renderer.
• KeyState({keys,selectedId,canCreate,creating,pendingApp,error,onSelect,onCreate}); local states and existing handlers: home/page.tsx:235–333, 420–489.
• GatewayCheck({state,result,error,onRun}); ConnectionResult.ok/latency_ms/reason/path/model/request_id: home/home-api.ts:70–77; test call contract :180–185.
• RecentCreations({assets,partial,loading,error,onRetry}); asset_id/kind/prompt/model/created_at/download_url/poster_asset_id: home/home-api.ts:41–57; preview-prioritizing fetch :132–172.
• OwnerAttention({isOwner,statusCopy,shellAlertPresent}); existing inputs: home/page.tsx:987–992,1022–1035.
Needs backend fields if desired later: capability_checked_at; app-specific successful-request attribution; verified household_launch_url. Until supplied, omit “just checked”, “app connected” and “Open household app”.

Accessibility and removal
Use one visible h1, semantic sections, keyboard-operable choice controls, 44 px targets, explicit selected state, visible focus, text plus icons, and polite result announcements. Announce copy success; preserve manual-copy fallback. Do not announce every polling refresh. Associate input errors and check scope with controls. Respect reduced motion; no autoplay. Test layout later at 200% text and phone keyboard height.
Remove hidden-only readiness, tooltip-only reasons, Other→Python shortcut, default-alias-only blocking, “Setup notes” hiding essential steps, assumed phone/Open WebUI connection, blanket key-blaming error, and owner banners before setup.

Evidence and limits
Reused terminal Home and Apps audits and read permitted Home/Apps source in bounded excerpts. Guardian denied web/src/lib/product-api.ts as outside these review paths; stopped that path without retry or workaround and reported the blocker. No excluded contents obtained. No writes, shell, checks, rendered UI tests, live gateway/client calls or external recipe verification. Workspace receipt: diff_bytes=0, checks=[]. Cancellation/authority and owner notes checked before submission; active, no notes. Source-derived design proposal, not implementation or verified runtime behavior.
