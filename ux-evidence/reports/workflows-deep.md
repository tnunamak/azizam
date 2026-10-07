# azizam-ux-workflows-deep-20261007

Completed six-file, source-only workflow journey audit: eight ranked findings, three delight opportunities, preserved strengths, and explicit running-state coverage limits. Verified empty diff; no tests or execution.

Workflow journey audit

Evidence: all six allowed files read in full at assignment base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Source-derived findings below are not live reproductions. Paths beginning workflows/ or try/ are under web/src/app/.

Ranked findings

1. P1 — Result cost can contradict reported charges. try/result-cost.ts:15–25.
As an owner, I can receive “Local · no provider charge” even when the response payload contains a positive cost: the locality fallback returns before payload cost is inspected. That undermines the explicit promise of honest cost reporting.
Change: normalize and validate header and payload charges first, then apply a clearly qualified locality fallback. A positive reported amount must remain visible even for Local; when charge data is absent, say “Provider cost not reported” rather than asserting zero. Reject negative/non-finite payload amounts and format all numeric values consistently. This is a concrete branch-order issue; whether a current provider supplies that combination was not checked.

2. P1 — Longer prompts replace the field while someone types. try/workflow-inputs.tsx:316–339.
As a household member writing a detailed prompt, crossing 80 characters changes my Input into a Textarea; deleting back across that boundary changes it again. React changes the element type, so the editor remounts, threatening focus/caret continuity at exactly the moment I am concentrating.
Change: select the control from field purpose/schema, never the current text length. Render prompt-like strings as an auto-growing Textarea from the first character; retain one DOM element and caret throughout entry. This avoids making ordinary writing feel broken.

3. P1 — Unavailable workflows retain the same primary promise to run. workflows/page.tsx:326–345, 401–420, 616–623.
As a first-time user, I see an unavailable badge beside “Run in Try,” while the explanation is buried under Technical details. I must change surfaces to discover whether my choice can work, making responsibility for failure ambiguous.
Change: use availability.available when supplied and an explicit status mapping otherwise. For definitively unavailable entries, show the reason beside the card action and offer “Why unavailable?” opening that explanation. Keep “Open in Try” as a secondary inspection path if useful; do not imply readiness. For unknown states, say “Availability unknown.” Keep idle distinct from unavailable rather than applying a blanket disable rule.

4. P2 — Media recognition and acceptance disagree, silently losing outputs. try/workflow-media.ts:32–111, 157–161, 201–207, 223–233.
As someone whose workflow returns a JPEG data URL or inline audio/video, I can receive no media item even though the parser recognizes that media kind: safeMediaUrl accepts only the PNG data prefix. I cannot tell a display limitation from generation failure.
Change: return structured rejected-output reasons alongside accepted media. Render “Output received, but this format cannot be previewed” with a safe supported recovery action. If inline formats are intentionally supported, allow only explicit reviewed MIME types with size limits; preserve the existing scheme restrictions. Do not silently broaden URL acceptance. Actual provider payload prevalence is unverified.

5. P2 — Guided inputs speak implementation language and expose irrelevant controls. try/workflow-inputs.tsx:132–162, 184–212, 478–502.
As a family member, I am told that controls update JSON, see raw names/types and “Writes …,” and must scan ignored fields I cannot use. The form asks me to understand request plumbing before stating what I need to provide.
Change: put accepted required inputs first, followed by useful optional controls. Move ignored/unsupported fields and mappings into “Advanced input details.” Replace the introduction with “Add what this workflow needs. Optional settings already have defaults.” Provide human-readable labels with the exact API name in advanced details. Render “Upload image” at the real picker location instead of a disabled placeholder instructing me to search below; preserve the advanced JSON path for builders.

6. P2 — Catalog selection hides time and sometimes the actual purpose. workflows/page.tsx:160–170, 305–349, 444–451.
As a phone user, I may see an empty purpose paragraph when the description contains technical terms, and typical duration is available only inside Technical details. I cannot quickly decide what the workflow does or whether I have time for it.
Change: require a short plain-language purpose in catalog metadata, with a truthful fallback such as “Create an image” only when capability supports it. Add a compact “Needs: image · Produces: video · Usually ~30 s” line using existing inputs, outputs and typical_seconds. Omit unknown facts; never infer a price or promise a completion time.

7. P2 — Optional choices cannot return to an unset state. try/workflow-inputs.tsx:249–293.
As a builder comparing defaults, once I select an optional enum I cannot clear it in guided controls. An absent boolean also looks “Disabled,” although omission and explicit false can have different server semantics.
Change: give optional enums “Use workflow default,” which removes the property; provide “Default / On / Off” for optional booleans when omission is meaningful. Show the effective known default, or “Default not specified.” Keep required choices explicit. This preserves user choice without forcing JSON editing.

8. P2 — Receipt is presented as an identifier rather than a useful destination. try/receipt-link.tsx:9–29; workflows/page.tsx:729–737.
As a household member reviewing a result, I see a shortened code as the receipt link, while the catalog history correctly says “View receipt.” Copying the nearby icon gives only an ID, so it is not a ready-to-open link for an owner helping me troubleshoot.
Change: use visible “View receipt” consistently, retaining the ID as secondary metadata. Add “Copy receipt link” separately from an advanced “Copy receipt ID”; do not imply the link grants access or automatically share it. State on the destination what the receipt can explain, using verified data only.

Three delight opportunities

• Outcome-first preview: beside each catalog purpose, offer a clearly labeled example input/output pair and “Use these example inputs.” Let users understand the transformation before investing in their own upload; never submit automatically.
• Calm run narrative: when lifecycle data exists, show “Queued → Running → Output ready,” elapsed time and a plainly labeled typical-time estimate. Preserve the request ID so a temporary connection problem can say “Checking this run again” instead of encouraging a potentially duplicate paid run. This is a proposal, not a verified lifecycle deficiency.
• One useful next step: after output, offer “Make another with these inputs,” preserving editable settings; show Download and View receipt together. Make experimentation feel continuous without concealing costs or automatically rerunning.

Keep

Purpose-led grouping; capability filters and Clear filters recovery; distinct catalog/recent-output loading and error states; mobile input summaries; 44px action targets; required/range/type validation; advanced request examples; explicit ignored-input explanations; accessible receipt-copy feedback; recent output links to Library and receipts; restricted media URL schemes and canonical job routes.

Limits and completion

The allowed files expose catalog, input helpers, media/job parsing, receipt links and cost formatting. They do not expose the Try controller, polling loop, running UI, result renderer or receipt page: cancellation, retry safety, refresh recovery, persistence, failure attribution and final receipt content remain unverified. No imports followed, excluded paths read, product-strategy document opened, tests/checks run, live requests issued or files changed. No denied/cancelled reads occurred. Pre-finish get showed active claim, no cancellation/revocation and no owner notes. workspace_result verified diff_bytes=0 and empty-diff SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855. Review completed, not owner acceptance.
