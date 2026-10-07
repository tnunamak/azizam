# azizam-ux-spec-money-20261007

Report-only honest-money design spec covering six surfaces, evidence-qualified charge/price states, precision, paid-action disclosure, mobile layout, props and backend gaps. Empty diff; no checks or live billing claims.

Honest money: buildable UI specification

Purpose
Before a request: “Could this charge a provider, and what do we know about its price?” Afterward: “What charge was recorded, and how complete is that evidence?” Never turn missing data into zero, a catalog rate into a request total, or a gateway estimate into a provider bill.

1. Shared state model and exact copy
Keep four independent concepts: price/rate; request charge; aggregate coverage; execution/cap state. A failed, cancelled or rejected request can still have a charge.

Charge states:
• local_no_charge: “Local · no provider charge.” Detail: “No external provider charge is recorded for this execution. Running your own hardware may still cost money.” Require recorded executed-route/no-external-charge evidence, including workflow children/fallbacks; selected-model locality alone is insufficient.
• measured: “Recorded charge · USD {amount}.” Detail: “Amount reported for this request; provider billing may differ.” Preserve source and time if supplied. A bare numeric cost is reported, not automatically final.
• actual: “Verified provider charge · USD {amount}.” Only a reconciled actual amount with explicit source/status supports this stronger label.
• estimated: “Estimated charge · USD {amount}.” Detail: “An estimate, not a final provider charge.” Pending settlement: “Awaiting verified charge.”
• unknown: “Cost not recorded.” Detail: “This does not mean free.” Never omit this row from a receipt.
• included: “Included in {named plan} · no additional provider charge,” only with explicit applicable billing evidence. No inspected field proves inclusion. Hide this branch until supported; access permission is not billing inclusion.

Before-run price states:
• priced: “Paid · {provider}” and each known rate.
• unpriced: “Paid · price unavailable. This request may incur a provider charge.”
• unknown: “Price not reported. This request may incur a provider charge.”
• configured local without execution proof: “Local model selected · provider charge not confirmed.”
• local-only route with explicit no-paid-fallback guarantee: “Local · no provider charge.”
• estimate: “Estimated for this request: USD {amount/range}” plus “Final charge may differ.” Show assumptions and covered steps in expandable details. Never derive a total from output text length or silently assume all workflow steps are priced.

Cap is an overlay, not a charge state:
• confirmed blocked: “Spending cap reached. This paid request cannot run.” Disable execution; owner action “Review provider limits,” member copy “Ask the gateway owner to review the limit.”
• spent ≥ configured cap without enforcement evidence: “Recorded spend has reached the configured cap.” Do not claim the gateway will block requests.
• incomplete spend: “Remaining budget unavailable while charges are unrecorded or estimated.”
• reset time absent: omit countdown. Null cap: “Cap not reported”; “No cap set” requires explicit no-cap evidence.
A cap is not a quote or guarantee that the next call fits.

Loading: “Loading price…” / “Loading recorded charge…”; error: “Price could not be loaded” / “Recorded charge could not be loaded,” with “Retry.” Keep previously loaded values visibly stale on refresh failure.

2. Formats
Use explicit USD throughout; do not infer currency from browser locale or silently convert. Unsupported/missing currency: “Currency not reported,” without a currency symbol. Existing *_usd and USD-denominated rates establish USD.
Summary amounts: USD 12.34; nonzero below one cent: “< USD 0.01.” Explicit zero: “USD 0.00,” never “Free.”
Receipt amounts/rates: 2–6 fractional digits, preserving meaningful supplied precision in “Exact recorded amount.” Positive below 0.000001: “< USD 0.000001,” never rounded zero. Preserve decimal strings; do not manufacture precision from binary floats. Reject NaN, infinity, negative or arbitrary text as ordinary charge values; credits require a separate supported type.
Rates: “Input: USD 0.15 / 1M tokens” and “Output: USD 0.60 / 1M tokens.” Use “USD {n} / image” or “/ minute” only with that explicit unit. Unknown output rate: “Output price unavailable,” not zero. Do not parse an opaque catalog price string into a numeric quote.

3. Placement and responsive layout
1440 px: use existing page shell, 24 px content gaps, tabular numbers. Models rows: name/capability | location/provider | price state and rates | Open in Try. Details repeat rates beside the action. Do not make the user open Details to discover paid status.
Try: main form plus result panel. Immediately above every Run/Retry action, place a full-width ChargeDisclosure: selected model/provider, price/estimate, warning or cap state. Example:
“Paid · {provider}
Input: … · Output: …
Total before running: unavailable.
[Send paid request]”
Unknown-price CTA: “Send request · may incur charges.” Confirming a selection never executes it. Changes to selection, inputs or workflow invalidate the old estimate; show “Estimate needs updating.” Retry is a new potentially charged action with the same disclosure.
Usage: summary card “Recorded remote spend,” amount plus “Partial” or “Coverage unknown” on the same card. Adjacent sentence: “Charges recorded for {n} of {m} requests,” when both counts exist. Do not call a known subtotal the bill. Every model row and receipt has an explicit money state.
Activity: charge sits immediately below outcome in the receipt and alongside outcome in the list. Keep outcome and money independent; never present parent-plus-child costs as a sum unless non-overlap is established.
Providers: collapsed provider card always includes monthly recorded-spend state, configured cap and pending-estimate warning. Expanded card separates “Recorded spend,” “Configured caps,” and “Charges awaiting verification.” Show estimated and actual amounts separately in reconciliation rows; actual replaces estimate in a total only under a supported accounting contract. Unknown spend has text, not a zero-length progress bar.
Home: remain task-first. Navigation-only capability tiles need at most a supported paid/local badge and “View models and prices”; they are not quotes. Put money disclosure directly before “Check connection” if it can execute a provider request. The inspected connection response does not establish a charge-free check, so never label it free. If test semantics are unknown: “This check may send a model request and incur a provider charge.” Recent media previews do not need guessed prices.

