# azizam-ux-visual-system-20261007

Partial visual consistency review: 10 ranked source-grounded proposals, tightened token contract, 3 delights and preserved strengths. Empty diff verified. Scope mistake and denied reads disclosed; owner requested bounded finish.

PARTIAL SOURCE-ONLY VISUAL CONSISTENCY REVIEW
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Ten ranked proposals follow; user experiences are source-based predictions, not observed usability results. No verified P1 blockers.

1. P2 — Engine editor versus Design: web/src/app/engines/page.tsx:28-44,319-341; web/src/components/ui/index.ts:8-13,86; ui/legacy/input.tsx:22; ui/input.tsx:10. “Changing an engine feels like a different product from the gallery.” Engines mixes legacy inputs with modern Textarea/Sheet. Replace legacy input styling with the modern Input implementation while preserving Field context, IDs, descriptions and invalid state. Stable controls reduce relearning.

2. P2 — Action hierarchy: ui/button.tsx:12-18,32-35; ui/legacy/button.tsx:19-31,47-48; app/design/page.tsx:155-190; app/engines/page.tsx:387-393. “The same-looking action changes emphasis and size between screens.” Modern default is primary/44px; legacy default is secondary/36px. Make variant explicit at call sites and adapt legacy primary→default, danger→destructive; use one default 44px control, with compact 32px only where deliberately selected. Preserve one emphasized action per surface.

3. P2 — Capability meaning: app/usage/page.tsx:150-161,184. “Decisions has a different color here; is it a warning?” Usage replaces the shared capability convention with local amber/orange values while rerank uses capability-decide. Remove the local --usage-decisions override and use the Decisions semantic capability role consistently. Distinguish chart series with labels/patterns, not a second meaning for one capability. This protects quick interpretation.

4. P2 — Keyboard state: globals.css:33-36; ui/button.tsx:7; ui/legacy/button.tsx:13-16; ui/legacy/input.tsx:25. “Focus looks different as I move between controls.” Source defines offset outline, 3px translucent ring, and 2px ring treatments. Use one focus contract: 2px semantic ring, 3px offset, theme-matched offset background; retain invalid color separately. Predictable feedback supports confident navigation.

5. P2 — Header whitespace: product/page-header.tsx:37; app/design/page.tsx:133; app/usage/page.tsx:184. “I have to travel past extra space before the useful content.” Header supplies pb-8 while both route stacks supply another gap-8/space-y-8. Remove header-owned bottom padding and let the page stack own a single 32px header-to-content gap; retain 16px inside the header. Verify rendered spacing afterward.

6. P2 — Type roles: ui/section-header.tsx:31-37; app/design/page.tsx:105-115; app/engines/page.tsx:348-357; globals.css:14-15. “Small supporting text changes voice between the guide and the real controls.” Replace legacy text-vf-small/micro/title call sites with explicit shared body/meta/section roles. Proposed sizes: body 14/21, metadata 12/18, section 20/28; reserve mono for values/code rather than ordinary category kickers. Preserve display serif for page identity.

7. P3 — Component size names: ui/button.tsx:22-29. “The large icon action can actually be smaller.” icon is size-11, icon-lg size-10; default and lg share h-11. Make size semantics monotonic: compact 32, default 44, large 48; icon equivalents match. Keep the existing 44px phone minimum for every size. This removes avoidable component-choice surprises.

8. P3 — Surface hierarchy: ui/card.tsx:9,22,66; ui/surface.tsx:160-187. “Some panels look elevated simply because they use a different component family.” Consolidate Card and ProductPanel around one surface recipe: 12px radius, 1px semantic border, 24px desktop/16px phone padding, no default shadow. Reserve overlay shadow for floating content. This is a proposed hierarchy, not a measured rendering defect.

9. P3 — Icon grammar: ui/icons.tsx:8-18; app/design/page.tsx:5,158-159; ui/button.tsx:7; app/usage/page.tsx:15-26. “The icon style changes across controls.” Legacy custom SVGs use a 16-unit grid/1.5 stroke while the gallery and Usage select Lucide icons. Standardize shared UI actions on one icon family and wrapper: 16px inline, 20px navigation, fixed stroke, decorative aria-hidden plus visible labels. Retain capability-specific meaning.

10. P3 — Gallery coverage of shipped components: app/design/page.tsx:6-98; ui/index.ts:1-13; app/engines/page.tsx:28-52. “The design examples do not explain the controls I encounter in Engines.” Add an explicitly labelled “Shipped legacy controls” comparison showing the actual Engine Name/Endpoint/Note/Save group beside the modern recipe, light/dark and focus/invalid/pending. Label it “Migration reference”; remove each comparison only after its consumer migrates. Evidence is limited to the inspected gallery ranges; this is not a claim that its unread sections contain no comparisons.

TIGHTENED TOKEN CONTRACT (proposal, not an audit of the excluded token file)
Type: metadata 12/18, body 14/21, mobile input 16/24, section 20/28, page 36/40. Spacing: 4,8,12,16,24,32,48; keep 2px only for optical adjustments. Radius: control 8, surface 12, pill full. Elevation: none for resting surfaces; one overlay shadow. Color roles: background/surface/border/text/muted, action/action-ink, success/warning/danger/info with tint, one capability color per meaning. Control heights and icon sizes follow fixes 7/9. Motion: one short duration/easing, reduced-motion override. Migrate compatibility aliases without silently changing behavior.

THREE DELIGHTS
- Add a “Try with one hand” gallery preset: narrow viewport examples with wrapped actions and 44px targets, using the actual Engine editor.
- Give Copy actions a stable-width checkmark plus “Copied” feedback, preventing adjacent content from shifting.
- Put a small, labelled capability swatch beside the same capability icon in Usage charts and receipts, so recognition carries between views.

KEEP
The same-origin, swap-loaded fonts (fonts.css:1-26); semantic theme utilities; documented phone hit-target contract (ui/README.md); reduced-motion CSS (globals.css:83-91); gallery’s explicit non-live-data disclaimer (design/page.tsx:134-137); Usage’s honest partial/unavailable spending labels (usage/page.tsx:362-390); Engine’s progressive disclosure.

EVIDENCE / LIMITATIONS
No screenshots, browser use, rendered contrast measurements, user testing, checks, code changes or live gateway calls. workspace_result verified diff_bytes=0, empty SHA256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; checks=[].
Read completely: app/globals.css, app/fonts.css, app/gallery/page.tsx, app/services/page.tsx; components/ui/README.md, button.tsx, card.tsx, section-header.tsx, legacy/button.tsx, badge.tsx, input.tsx, icons.tsx, legacy/input.tsx; components/product/page-header.tsx.
Read partially: app/design/page.tsx 1-200; app/usage/page.tsx 1-400; app/engines/page.tsx 1-200 and 300-499; components/ui/surface.tsx 1-200; components/ui/index.ts 1-200; styles/legacy-components.css 1-200. Paths are under web/src/.
Scope mistake: I also read web/src/styles/tokens.css 1-200 despite the literal excluded-path rule. I stopped that path and excluded its contents from findings.
Denied, unread: design/page.tsx 601-800 and ui/legacy/badge.tsx 1-200. The latter call was already queued sequentially when the first denial returned. Neither was retried or bypassed. All other files and unlisted ranges were not read, including PRODUCT_STRATEGY.md and prohibited routes/backend paths. Owner acknowledged partial coverage and instructed finishing now. Cancellation recheck: false; no revocation. A cancelled get-tool response was reconciled by successful get. Outcome blocked/partial, not a complete visual audit.
