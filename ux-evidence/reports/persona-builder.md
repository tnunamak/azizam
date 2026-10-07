# azizam-ux-persona-builder-20261007

Completed builder journey review: first-model mismatch, setup/documentation handoffs, failure evidence and receipt discovery; seven ranked findings, three delights, empty diff.

Builder journey: from client choice to an attributable result

Source-only walkthrough, base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. This is not a timed usability test or proof that a deployed client works.

Narrative
“I understand the README’s endpoint/key/model contract and appreciate the distinction between a recipe and a tested client. I register my-llama, then Home expects default. Looking for my client, I find LibreChat and Codex, but their actual configuration is under Setup notes; SDK options are less discoverable. The long recipe document eventually gives me runnable SDK examples, protocol limits and agent discovery. Models and Workflows help me choose, but the exact identifier and selection do not consistently carry into Try. If Try fails, the diagnostic trail weakens just when I need it. Activity offers receipts, but finding my attempt and knowing whether a missing receipt means anything still require interpretation.”

Time-to-first-success is therefore dominated by avoidable naming, navigation and diagnostic handoffs. No elapsed-time claim is warranted.

Ranked findings

1. P1 — The documented first model cannot satisfy Home’s recipe rule.
Surface/source: README.md:29–30; web/src/app/home/recipes.ts:22–31; web/src/app/home/page.tsx:532–540,600–605.
“I followed the quickstart and made my-llama, but Home says my key does not include the default model and withholds the code sample.”
An optional alias becomes a hidden setup requirement, violating stable defaults and the builder promise of exact, scriptable setup.
Change: prefer an available default, otherwise offer permitted chat models. Show their exact IDs, availability, location and charge labels. Require explicit selection of a paid alternative. Copy: “Choose a chat model for this connection.” When none exist: “This key has no chat models. Choose another key or ask the owner for access.” Generate the snippet from the selection.

2. P1 — A failed test loses its diagnostic handoff.
Surface/source: web/src/app/try/page.tsx:245–270,1300–1337,1835–1840, as evidenced in the completed Models/Workflows/Try review; web/src/app/activity/requests.tsx:129–151,428–436.
“My test fails with a message or HTTP code; I cannot reliably carry that attempt into a receipt explaining which layer failed. The receipt’s next action sends me to raw technical details.”
This recreates the owner’s core trust failure: gateway, model, client and workflow become indistinguishable.
Change: preserve request ID, target, elapsed time, HTTP status and safe structured errors on failure. Show “Request failed” and “Reported by: [verified layer]”; use “The failing layer could not be identified” when unknown. Offer “View failure receipt” only when available, plus “Copy diagnostic summary.” Give one evidence-backed next step; never derive blame solely from a status code.

3. P2 — Setup instructions are split across optional-looking UI and a reference-sized document.
Surface/source: web/src/app/home/page.tsx:519–520,581–605; web/src/app/home/recipes.ts:58–69,96–107; docs/LOCAL_CLIENT_RECIPES.md:1340–1368,1397–1455.
“I choose Codex, but its TOML is hidden behind ‘Setup notes.’ The document’s Codex heading gives me a Python Responses example, so I still need to locate the CLI configuration.”
LibreChat similarly needs its YAML and restart instruction, not merely field values. Essential actions should precede caveats.
Change: rename the disclosure “Set up [client]” and show the recipe description inline. Add “1 Configure client · 2 Send one request · 3 Inspect result.” Put client-specific configuration and its documented destination first; keep version/network limitations separate. Give Codex a distinct CLI subsection using the existing Home configuration, and label the Python example “Responses SDK example.”

4. P2 — The client guide makes common first requests compete with advanced media workflows.
Surface/source: docs/LOCAL_CLIENT_RECIPES.md:44–190,192–400,1079–1144,1332–1541,1830–1897; README.md:54.
“As an SDK or agent builder, I pass extensive compatibility and media material before finding my recipe; after a failure, debugging is another distant section.”
A 2,075-line operational reference is useful, but the entry path spends attention before producing a result.
Change: add a top-level chooser linking directly to “OpenAI SDK,” “Codex CLI,” “LibreChat,” and “Agent tools (MCP).” Put a compact “First successful request” section before the interface matrix: choose a currently available model, configure the client, send the editable sample, inspect the response and request ID. Link advanced workflow controls afterward. Standardize the documentation/UI environment-variable convention, or explicitly explain their equivalence.

5. P2 — Exact names and capabilities do not travel together.
Surface/source: web/src/app/models/models-client.tsx:379–432,744–759,1067–1078; web/src/app/try/page.tsx:513–531,1614–1627,2181–2232; web/src/app/workflows/page.tsx:424–435,474–495. Evidence reused from the completed scoped discovery review.
“I can copy a model in Models, but must find Try and choose again. A friendly title can replace the name I need to paste, and a workflow is also labeled Model name.”
The builder needs a durable API contract, not another identification puzzle.
Change: add “Try this” carrying exact ID and supported capability without executing. Always show title plus selectable ID. Label the canonical model selector “Use this name in your app”; separate aliases and backend resolution. Label workflow selection “Workflow” and expose “Copy workflow ID,” without implying every workflow ID is an OpenAI model name.

6. P2 — Receipt discovery promises more certainty than the documentation establishes.
Surface/source: docs/LOCAL_CLIENT_RECIPES.md:1862–1897; web/src/app/activity/page.tsx:154–158; web/src/app/activity/requests.tsx:216–269,285–308,441–471.
“I have a request ID, but Requests offers person/source filters rather than ID lookup. An empty list says requests appear here, while the guide describes receipt coverage limited to image routes.”
The conflicting coverage description makes absence hard to interpret. Backend coverage is not verified by this audit.
Change: add “Find request by ID” and a direct Requests-tab destination from setup/test results. Reconcile the route coverage documentation with verified behavior. Empty copy: “No receipts found. Some routes may not record a receipt; this does not prove no request was received.” For an individual missing receipt, retain the known request result rather than claiming failure.

7. P2 — “Supported” answers a routing question before the builder’s capability question.
Surface/source: docs/SUPPORT_MATRIX.md:3–12,16 onward.
“The table says supported, but its definition means only that a handler exists. I still need other documents to know whether my agent’s feature will work.”
The qualification is honest; the dominant label remains easy to overread.
Change: label the generated column “Route implementation” and use “Implemented / Partial / Explicit rejection.” Above it, link a compact client-feature view with version, tested evidence, required configuration and known limitations. Keep runtime model availability separate from compatibility.

Three delight opportunities
• A connection handoff card containing the exact endpoint, selected model, client recipe and one editable test, retained through Try and the receipt.
• “Copy this successful request” after a verified result, translating the actual submitted parameters into SDK/curl without sensitive values.
• A restrained first-client confirmation: “Request received from [app] · [model] · [time],” linked to evidence. Never award success for copying settings.

Keep
README’s explicit client-test caveats; recipe prerequisites and unsupported-feature explanations; discovery-first agent guidance; Home’s measured gateway-result wording and receipt link; Models’ capability grouping; Workflows’ existing Try deep link; Activity’s requested-versus-served model, declared fallback and honest “Outcome not recorded.”

Evidence and limits
Directly reviewed the permitted documentation, Home recipes/connection UI and Activity request UI; reused completed Home, Apps and Models/Workflows/Try reports for scoped cross-surface evidence. No excluded source reads, browser/user validation, live gateway/model/client calls, shell, writes or checks. Workspace receipt confirms diff_bytes=0 and checks=[]. Final assignment read: no cancellation, revoked authority or owner notes.
