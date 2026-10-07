# azizam-ux-spec-page-anatomy-20261007

Partial page-anatomy spec: proposed PageHeader, tabs, segmented control and filter contracts, responsive behavior, copy and bounded migrations. Full inventory blocked by denied directory enumeration; no denial retried. Empty diff verified.

PARTIAL PAGE-ANATOMY SPEC — BLOCKED INVENTORY
Repository tnunamak/azizam, base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. This is a proposed frontend contract, not a claim that every route was inspected.

1. Purpose and evidence
Every page first answers: “Where am I, what does this cover, and what can I do?” Keep scope beside the title, navigation separate from filters, and at most one emphasized page action. Household pages should remain understandable without infrastructure vocabulary.

Evidence is limited to terminal reports azizam-ux-visual-system-20261007 (partial) and azizam-ux-ia-nav-20261007. Their file:line references below are secondhand source evidence, not fresh reads. The excluded token-file read from the visual review is not used.

2. Bounded existing-state inventory
“Unverified” means the reports do not establish the requested property; it does not mean absent. Paths below begin web/src/.

Page | Title font | Subtitle | Actions | Tab/filter pattern | Evidence
Home | Unverified | Unverified | Capability links | Links supply capability parameters | app/home/page.tsx:179–182
Models | Unverified | Unverified | Model detail opening | Capability initializes to “all”; detail null; inbound parameters ignored | app/models/models-client.tsx:834–905
Workflows | Unverified | “Choose by purpose and inputs” is reported introductory copy, exact subtitle placement unverified | Unverified | Unverified | app/workflows/page.tsx:788–797,811–820
Usage | Unverified | “Current month usage” | Unverified | Person filter; owners default to everyone | app/usage/page.tsx:158–176,188–190,203–214
Engines | Unverified | Unverified | Save in editor; page-primary placement unverified | Unverified | app/engines/page.tsx:319–341,387–393
Services | Same re-exported surface as Engines | Same surface | Same surface | Same surface | app/services/page.tsx:1
Design | Unverified | Non-live-data disclaimer; exact copy unverified | Unverified | Unverified | app/design/page.tsx:133–137
Try, Library, Apps & keys, Status, Providers, Activity | Unverified | Unverified | Unverified | Unverified | Destinations only: lib/product.ts:24–65; detailed page anatomy not established
Gallery and any additional routes | Unverified | Unverified | Unverified | Unverified | Prior visual report lists Gallery as read but provides no usable anatomy references; full route discovery blocked

Shared header owns pb-8 at components/product/page-header.tsx:37; Design and Usage add outer gap-8/space-y-8 at app/design/page.tsx:133 and app/usage/page.tsx:184. Title typefaces, full subtitles, primary actions and all tab patterns cannot responsibly be reconstructed from these reports.

3. One PageHeader
API (proposed, not current): PageHeader({id, title: string, description?: string, scopeLabel?: string, primaryAction?: {label, onPress | href, pending?, disabled?, disabledReason?}, secondaryActions?: Action[], status?: {tone: neutral|warning|danger, text: string}}).
Require one title; description max two short sentences; actions use visible text. Action union must choose navigation href or event handler, never both. Secondary actions max two before a labelled “More actions” menu. No arbitrary children slot that recreates bespoke headers.

