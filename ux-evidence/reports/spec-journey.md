# azizam-ux-spec-journey-20261007

Buildable Where it stopped spec for Usage, Activity, Try and Home: conservative evidence mapping, exact copy, responsive layouts, typed props, five-field help-copy allowlist and explicit backend prerequisites. Empty diff; no checks or runtime claims.

Where it stopped — reusable request journey specification
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

1. Purpose and boundary

Answer “What is recorded about this request, and what should I share for help?” before technical details. Four steps always appear in order: Your app → Gateway → Engine / Provider → Model. This is an evidence summary, not a distributed trace or a general health verdict. “Verified” means the accompanying specific event is recorded, not that the entire stage succeeded.

Ship a conservative version with current data. Granular stage events/timestamps are a separate backend prerequisite; absence must never become a fabricated checkmark, failure, or timing.

2. States and exact copy

Step states:
• Verified: check icon + “Verified” + one factual event, e.g. “Receipt recorded by the gateway.”
• Failed: error icon + “Failed” + a recorded failure event.
• Not observed: hollow circle + “Not observed” + “This receipt has no evidence for this step.”
Every step has a timestamp line: “Recorded at [local date, time, timezone]” only for an actual stage event; otherwise “Time not recorded.” Overall receipt time is separately labeled “Receipt time.” Never subtract latency from receipt time or distribute total duration across stages.

Section heading remains “Where it stopped.” Summary variants:
• Explicit stage failure: “Failure recorded at [stage].”
• Generic failed/error/timed_out: “The request did not complete. This receipt does not identify where it stopped.”
• Completed: “The gateway recorded this request as completed.”
• Accepted/running/in_progress: “No final outcome yet. The latest receipt says [Accepted / In progress].”
• incomplete/stale_incomplete/missing/unknown status: “The final outcome was not recorded.”
• Cancelled: “The request was cancelled. The stopping point is not recorded.”
• Disconnected: “The connection ended. The stopping point is not recorded.”

Add persistent explanatory text: “Not observed does not mean failed. This shows recorded evidence for this request.”

Current receipt adapter:
• Any genuine gateway receipt: Gateway Verified, event “Receipt recorded by the gateway.” Keep its stage timestamp missing unless its semantic meaning is explicitly documented.
• rejected: Gateway Failed, “The gateway rejected this request,” following the current receipt semantics.
• backend_failed: Engine / Provider Failed, “The receipt reports an engine or provider failure.” Model stays Not observed; this status does not distinguish a model failure.
• success/succeeded/ok: show the completed summary; do not mark all four stages Verified.
• All other statuses: no stage attribution from HTTP status, free-text warnings, model name, duration, or receipt presence alone.
• Missing receipt: all steps Not observed unless the surface has a specific directly observed event. “No request receipt is available.”

Multiple recorded failures must remain visible; say “Failures recorded at multiple steps” rather than guessing causal order. Conflicting observations produce “The recorded evidence is inconsistent,” retaining evidence without choosing a culprit.

3. Surface integration

Usage and Activity: replace the existing outcome-explanation box with this component above metadata/Technical details. Select receipts by request ID and derive the displayed record from refreshed data; keep previous evidence during fetch failures. Refresh never resubmits a request. Show “Couldn’t check for a newer outcome. Showing the previous receipt.” Only show “Check latest outcome” where a real adapter supports it.

Try: place after the result heading and before output media. “Your app” has subtitle “Try in this browser”; a real received HTTP response can verify the browser’s receipt of that response, not the user’s external app. Preserve response/receipt metadata on non-OK responses before raising the UI error. This is a frontend change to the current throw path, not a claim that failed responses always contain a receipt. HTTP success alone must not mark asynchronous work completed. Keep workflow job outcome separate from request acceptance. Verified receipt links remain separate from stage verification.

Home: place immediately below “Check gateway with this key.” Keep “Checks the gateway and key; it cannot test [selected app].” Your app stays Not observed: “This check did not test your app.” When ConnectionResult.ok is true, Gateway Verified: “Gateway responded with this key.” Engine / Provider and Model remain Not observed absent receipt evidence. A model string is not proof it ran. ok=false or a catch error does not identify a failing layer: “We couldn’t complete the gateway check. Your key may still be valid.” Do not claim a successful conversation or healthy gateway overall.

No automatic rerun/retry of potentially chargeable requests. The component may refresh evidence or copy details only.

4. Responsive layout

1440 px viewport, main-content container ≥720 px: bordered card, 24 px padding. Top row has heading/summary at left and receipt time at right. Four equal-width step columns beneath, with light neutral connectors; each contains stage label, icon+state, wrapped evidence, timestamp. Model identifiers occupy a separate wrapping metadata line. Footer: “Preview help details” disclosure, then “Copy help details” button, then optional receipt link/refresh. Expanded preview spans the card.

Receipt sheets remain their existing approximately 512 px desktop width; use the vertical layout there rather than squeezing four columns. Use container width, not viewport width, to switch at 720 px.

