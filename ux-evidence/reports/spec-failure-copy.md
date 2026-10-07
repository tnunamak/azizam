# azizam-ux-spec-failure-copy-20261007

Completed canonical observable-failure design spec: 18 persona copy rows, non-failure states, component/data/layout/accessibility contract and cited integration points. Empty diff; no execution. Exhaustive repository coverage is not claimed.

Canonical observable-failure specification
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Report only.

Purpose
Answer “What happened, who can help, what next?” without asking members to diagnose infrastructure. “Next step belongs to” describes responsibility for recovery, never proven fault. Show “Cause not identified” unless a structured observation actually identifies the layer. A failed request does not establish a broken gateway, model, app or workflow.

Evidence keys (paths relative to web/src/)
E = lib/error-copy.ts:14–102: code, status, TypeError and existing recovery mapping.
R = app/activity/requests.tsx:26–49,60–157: receipt.status, request_id, warnings, metadata.status_code / status_code, created_at.
U = app/usage/page.tsx:81–96,900–973: status, requestId, createdAt, costUsd, raw.metadata.status_code / raw.status_code.
H = lib/health-presentation.ts:20–49: enabled, observationAvailable, health.observation, health.tone, backendStatus.
S = app/status/status-client.tsx:347–499: query errors, current_health.observation_available, signals[].source/reasons, incomplete, provider.enabled/reachable/keyState.
C = app/engines/engine-checks.tsx:87–146: probe.ok/status/error, detected.ok/models/error, mutation error.
J = app/try/workflow-media.ts:12–19,117–157 and app/try/page.tsx:1976–2008: job.status, requestId, error.
These are UI-consumed evidence, not independently verified backend semantics.

Classification
Normalize each observation in its own context: page-load, action, receipt, job, health or connection-check. Never promote a page-load error into an engine outage. Precedence within an action: recognized explicit code, then HTTP status, then narrowly typed local validation, then unknown. Receipt/job outcome remains the headline; HTTP evidence adds a qualified detail. Contradictory evidence displays “Recorded details disagree; cause not identified.” Never regex free-form messages into a diagnosis. Unknown codes retain only a sanitized technical identifier.

Copy matrix
Each cell is exact one-sentence “what happened”; then next-step owner and one action label. O = owner, M = household member, B = builder. “Help” opens a redacted preview to copy, never sends anything automatically.

1. Unusable link: link_unavailable; invite_unavailable/expired/revoked/consumed/used/not_found (E:35–61).
O: “This link can no longer be used.” Owner; Review access.
M: “This link can no longer be used.” Owner; Ask for a new link.
B: “The supplied link is unavailable.” Owner; Copy help details.
No edits to excluded routes are specified.

2. Access removed: account_disabled / credential_revoked (E:62–67).
O: “Access for this account or key is no longer available.” Owner; Review access.
M: “Your access is no longer available.” Owner; Copy help details.
B: “The account or app key no longer has access.” Owner; Copy help details.

3. Sign-in required: session_expired / authentication_required; browser-context HTTP 401 (E:68–73).
O: “Sign in to continue this action.” You; Sign in.
M: “Sign in to continue.” You; Sign in.
B: “This browser action requires sign-in.” You; Sign in.
For app-request 401, all personas: “The request was not authorized.” Owner/app configuration; Copy help details. Do not claim the browser session expired.

4. Permission denied: HTTP 403 (E:74–84).
O: “This action was not permitted.” Owner; Review access.
M: “You do not have access to this action.” Owner; Copy help details.
B: “The request was denied permission.” Owner; Copy help details.

5. Configuration conflict: generation_mismatch / configuration-context 412 (E:85–89).
O: “The engine configuration changed before this save.” You; Refresh configuration.
M: “The configuration changed before this save.” Owner; Copy help details.
B: “The configuration version no longer matches.” You; Refresh configuration.
Preserve unsaved input; never auto-reapply. A 412 outside this context uses unknown HTTP failure.

6. Rate limit: HTTP 429 (E:90–94).
O: “The request was rate-limited; the limiting service is not identified.” You; Review receipt.
M: “This request was limited; wait before trying again.” You; Review request.
B: “A service returned HTTP 429; its identity is not recorded here.” You; Review receipt.
No invented countdown or automatic retry.

7. Server response failure: HTTP 500–599 (E:95–96).
O: “A service returned an error; the failed layer is not identified.” Owner; Review receipt.
M: “Your request returned an error.” Owner; Copy help details.
B: “The request returned HTTP [status]; its source is unconfirmed.” Builder; View technical details.

8. Fetch/read/check incomplete: scoped fetch TypeError, query error, check mutation error, absent observation (E:97–101; S; C:103–110,135–146).
O: “We couldn’t complete [check/load].” You; Check again.
M: “We couldn’t load [item].” You; Reload [item].
B: “The [operation] did not return usable evidence.” You; Check again.
Cause unknown; preserve prior data and its historical label.

9. Rejected receipt (R/U status=rejected).
O: “The gateway recorded this request as rejected.” Owner; Review receipt.
M: “Your request was not accepted.” Owner; Copy help details.
B: “The receipt records rejection without a confirmed cause.” Builder; View technical details.

10. Engine/provider outcome (R/U backend_failed).
O: “The receipt records an engine or provider failure.” Owner; Review receipt.
M: “The AI service could not finish this request.” Owner; Copy help details.
B: “The receipt records backend_failed without identifying the exact failed layer.” Builder; View technical details.

11. Generic failure (R/U failed,error; J failed).
O: “The request was recorded as failed.” Owner; Review receipt.
M: “Your request did not finish successfully.” Owner; Copy help details.
B: “The recorded outcome is failed; cause is unclassified.” Builder; View technical details.