Layout at 1440px: use existing shell content boundaries; one horizontal row containing flexible title/description/scope at left and actions at right, gap 24px. H1 proposed shared display face, 36/40, same face everywhere; description 14/21, max-width 65ch. Scope is text (“Your usage” / “Everyone's usage”), never color alone. Header owns no bottom padding. Page stack provides 32px header-to-controls and 24px controls-to-content.

At 390px: 16px side padding gives 358px content; title 30/36, description wraps naturally. Title, description, scope, then actions vertically. Primary full-width; secondary actions wrap below. Controls have minimum 44px targets. Page stack gaps 24px then 16px. No fixed header height; long translated labels must reflow without clipping.

States: static title remains during initial data loading. Do not replace the header with a spinner. Optional action pending copy: “Saving…” only for actual persistence. Prevent duplicate submission; retain focus. Failed action: “Couldn't save changes. Try again.” adjacent to the action, preserving entered values. Fetch failure belongs to affected content: “Couldn't load this view.” / “Retry”. Empty results: “No results match your filters.” / “Clear filters”. Genuine empty datasets use page-specific copy and must not imply a failure. Never infer “healthy”, “free” or “no activity” from missing data.

4. One tab pattern
PageTabs({label, items: {id,label,href?}[], value, onValueChange?}).
Use for distinct peer views with different content structures and meaningful deep links, e.g. proposed Activity “Changes” / “Requests”. Never for capability filters, display density or person scope.
Local panels: semantic tablist/tab/tabpanel, roving focus, arrows plus Home/End, selected state, labelled panel; manual activation when content loads asynchronously. Route destinations: navigation links with aria-current, not fake ARIA tabs.
1440px: text tabs with one selected underline, no pill containers. 390px: horizontally scrollable single row, visible edge cue, focus scrolls selected item into view; page itself never overflows. Preserve selection and valid query state through Back.

5. One segmented control
SegmentedControl({label, options: {value,label}[], value, onChange, disabled?}).
Use for 2–3 mutually exclusive ways of presenting the same data, e.g. “List” / “Grid” if a page actually supports both. Never invent a second view to justify this control. Implement labelled radio-group semantics with arrow-key selection. Full visible labels, no icon-only choices. 1440px: compact intrinsic width; 390px: equal-width segments. If three labels cannot fit at 44px minimum height with readable text, use the same labelled select presentation rather than clipping or shrinking targets. Selection must not submit or rerun work.

6. One filter pattern
FilterBar({label, filters: FilterDefinition[], values, onChange, resultCount?: number, pending?, onClear}).
Each definition has id, visible label, type (search|single|multiple), and options where appropriate. Use when narrowing the same dataset: capability on Models; person scope on Usage. Prefer “Capability: All” and “Person: Everyone” over contextless selected chips. Search label “Search”; placeholder may name the dataset.
1440px: search then labelled selectors in one wrapping row; applied filters below as removable chips; trailing “Clear filters”. 390px: full-width search, then “Filters (N)” disclosure containing the same controls. Applied chips remain visible and wrap, never create a second horizontal page scroll. Changes apply immediately; disclosure closes with “Done”. No backend queries are implied by this component.
Removing a chip announces its label and updates a polite results region; restore focus to the next chip or filter trigger. Hide counts if unknown; do not display fabricated zeroes. Debounce search only if required by existing fetch behavior. Invalid URL filter values fall back to known defaults without erasing unrelated parameters.

7. Per-page migration and copy proposals
Home: PageHeader “Home” / “Connect an app or choose what you'd like to do.” Keep capability links as navigation.
Models: “Models” / “Find a model for your app or request.” Capability uses FilterBar; consume valid inbound selection and preserve Back. Unavailable target: “This model is no longer available to your account” / “Browse available models.”
Workflows: “Workflows” / “Ready-made ways to create.” Treat purpose/input narrowing as filters if verified. Empty copy: “No workflows are available to your account yet. You can still explore models.” / “Explore models.”
Usage: “Your usage” / “Your requests and remote spend this month.” Owner-selected everyone changes title to “Everyone's usage” and description to “Requests and remote spend across your household this month.” Keep scope beside spend.
Engines and Services alias: same “Engines” header; normalize active navigation to Engines. Preserve existing editor actions, including Save, in their local context rather than promoting Save to a page action.
Design: “Design system” / “UI components and interaction states. Examples do not use live data.” Add the four patterns and narrow-view/focus/error examples.
Try, Library, Apps & keys, Status, Providers, Activity, Gallery, additional routes: do not prescribe actions or remove current controls until their anatomy is verified. Migrate shared header only after recording actual titles, scope, action semantics, URL state and empty/error behavior. Activity's proposed tabs require source confirmation.

8. Data, accessibility, removals
These four components need no new backend fields: titles/copy are static frontend configuration; selected values and actions are controlled frontend props. Current identifiers are unverified. Existing behavior evidence: Models capability/detail state at models-client.tsx:834–905; Usage person scope at usage/page.tsx:158–176,203–214. Exact catalog option fields, role field, person identifier and result count fields remain integration blockers; do not invent them or assume new backend work. No backend-derived counts/status should ship until their existing field and source line are verified.
One h1 per page; preserve shell skip link, focus restoration, visible focus and aria-current. Use reduced motion. Remove duplicated header bottom spacing, variant-specific header markup, filter-as-tab styling, redundant selected chips, and color-only scope signals only after equivalent behavior is preserved.

Completion limits
Broad app/components directory listings were denied for potential enumeration of excluded paths. Neither denial was retried, narrowed or bypassed; no fresh source files were read. Full inventory and per-route integration remain blocked. No patches, checks, shell, runtime/model APIs, browser or live calls. workspace_result: diff_bytes=0; SHA256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; checks=[]. Latest assignment get: not cancelled or revoked, no owner notes.