390 px viewport: full-width sheet/card, 16 px inner padding, no horizontal scrolling. Heading → summary → receipt time → four vertical rows with a 20 px icon rail and wrapping text column → model metadata → preview → full-width copy button. Each step includes its timestamp line even when missing. No side-by-side actions that truncate. Long IDs wrap; full values remain selectable. Keep all four steps visible; only help preview and existing technical details collapse.

5. Component API

RequestJourney props:
• surface: usage | activity | try | home
• requestId: string | null; receiptTime: normalized timestamp | null
• outcome: completed | failed | rejected | accepted | in_progress | cancelled | disconnected | timed_out | unknown
• requestedModel / servedModel: string | null
• steps: fixed four entries with id (app/gateway/provider/model), state (verified/failed/not_observed), evidenceCode from a reviewed finite vocabulary, observedAt: timestamp|null, evidenceSource (receipt/direct_browser)
• failureLayer: app | gateway | provider | model | multiple | unknown | none
• receiptHref?: verified or established receipt destination
• refreshState?: idle | loading | error; onRefresh?: callback
• copyState/onCopy handled by component; no raw receipt, request body, arbitrary error message, or key props

Adapters own evidence mapping. Validate contradictory props, unknown enum values and invalid dates; fall back to unknown. “none” means no recorded failure with a completed outcome, not proven health. Do not enable unsupported callbacks.

6. Data provenance and prerequisites

Exact paths abbreviated here only: U=web/src/app/usage/page.tsx; A=web/src/app/activity/requests.tsx; T=web/src/app/try/page.tsx; H=web/src/app/home/home-api.ts.

• Usage IDs/time/models/status: U:82–96 (UsageReceipt). Normalization U:1132–1158 uses request_id/id, created_at/createdAt/accepted_at_ms, status, requestedModel/servedModel. Preserve the true source; replace synthetic requestId fallback “receipt” with null before sharing. Do not interpret ambiguous numeric timestamp units without a documented adapter.
• Activity: A:26–44 receipt.request_id, requested_model, resolved_model, status, latency_ms; record.created_at. Existing status semantics A:61–145. A:439–480 displays model/ID/time. Person/app/source labels are unnecessary for help copying.
• Try: T:119–136 Result has receiptId, elapsedMs, request, job; no receipt timestamp or typed failure layer. T:270–288 extracts response ID; T:292–338 verifies receipt existence but discards receipt payload. T:255–264 throws away non-OK response context. Read only the selected model field into the adapter; never pass Result.request or body to the component.
• Home: H:72–79 ConnectionResult has ok, latency_ms, reason, path, model, request_id; no timestamp or failure layer. Home page:543–565 consumes it; :587–590 explicitly limits app verification.

Needs backend fields: journey.steps[].stage, event_code, state, observed_at with defined UTC format/semantics; journey.failure_layer with documented attribution; receipt_recorded_at where absent. External-app execution/response observation requires genuine app telemetry, not inference from receipt existence. Named provider identity is optional and not currently established by these receipt contracts. New fields are proposed, not observed. Browser observation timestamps could be instrumented prospectively and labeled “Observed by this browser”; they cannot backfill historical receipts.

7. Copy help details and accessibility

Visible disclosure label: “Preview help details.” Exact five-line payload:
Request ID: [real ID / Not recorded]
Time: [receipt ISO 8601 UTC / Not recorded]
Model: [Served: X; Requested: Y / Requested: Y / Not recorded]
Outcome: [normalized human label]
Failure layer: [explicit layer / Not identified / No failure recorded]

Preview and clipboard must use the identical allowlisted object. Plain text only; bound lengths, reject control characters, and render as text. Exclude bodies, prompts, generated output, keys, headers, URLs, raw errors, warnings, person labels and arbitrary metadata. Copy caption: “Includes only the details shown above. Review before sharing.” Success: “Help details copied.” Failure: “Couldn’t copy. Select and copy the details below.” Never automatically send or upload.

Use a labeled section and ordered list; icons decorative, status always textual. Keyboard-operable disclosure, visible focus, minimum 44 px actions, accessible datetime values, polite copy/refresh announcements. Preserve focus on refresh; no auto-opening dialogs, animated progress connectors, or color-only meaning.

Remove duplicated outcome prose, generic “Open Technical details” recovery, key-blaming catch copy, and inferred all-green paths. Retain raw technical disclosure separately, model distinctions, existing outputs and valid receipt links.

Verification/limits: source-only design; reused terminal Home, Usage/Activity and Models/Workflows/Try reports and bounded scoped reads (≤200 lines). One nonexistent home/api.ts lookup returned file-not-found; actual home-api.ts was identified from imports. No excluded paths, backend inspection, browser, runtime, checks, edits or writes. Empty diff verified: 0 bytes, checks=[]; no cancellation/revocation or owner notes at final read.