12. Timeout (R/U timed_out).
O: “No result was recorded before the request timed out.” Owner; Check latest outcome.
M: “The request took too long; completion is unconfirmed.” You; Check latest outcome.
B: “The receipt records a timeout, not proof that execution stopped.” Builder; Check latest outcome.

13. Disconnection (R/U disconnected).
O: “The connection ended before completion was recorded.” Owner; Check latest outcome.
M: “The connection ended; your result may still be processing.” You; Check latest outcome.
B: “The connection ended without a confirmed final result.” Builder; Check latest outcome.

14. Cancellation (R/U cancelled; J cancelled,canceled).
O: “The request was recorded as cancelled.” No fault assigned; Review receipt.
M: “This request was cancelled.” No fault assigned; Review request.
B: “Cancellation was recorded; the actor is not identified.” Builder; Review receipt.

15. Health unavailable/degraded (H down/degraded, or up with bad/warn tone; S provider.reachable=false; C probe.ok=false).
O: “[Engine/provider] was [unavailable/degraded] in this observation.” Owner; Check [name].
M: “The AI service needs attention.” Owner; Copy help details.
B: “The latest observation reports [state]; request-specific impact is unconfirmed.” Owner; Review status.
Use “Connection check failed” for probe.ok=false, not a proven outage.

16. Provider configuration missing (S keyState=missing, enabled=true).
O: “[Provider] is missing its configured access key.” Owner; Review provider.
M: “This AI service needs the owner to finish setup.” Owner; Copy help details.
B: “The provider observation reports missing access configuration.” Owner; Review status.

17. Health signal (S signals.source/reasons).
O: “[Source] reported a health warning.” Owner; Review status.
M: “The shared AI service reported a warning.” Owner; Copy help details.
B: “A [source] warning was observed; request impact is unconfirmed.” Owner; Review status.
Only API-source warnings use Gateway; only explicit host/storage sources use corresponding labels.

18. Unknown/unclassified (unrecognized/missing code/status; R/U incomplete,stale_incomplete; unsupported J status).
O: “The available evidence does not identify the outcome or cause.” Owner; Check latest outcome.
M: “We don’t have a confirmed result yet.” You; Check latest outcome.
B: “No recognized final outcome or diagnosis was recorded.” Builder; View technical details.

Non-failure states
Keep success/succeeded/ok → Completed; accepted → Accepted, not completed; running/in_progress → In progress. J succeeded/completed means completion recorded, not necessarily output downloaded. H busy/starting remain Busy/Starting; idle/standby_stopped/disabled are neutral configuration/availability states, not incidents. detected.ok=true with empty models means “No model names detected”, not engine failure. Missing observation means Not checked. Local required-input errors point to the exact field and say “Choose [input] before sending”; no request-failure banner.

Component and layout
FailureNotice props: context, audience(owner/member/builder), evidence{code?,httpStatus?,outcome?,observation?,observationAvailable?,source?,requestId?,observedAt?,checkedAt?,subject?}, variant(inline/receipt/banner), primaryAction{label,href|onClick}, busy, safeDetails. Resolver returns classification, sentence, responsibility, causeConfidence and action kind. Audience changes wording, never access permissions. Context-specific adapters supply verified navigation/callbacks; do not fabricate missing links or IDs.

1440px: one 720px-wide notice beneath the affected title/form; headline plus state, sentence, “Next step: [responsibility]”, primary button; receipt facts alongside; Technical details collapsed below.
390px: full-width single column with 16px inset; identical evidence and action, wrapping names/IDs, minimum 44px controls. Never hide outcomes or charges on mobile.

Data gaps
Existing fields above suffice for conservative classification. Needs backend field failure_layer for precise attribution, retry_after_seconds for countdown, retry_safe for safe resubmission, cancelled_by for actor, and observed_at where no measurement time exists. Until supplied, show unknown; browser checkedAt must not masquerade as observedAt. Preserve request cost separately: absent cost is not free.

Adoption/removals
Replace duplicate receipt logic: activity/requests.tsx:60–157,403–462; usage/page.tsx:731–842,900–973. Central entry points: lib/error-copy.ts:9–102; components/product/error-state.tsx:8–88. Health: status/status-client.tsx:108–111,185–234,347–499,503–599; engines/engine-checks.tsx:87–161,187 onward; engines/page.tsx:600–607. Try: page.tsx:1261–1285,1336–1346,1362–1408,1976–2008. Home: page.tsx:308–330. From completed copy reports: providers/page.tsx:1338–1343; showcase/page.tsx:184–188. Shared ErrorState consumers inherit the same policy; all custom error overrides require this adapter. Services was not read. Exhaustiveness beyond these inspected/report-supported integration points is not certified.

Remove raw provider-error primary copy, generic “Check your connection,” unsupported key blame, blanket 5xx/429 gateway attribution, guessed Decisions-disabled diagnosis from 404, and technical-JSON-only recovery. Preserve safe codes, request IDs, progressive disclosure, explicit incomplete states and reachability-versus-model distinction.

Accessibility and acceptance
Use role=status for refresh/progress; alert only for a newly failed deliberate action. Link errors to inputs; move focus to first invalid input, restore drawer focus, retain focus during updates. Never rely on color; respect reduced motion. Verify every matrix row, precedence conflicts, missing IDs, stale results, unknown codes and both widths. No automatic paid resubmission.

Evidence/limits
Static bounded reads and four terminal reports only; no backend, excluded routes, Services, runtime, shell, writes, metered calls or checks. Final get: active, no cancellation/revocation/owner notes. Workspace: diff_bytes=0; checks=[].
