# azizam-ux-library-20261007

Completed source-only Library/Gallery UX audit: eight ranked findings, three delight opportunities, strengths to retain. Empty diff verified; no tests or live validation.

Library/Gallery heuristic UX audit
Revision: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Verdict: Library has a credible, calm foundation, but its default scope and failure explanations fall short of “confidently show my family.” Gallery is a component-gallery relocation screen, not another media surface. These are source-derived hypotheses, not observed user behaviour.

Ranked findings

1. P1 — Personal-library expectations change silently.
Surface: Library; web/src/app/library/page.tsx:742–760, 797–817, 967–973, 1252–1265. Strategy: docs/PRODUCT_STRATEGY.md:64–67.
“As the owner, I open Library to show something I made and land in Everyone’s. Clearing filters after choosing Mine returns me to that broader view; my Delete action also disappears.”
The UI defaults owners to instanceView, while the strategy promises an owner-scoped personal library. This undermines privacy expectations during casual family use; it is not evidence of an authorization bypass.
Change: default to Mine for every role and send explicit scope=me. Keep scope separate from content filters so “Clear filters” never broadens it. Offer an explicit owner-only “Gateway media” switch with persistent text “Showing everyone’s media”; label its return action “Back to my library.” Clarify the product-policy decision before retaining broader owner access.

2. P1 — Unavailable media does not explain the next safe step.
Surface: Library; web/src/app/library/page.tsx:211–236, 451–475, 1043–1054.
“My creation says ‘Preview unavailable.’ Retry may silently fail, and I cannot tell whether the file is missing, the preview expired, or the service is unavailable.”
The source carries unavailable_reason but never displays it; retry rejection is swallowed. This repeats the exact failure-attribution problem in the brief.
Change: distinguish a failed preview from unavailable media using verified response reasons. Show “We couldn’t load this preview. Your file may still be available.” with “Retry preview” and an independently attempted Download. For confirmed missing files, say “This file is no longer available” and retain prompt/receipt access. While retrying show “Retrying…”, then an inline reason on failure. Never imply a file is safe or deleted without evidence.

3. P2 — Filter changes temporarily mislabel existing results.
Surface: Library; web/src/app/library/page.tsx:848–877, 1006–1033, 1098–1110.
“I choose Audio or Mine but can still see the previous results under my new selection, with no ‘updating’ explanation.”
placeholderData retains the old list, while isPlaceholderData/isFetching do not produce updating feedback. A scope change is especially sensitive to this ambiguity.
Change: keep prior results only for same-scope content filters, mark the region aria-busy, and show “Updating results…” above it. On scope changes, replace the list with loading placeholders until the new scope resolves. On failure say “Couldn’t update results. Showing the previous results.” Keep the selected filters visible as removable chips on phones.

4. P2 — Recorded model provenance disappears.
Surface: Library; web/src/app/library/page.tsx:259–264, 629–636, 718–731, 1272–1280.
“I can see a technical model name in the filter, but the item itself may show no model at all if it is no longer in the catalog.”
modelDisplayName returns an empty string despite a recorded model identifier. Honest provenance should survive catalog changes and advanced details should remain inspectable.
Change: display the friendly name when available, otherwise the recorded model ID. In Details show both “Model” and “Model ID”; use “Not recorded” only when the asset lacks a model. Use the same friendly-name-plus-ID treatment in filters.

5. P2 — The phone puts finding and saving behind avoidable scrolling.
Surface: Library; web/src/app/library/page.tsx:976–1006, 1154–1181, 1209–1212.
“To find something on my phone I first open Filter to discover Search. Opening a long-prompt image repeats the entire prompt in the heading and body before I can download it.”
This spends member attention on controls and duplicated text rather than their creation.
Change: keep Search prompts and kind chips above the grid on phones; collapse only model/date/source. Use a two-line drawer title, display the full prompt once in an expandable Prompt section, and place Download plus Copy prompt directly below the preview in a sticky action row.

6. P2 — Download wording promises the wrong action for workflows.
Surface: Library; web/src/app/library/page.tsx:240–254, 1186–1192, 1245–1250.
“‘Open in ComfyUI’ downloads a file rather than opening the app. ‘Download ready’ appears after a link click, without confirming that my device saved anything.”
The product should describe actions precisely.
Change: rename the workflow action “Download workflow” with “Import this file into ComfyUI.” Use “Download requested. Check your browser’s downloads.” after dispatch, and preserve “Preparing…” while retrieving the link. Do not claim successful local saving without evidence.

7. P2 — Several audio clips can play together.
Surface: Library; web/src/app/library/page.tsx:267–378, 613–620.
“I press Play on a second audio card and the first can keep talking; opening the drawer can add another player.”
Each card owns an independent audio element with no visible coordination, making a family browsing session unexpectedly noisy.
Change: when any Library player starts, pause the previous player, including drawer playback. Pause card audio when its drawer opens or filters remove it; keep the explicit 44-pixel Play/Pause control.

8. P3 — Delete copy leaves recovery ambiguous.
Surface: Library; web/src/app/library/page.tsx:1348–1356.
“‘This removes the item from your library’ does not tell me whether I can undo deletion.”
A low-risk-feeling family surface still needs honest consequences. Backend permanence was not reviewed.
Change: establish actual retention semantics before choosing copy. If unrecoverable: “Delete this item? You can’t undo this. Copies you’ve downloaded won’t be deleted.” If recoverable: name the recovery window and provide Undo. Use a short item title/thumbnail rather than the full prompt in the confirmation heading.

Three delight opportunities

• Library: a private “Favourites” shelf, explicitly never shared, lets family members find the few creations they love without remembering prompts.
• Library drawer: “Showcase” opens an uncluttered full-screen image/video view with previous/next controls; no owner names, technical metadata or adjacent creations appear until requested.
• Library provenance: a compact “Made with…” story combines recorded model, date, source and receipt; “Use this prompt” can prefill creation without submitting a paid request.

Keep

Keep explicit media-kind badges, real duration labels, manual audio playback, lazy preview work, full-card opening targets, and progressive disclosure of workflow details. Preserve separate first-load versus pagination errors, retained content after refresh failure, searchable prompts, deep links and receipts. Keep deletion behind the overflow menu and confirmation: shared ConfirmDialog retains the target on failure and blocks dismissal while pending (web/src/components/ui/confirm-dialog.tsx:68–94). Gallery’s honest “component gallery has moved” wording should remain a developer compatibility page (web/src/app/gallery/page.tsx:4–13); do not present it as a second family-media destination.

Limits and verification: inspected both route files, PRODUCT_STRATEGY and the relevant shared Sheet/ConfirmDialog source. No excluded files read; no browser rendering, live calls, user validation, accessibility measurement or backend isolation verification. Tests/checks not run. No edits. workspace_result verified diff_bytes=0 and no checks. Before finish, cancellation/authority flags were clear and owner_notes was empty. One initial read returned “user cancelled”; get confirmed no cancellation and one same-call retry succeeded.