390 px: 16 px outer padding, single column, 12–16 px gaps. Models/Usage/Activity rows become cards: title/time; outcome; charge state/rates; secondary model/source. Preserve price text, wrap units, and never horizontally scroll essential money information. Try disclosure and full-width 44 px CTA stay adjacent in document flow; no sticky bar may hide warning text. Receipt sheet uses full width and scrolls vertically. Provider cards stack spend, cap, then action. Same evidence and copy at both widths.

4. Component API
MoneyValue({ amountDecimal, currency, format: "summary"|"receipt"|"rate", unit?, exactDecimal? }): formatting only, no free/local inference.
MoneyState({ phase: "before"|"after", state, amountDecimal?, currency?, provider?, rates?, estimateRange?, evidence?, detailId?, density }): discriminated union; numeric states require valid amounts, local/included require qualifying evidence.
ChargeDisclosure({ money, actionLabel, capState, estimateStatus, onRun, disabledReason? }): labels and describes action; confirmed cap blocking disables it.
SpendSummary({ amount, coverage: "complete"|"partial"|"unknown", measuredCount?, totalCount?, includesEstimates, period?, timezone?, cap? }): no sum of receipt rows.
Evidence includes source, recordedAt, requestId and billing basis when known; never fabricate them. Suggested props are an adapter contract, not claims that backend fields exist.

5. Data mapping and gaps
Paths below relative to web/src/app.
• Models: models/models-client.tsx:233–249 reads row.locality, cost.kind, cost.price; provider/engine labels at 252–273. These support catalog disclosure, not executed-route proof.
• Try: try/page.tsx:494–538 carries price, locality, provider; workflows:557–598 currently set empty price. try/result-cost.ts:1–28 reads x-litellm-response-cost and cost/remote_cost/measured_cost, including amount_usd. Replace string-returning adapter with validated state; locality fallback at 15 is not execution evidence.
• Usage: usage/page.tsx:1005–1061 maps remoteSpendUsd, spendMeasuredRequests, spendTotalRequests, paidRequests, unknownPaidRequests and coverage fields; :1119–1190 maps receipt/model costUsd. Preserve missing paid-count fields as unknown rather than defaulting them to zero and displaying “No paid requests.” Render coverage independently of paid-count availability.
• Activity: activity/requests.tsx:26–49 has open-ended receipt data but no declared charge contract. Needs backend field receipt.money (state, amount_usd, source, recorded_at, executed locality/no-charge evidence); do not assume arbitrary keys are present.
• Providers: providers/page.tsx:240–288 maps input/output USD-per-million prices; :418–478 maps spend/caps/status and pending counts; :481–507 maps estimatedUsd, actualUsd, decimal text, status and costSource. :594–626 currently treats unavailable spend as 0% and null caps as “No cap set”; remove those inferences.
• Home: home/home-api.ts:75–83 ConnectionResult contains outcome/latency/model/request_id, no charge; home/page.tsx:490–521 offers Check connection. Needs backend preflight/check semantics and money evidence for an honest charge-free claim.
• Needs backend fields: preflight estimate (amount/range, USD, assumptions, expires_at, covered steps); executable local-only/no-paid-fallback evidence; explicit included-plan billing basis; cap enforcement state/scope/reset_at; aggregate billing basis and completeness; actual settlement source/status; accounting period/timezone. Until supplied use stated unknown copy, not invented values.

6. Accessibility, removals and acceptance
Use text labels rather than color alone; connect disclosure to CTA with aria-describedby. Polite live announcements for estimate/settlement changes; assertive alert only when execution becomes blocked. Accessible cap text must state amount, cap and uncertainty; omit determinate progress for unknown totals. Exact amounts available via keyboard/tap disclosure, never hover alone.

Remove blank cost cells, phone-hidden charges, raw arbitrary cost strings, local-selection-equals-free logic, “No paid requests” from absent counters, unconditional zero progress, and bill/finality language unsupported by evidence. Keep Usage’s existing partial-coverage distinction and Providers’ reconciliation detail.

Acceptance examples: null ≠ zero; 0.0000001 stays nonzero; failed request with recorded charge retains it; local-selected/remote-executed never reads no-charge; missing counters never imply no paid usage; included is impossible without evidence; every execution/retry has visible disclosure; 390 px keeps all essential money information.

Evidence/limits
Source-only at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba, reusing completed Usage/Activity and Models/Workflows/Try reports. Bounded allowed source reads, not exhaustive repository grep; no excluded paths, shell, runtime, live calls, checks, edits or provider billing verification. Proposed backend fields are explicit requirements, not confirmed availability. Pre-finish get: active, not cancelled/revoked, no owner notes. workspace_result: diff_bytes=0; checks=[].

