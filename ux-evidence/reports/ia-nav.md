# azizam-ux-ia-nav-20261007

Completed source-only IA/navigation review: seven ranked findings, target role-aware IA, three delight opportunities, and strengths. Empty diff verified; no checks or live validation.

Information architecture and navigation review
Source-only heuristic review of tnunamak/azizam at 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Findings describe source-defined behavior, not observed user failures.

Ranked findings

1. P1 — Cross-links discard the user's choice.
Evidence: web/src/app/home/page.tsx:179–182; web/src/components/shell/app-shell.tsx:651–656; web/src/app/models/models-client.tsx:834–905.
Member: “I picked Images on Home, but Models makes me choose again.” Owner/builder: “Search found my model, but selecting it does not open that model.” Home sends capability parameters and command search sends model parameters; Models initializes capability to “all” and detail to null without consuming either. This breaks the promised calm handoff and makes navigation feel unreliable.
Change: Consume validated capability and model parameters after the catalog loads. Apply the selected capability, open/focus the exact matching model drawer, and preserve filters in the URL and browser Back. If no longer available, say “This model is no longer available to your account” with “Browse available models.” Preserve music and sfx explicitly instead of mapping both to audio.

2. P1 — “You” changes meaning for owners on Usage.
Evidence: web/src/lib/product.ts:48–53; web/src/app/usage/page.tsx:158–176, 188–190, 203–214.
Owner: “I opened Usage under You; are these my requests or everybody's bill?” The default is all people for owners, while the title is simply “Usage” and “Current month usage.” A filter exists, but the navigation promise and default disagree. Scope ambiguity around spend harms trust.
Change: Default the You destination to self for every person. Use title “Your usage” and subtitle “Your requests and remote spend this month.” Give owners an explicit “Everyone's usage” choice, changing the title to “Everyone's usage” and retaining a visible scope label beside the money summary. Owner-wide entry points should set scope explicitly.

3. P2 — Phone navigation spends scarce space twice on health.
Evidence: web/src/components/shell/app-shell.tsx:989–1002, 1009–1049; web/src/lib/product.ts:30–47.
Member: “I can reach Status from the header and bottom bar, but have to open More to find what I made or try something.” The bottom bar is Home / Apps & keys / Status / More, while the header already contains the health popover with View status. This favors administration over the household's recurring work.
Change: For members use Home / Try / Library / More. Keep Apps & keys directly on Home and in More. Preserve the header health indicator and its Status link. For owners retain Home / Apps & keys / Status / More if operational access is the priority. When a More destination is active, mark More current and display that destination's label, e.g. “Models,” so location is never lost.

4. P2 — Search promises members model results it never fetches.
Evidence: web/src/components/shell/app-shell.tsx:522–540, 599–608, 649–665.
Member: “Search says models, but a model I can use gives no matches.” Model results are sourced from the service registry and enabled only for admins, while the placeholder promises model search to everyone. Owners' results also depend on local service configuration rather than the complete user-visible catalog.
Change: Search the permitted Models catalog for every authenticated person. Match friendly names and usable aliases; select into the same model drawer as finding 1. Keep “Search pages, models, apps…” only when these categories are supported; show “Couldn't load models. Retry” separately from genuine “No matches.”

5. P2 — Three sibling destinations make users classify implementation concepts.
Evidence: web/src/lib/product.ts:24–41; web/src/app/workflows/page.tsx:788–797, 811–820.
Member: “Should I choose Models, Workflows, or Try to make an image?” “Choose by purpose and inputs” helps inside Workflows, but the navigation offers no relationship between discovering a model, discovering a workflow, and running a request. Eight member destinations including Status dilute the first choice.
Change: Retain stable route labels but group Models and Workflows under an expandable “Explore.” Add descriptions “Models · choose a model name” and “Workflows · ready-made ways to create.” Keep Try directly visible with “Test a request.” Workflows empty-state copy should be “No workflows are available to your account yet. You can still explore models,” with “Explore models”; leave engine/access-plan setup instructions to owner-specific guidance.

6. P2 — Legacy Engines links lose their navigation context.
Evidence: web/src/app/services/page.tsx:1; web/src/lib/product.ts:5–7, 60–65; web/src/components/shell/app-shell.tsx:373–376.
Returning operator: “My saved Services link opens Engines, but nothing in navigation is selected.” The compatibility route re-exports Engines, but canonicalization only recognizes Home aliases. This makes an intentional compatibility path feel unfinished.
Change: Canonicalize /services to /engines for active navigation and shell page lookup; preserve the old URL as a supported alias. Use “Engines” consistently in visible labels. This is an IA finding, not an assertion about backend access enforcement.

7. P3 — The account menu mixes identity with contributor tools.
Evidence: web/src/components/shell/app-shell.tsx:188–208.
Member: “I opened my account and found Design system and API reference. Are these part of using my AI?” The public contributor surface competes with useful account actions and introduces unnecessary vocabulary.
Change: Account menu: person's name, Apps & keys, Sign out. Add a separate “Help” entry with “Connect an app,” “Troubleshoot a request,” and an “API reference” subsection for builders. Link “Design system” from contributor documentation rather than every household account menu.

Target IA (proposed; preserve existing URLs)

Azizam
├─ You
│  ├─ Home: connect an app and available capabilities
│  ├─ Explore
│  │  ├─ Models
│  │  └─ Workflows
│  ├─ Try
│  ├─ Library
│  ├─ Usage: your usage by default
│  └─ Apps & keys
├─ Gateway [owners]
│  ├─ Status
│  ├─ Engines
│  ├─ Providers
│  ├─ People
│  ├─ Activity: Changes / Requests
│  └─ Settings
└─ Utilities: Search, status indicator, Help, Appearance, Account

Reasoning: discovery, testing, outputs, and account management stay distinct; owner administration remains role-scoped. Members reach their capability-level Status through the persistent indicator and Help. People/Settings placements follow the strategy, not inspection of those excluded pages.

Three delight opportunities
• A model-search result previews capability, availability, and paid/local status, then opens the exact model in one step.
• Preserve the return journey: a result's “View in Library” opens the exact item, with “Back to your request” restoring inputs without running anything.
• Contextual navigation help: “Looking for the image you made? Open Library” and “Checking a failed request? View recent requests,” using the current surface rather than a generic tour.

Keep
• One shared destinations list, You/Gateway role separation, and the working /services compatibility route.
• Skip-to-content, aria-current, explicit expanded-navigation controls, and focus restoration.
• Honest “Check gateway reachability” wording, separate engine observation, and status links that focus the affected section.
• Member Status's connection-settings / Try / recent-requests recovery choices and Models/Workflows receipt links.

Evidence and limits
No patches, checks, shell, live API calls, browser sessions, model APIs, or user validation. Excluded files were not read; imports were not followed into them. Route inventory, complete shell, strategy, and selected permitted route source were reviewed; this is not an exhaustive audit of every page body. workspace_result confirms diff_bytes=0, empty-diff SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855, checks=[]. Latest get showed no cancellation/revocation and no owner notes.
