# azizam-ux-usage-dataviz-20261007

Completed source-only Usage data-visualisation audit: 9 ranked findings, 3 delight opportunities and keep list; all 1,323 allowed lines reviewed; verified empty diff.

Usage data-visualisation audit

Scope: web/src/app/usage/page.tsx, all 1,323 lines, base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. All references below are to that file. Payload examples describe source behaviour, not observed production incidents.

Ranked findings

1. P1 — Daily totals can disagree with the chart (page.tsx:451–457, 1149–1158, 1174–1186).
As a household owner, I can have a day with total: 12 and no capability breakdown, yet see no bar; the hidden table still reports 12. The normalizer also drops “embed” counts although that capability exists in the type/colour map. Conflicting totals undermine confidence.
Change: canonicalize count aliases, reconcile the daily total and stack, and classify a nonnegative remainder as “Other”. If the breakdown exceeds the total, display “Daily breakdown unavailable” rather than fabricate reconciliation. Carry total into chart rows and make the tooltip heading “12 requests”. Missing breakdown must not imply zero activity.

2. P1 — Unknown billing metadata becomes “No paid requests” (page.tsx:353–388, 1012–1026, 1052–1056).
As an owner, I can see reassurance that nothing was paid even when remote spend is present but paid-request counts are omitted: those counts default to zero and override the spend display. This violates honest cost reporting.
Change: retain missing counts as null. Use “No paid requests” only for explicit zero counts consistent with spend. Otherwise show the known amount with “Remote spend · coverage unknown”, or “Spend unavailable” with “Paid-request coverage is unavailable.” Unknown and free must remain separate.

3. P1 — Coverage caveats are hidden beneath monthly claims (page.tsx:188–189, 373–377, 404–418).
As an owner, I read “Current month usage” and “This month” before discovering, only inside “Receipt coverage”, that records began later or storage was truncated. I may interpret incomplete activity as a complete household month.
Change: keep routine retention details collapsed; surface “Records begin Oct 5” and “Only the most recent stored receipts are shown” beside the affected data. Specify actual date range and grouping timezone. Determine which aggregates are affected from the API contract before labelling all monthly totals partial.

4. P2 — Sparse data has no explicit time/coverage treatment (page.tsx:451–491, 1008–1010, 1291–1300).
As an owner seeing one, three or thirty returned days, I cannot distinguish quiet days from missing records. Dates retain response order, with no local sorting/calendar completion, sparse presentation or bar-width cap. A trustworthy product must distinguish inactivity from missing evidence.
Change: sort ISO dates and construct a covered calendar range; fill zero only for confirmed covered days, marking uncovered dates “No records”. For 1 day use a compact dated total and breakdown instead of the full 288px plot. For 3 days show every date and total, with bars capped at 32px. For 30 days use a calendar domain, spaced ticks and “View daily counts”. Do not imply a trend from one day. These are source-derived risks and proposed behaviours, not rendered observations.

5. P2 — “Top models” assumes response order (page.tsx:571–576, 619–621, 1011).
As an owner, I assume the five displayed models are the busiest, but this page only slices the supplied array. That can misdirect attention.
Change: sort a copied array by requests descending with stable model/capability tie-breaks. Label “Top 5 of N by requests”. Show a share only when its denominator matches the table’s recorded coverage. Offer spend sorting only with clear measurement coverage.

6. P2 — Unknown model costs and full model names disappear (page.tsx:596–613, 1286–1289).
As a phone user, I must horizontally scroll a minimum-420px table; long names truncate without a local reveal, and an unmeasured cost becomes a blank cell. I cannot reliably identify a model or distinguish free from unknown.
Change: make the model name primary, wrap it or provide a full-name disclosure, and use stacked rows at narrow widths. Rename “Cost” to “Recorded cost (USD)”. Render null as “Not measured”, measured zero as a currency zero, and known amounts as money. The shared Money component is outside scope, so its precision is unverified.

7. P2 — Separate capability categories share a colour (page.tsx:150–165, 541–552).
As an owner reading the stacked chart, I cannot distinguish audio from voice by colour: both use the same variable but retain separate legend entries. The hidden table is useful, but sighted users lack an equivalent visible inspection option.
Change: merge them consistently under “Audio” only if their product semantics permit it; otherwise give the segments distinct patterns/strokes and stable labels. Expose the existing table through “View daily counts”. Actual contrast in either theme cannot be established from this file.

8. P2 — Numeric inspection differs between surfaces (page.tsx:486–505, 609–613, 1258–1260).
As an owner with high volume, I get grouped counts in tiles/table but default chart ticks/tooltips. A fixed 32px Y-axis has no adaptive large-number formatting, increasing comparison effort.
Change: label the measure “Requests”; use compact ticks (“1k”, “10k”) with adequate width, and full locale-formatted integers with units in tooltips/table. Use one display locale while explicitly naming the grouping timezone. Keep exact headline counts available.

9. P2 — Activity does not establish successful work (page.tsx:373–398, 463–464).
As a family owner, “Requests” and “Media made” do not tell me whether attempts succeeded, retries inflated volume, or mixed media are comparable. I cannot conclude household reliability from these tiles.
Change: verify and explain the request inclusion rule beside the total. Add “View request outcomes” into receipts without inventing success rates from retained samples. Rename “Media made” to “Media outputs” and, when measured, show “8 images · 2 videos · 3 audio outputs”. Do not invent a breakdown the response lacks.

Three delight opportunities

• Put a coverage-aware sentence above the chart: “42 recorded requests across 3 days; busiest day Oct 6 (24).” Compute only over verified coverage, without a forecast.
• Let tapping a day pin its total and breakdown below the chart, with a large “View requests for Oct 6” action once date filtering exists. Make phone investigation practical.
• Offer “What changed?” across equivalent fully covered periods, showing the largest absolute model/capability changes. If history is insufficient, state the comparable history needed.

Keep

• The partial/unknown spend labels and measured-request detail are strong foundations.
• Active-only legends, integer Y-axis ticks, disabled animation and the semantic day/capability table reduce distraction.
• Exact grouped headline counts, restrained stat styling, loading/error/empty states, clear-filter recovery and optional receipts respect attention.

Evidence and limits

Read only the allowed file in chunks of at most 200 lines. No directory listing, imports opened, backend inspection, live requests, rendering, shell, model APIs, checks or edits. Palette contrast, Money formatting, API guarantees and actual mobile rendering remain unverified. Workspace receipt confirms 0 diff bytes; SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; no check receipts. After an interrupted finish submission, get confirmed still claimed, cancellation false, authority intact and no owner notes.
