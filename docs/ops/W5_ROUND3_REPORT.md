# W5 round 3 verification report

Branch: `engine/w5-r3`. Base: `7a288436`. Merged `origin/main` r65 (`5f9a825f`) and W1 round 2 (`c6f6330a`).

## UX review

| Item | Result | Evidence or limit |
|---|---|---|
| H1 | Fixed | On/Off is separate from connection health. Missing result/time is Unknown. Component tests cover enabled/failed and enabled/unknown. |
| H2 | Fixed | Server receipts and configuration fingerprints bind saved proof. Draft results cannot be persisted. Changing settings invalidates proof, including changes through other registry routes. Tests cover each connection input, selected model, forged results, discard/reload, and edits during a running test. |
| H3 | Fixed | Both server probe routes reject unsupported saved protocols and mismatched server types. The page disables checking with a reason. |
| M1 | Fixed | Each saved operation has a method and destination. Live connection-check requests have a separate label. |
| M2 | Partial | Saved workflow tasks, inputs, options, loading/selection, adjustments and evidence are inspectable. Running an arbitrary workflow sample is deferred: the page has no safe input form or supported execution contract for these records. It explains that limit. No ComfyUI execution claim. |
| M3 | Fixed | Inference and control key facts are independent of editable key fields. Missing destinations stay unknown. |
| M4 | Fixed | Configured method, inventory meaning, result source/time and latest versus saved connection checks are distinct. Missing/unsupported evidence stays unknown. |
| M5 | Fixed | Checks/resources have specific read-only explanations. No alternative editor is invented. Unsupported fields explicitly have no editor on this page. |
| M6 | Partial | Declared settings file path, role, application kind and reload arguments are shown. Arbitrary command arguments cannot reliably identify unit/compose files. File-content access and a general owner-file editor are deferred; neither is provided by the saved contract/API. Moving a file is not described as changing its saved path. |
| M7 | Fixed | Read-only users can search models. Mutating controls remain disabled. |
| M8 | Fixed | Preserved conditions/options are displayed. Unknown built-ins do not claim a known effect. Empty state no longer promises untranslated requests. |
| M9 | Fixed | Phone adjustment pairs stack with visible labels, long IDs wrap, command rows fit, and the save bar is static at phone widths. |
| L1 | Fixed | Discovery says Find new models, matching its append-only behavior. |
| L2 | Deferred | Existing environment-key bindings remain visible and preserved. Changing/removing them needs explicit backend semantics; this round adds no cosmetic removal control. |

## Round 2 independent re-review

| Item | Result | Evidence |
|---|---|---|
| HIGH 1: administrator metadata saves | Fixed | Explicit owner-only comparisons and metadata path preserve untouched configuration. Disabled/note regressions assert exact equality. |
| HIGH 2: URL passwords/query secrets | Fixed | Shared redaction covers values, stored and organized records, request URL and every operation URL. |
| HIGH 3: health paths after save | Fixed | Flagged health requests preserve explicit-base or host-root semantics. Five URL cases and actual health waits cover this. |
| HIGH 4: merged hidden headers | Fixed | W1 merged. Header markers restore once, bound to their stored names. Unchanged/removal/reorder/rename cases cover the two representations. |
| MED 5: permissions before traffic | Fixed | Changed nonowner connection updates and probes stop before outbound calls. Sparse unchanged checks still work. |
| MED 6: D20 timeout migration | Fixed | Schema 11 removes numeric 600 once on hop-routed records. 120, absent, string, and direct cases stay unchanged; resetting 600 after upgrade survives. See ENGINE_CONNECTION_UPGRADE.md. |
| MED 7: readiness with list commands | Fixed | Readiness edits preserve the command arrays. |
| LOW: key withheld without explanation | Fixed | Engine attention explains unavailable environment keys and origin mismatch. |
| Added HIGH: page flattens command arrays | Fixed | State, argument controls and payload preserve arrays. Unit and desktop/390 px browser tests edit/save/reload paths containing spaces and commas. |
| Independent round 3 check | Fixed | Administrators cannot forge saved tests/checks through the whole-registry API. Only the internal verification writer can change those facts. |

## Copy ledger

Accepted means the requested meaning was applied; wording can differ slightly to fit the control.

| # | Decision |
|---|---|
| 1 | Accepted: On plus separate health. |
| 2 | Accepted: names the attempted on/off action. |
| 3 | Accepted: explicitly tested saved settings. |
| 4 | Accepted: explicitly tested unsaved changes; no save. |
| 5 | Accepted: previous saved settings and actual result time, not engine creation. |
| 6 | Deferred: the backend does not prove an app-facing test route. No result claims that route. |
| 7 | Accepted: concise saved-settings progress. |
| 8 | Accepted: gateway-computer progress. |
| 9 | Accepted: failure asks the reader to inspect the reason. |
| 10 | Accepted: capability/model, time and saved/draft context replace broad proof. |
| 11 | Accepted: one request with the selected model. |
| 12 | Accepted: secrets-hidden request trace. |
| 13 | Accepted: enter an address to preview the connection check. |
| 14 | Accepted: Connection check label. |
| 15 | Accepted: all saved destinations. |
| 16 | Accepted: start when needed then forward. |
| 17 | Accepted: direct request description. |
| 18 | Accepted: saved connection source in plain words. |
| 19 | Accepted: unknown request destination. |
| 20 | Accepted: inference key and separate control key. |
| 21 | Accepted: absent key versus unknown destination. |
| 22 | Accepted: no key sent for this check. |
| 23 | Accepted: model/models found. |
| 24 | Accepted: Find new models. |
| 25 | Accepted: list/find/add by server name without implying automatic start. |
| 26 | Accepted: public model names and engines are the sources. No unverified editor link. |
| 27 | Accepted: section-specific limits and no invented editor. |
| 28 | Superseded: argument arrays now have a lossless editor. Unsupported drivers still get a specific reason. |
| 29 | Accepted: saved protocol mismatch explains read-only fields. |
| 30 | Accepted: models/workflows and selection limits. |
| 31 | Accepted: no models/workflows listed. |
| 32 | Accepted: selected model not verified. |
| 33 | Accepted: app request model field chooses. |
| 34 | Adapted: “Request used to switch models.” The shared glossary bans “operation” in interface copy. |
| 35 | Accepted: process manager owns the server. |
| 36 | Accepted: stored process configuration chooses file location. |
| 37 | Accepted: unknown idle delay stays unknown. |
| 38 | Accepted: older configuration explained; identifier only in technical detail. |
| 39 | Accepted: declared method/path/status/selector clauses. |
| 40 | Accepted: unsupported check method explained. |
| 41 | Accepted: no configured check means unknown. |
| 42 | Accepted: Verified by a check. |
| 43 | Accepted: estimates are not enforced limits. |
| 44 | Accepted: known resource labels mapped; unknown shapes remain explicit technical details. |
| 45 | Accepted: Organized engine settings. |
| 46 | Accepted: plain description of the organized record. |
| 47 | Accepted: saved settings have no editor here. |
| 48 | Accepted: removal stops this engine's contribution; other engines can retain names. |
| 49 | Accepted: record-page access error suggests retry/Access plans. |
| 50 | Accepted: workflow-aware access heading. |
| 51 | Accepted: Name apps use. |
| 52 | Accepted: Context limit (tokens). |
| 53 | Accepted: explains token capacity, without promising enforcement. |
| 54 | Accepted: no extra adjustments configured. |
| 55 | Accepted: built-in behavior unknown unless described. |
| 56 | Accepted: unsupported saved adjustment is read-only. |
| 57 | Accepted: matching chat requests plus preserved conditions. |
| 58 | Accepted: separate minimum/maximum/both summaries. |
| 59 | Accepted: commands ask the process manager. |
| 60 | Accepted: save does not start the server. |

## Acceptance evidence

| Items | Regression files |
|---|---|
| H1, H2, H3, M7 | `web/src/app/engines/record/engine-r3-honesty.spec.tsx`: enabled states; protocol mismatch; search; draft path/header/model tests, Discard and reload; edit while test is running. |
| H2, H3 and rereview HIGH 2/4, MED 5/7, LOW | `tests/test_engine_w5_r3_evidence.py`: 39 cases including credentials, paths, headers, timeout, adjustments, models, opaque receipts, alternate save paths and exact argument arrays. |
| M1–M6 and resources | `web/src/app/engines/record/engine-sections.spec.tsx`: 23 cases including nine added contract-facts cases. |
| M8, M9 | `web/src/app/engines/record/adjustments.spec.ts` and `more-options.spec.tsx`: preserved conditions, unknown built-ins, bounds, readable phone pairs. |
| Rereview HIGH 1/3 and MED 6 | `tests/test_engine_w5_runtime_rereview.py`: metadata equality, five health-path cases and idempotent migration. |
| Administrator evidence bypass | `tests/test_engine_w5_evidence_permissions.py`: both forbidden evidence writes plus allowed metadata roundtrip. |
| Added command-array HIGH | `command-array-r3.spec.tsx` and the two `engines-real.spec.ts` contract journeys: argument boundaries, hidden token marker, readiness-only saves, actual save/reload at 1280/390 px. |

Test-only commits precede their fixes. Recorded failing runs include 30 backend review failures, 11 adjustment failures, 12 local contract-facts failures, the two raw-evidence permission failures, three model-configuration failures, and the missing credential-origin failure. The mobile journeys also failed on missing workflow inputs before the contract-details patch.

## Section 9 matrix

| Contract item | Result |
|---|---|
| 1. Connection | Checked: editable fields, saved destinations, keys and mismatch gates. |
| 2. Models/workflows | Inspectable; arbitrary workflow sample execution deferred under M2. |
| 3. Process control | Owner and command arrays checked; general owner-file discovery/content deferred under M6. |
| 4. Checks | Saved/session evidence distinguished; unsupported methods explained without execution claims. |
| 5. Resources | Nested values readable; every shape states enforcement or uncertainty. |
| 6. Stored record | Two read-only, redacted representations have plain descriptions. |
| 7. Add flow | Desktop/390 px add/check/test/edit journeys pass through the same API. Generic workflow input editing remains deferred. |
| 8. Editable effects | Payload/persistence and inference-connection tests pass. Arbitrary workflow/process execution from every control remains unverified. |
| 9. Unused settings | Unsupported controls and conditional fields are hidden; unsupported checks disabled. |
| 10. Derived/system values | Read-only with source/reason; backend permissions tested. |
| 11. Request destinations | All supplied operation/method/URL rows shown as saved. |
| 12. Key destinations | Inference/control shown separately; unknown destination remains unknown. |
| 13. Selected model | Selection evidence, source and time shown; absent proof explicitly unverified. |
| 14. Process owner | External manager, owned process and unknown are distinct. |
| 15. Check evidence | Configured request, meaning, result, source and time shown where available. |
| 16. Test trace | Method, URL, hidden headers, body and timeout shown; exact wire-byte identity unverified. |
| 17. Owner files | Declared file path/role/reload shown read-only; broader disclosure deferred under M6. |
| 18. Read-only explanations | Specific process/check/resource reasons; unsupported editing has no invented destination. |

## Verification

| Check | Measured result |
|---|---|
| Full hermetic Python | 11,517 passed, 3 skipped, 1 expected failure; 3,517 subtests passed; 107.38 s. Collected-case ceiling: 11,521. |
| Full web unit suite | 2,720 passed. |
| Final focused engine/glossary suite | 201 passed after the final display/selector changes. |
| Affected Playwright | All 30 distinct affected journeys passed across the verification runs. The first run had 27 passes and three stale selector/redaction assertions; after correcting them, all six cases in the affected two files passed. Both 1280/390 px command/workflow save/reload journeys passed. Retries are disabled. |
| TypeScript | `tsc --noEmit` passed. |
| Production build | Passed, including lint/type checks and export-shape check: 141 files, 21 pages. Existing warnings remain in unrelated files. |
| Count ceilings | `pytest=11521`, `vitest=2720`, `playwright=463` (445 main + 3 live-data + 15 household). Node ceiling unchanged at 87. |
| Independent review | Different-model re-review passed after the raw-registry evidence bypass was closed. It did not run tests. |
| UI inspection | Chromium screenshots at 390 px show stacked labeled pairs and long workflow IDs without page overflow. Mechanical design scan returned no findings. |

Commands use `scripts/run-hermetic-pytest.sh -q -n 12`, `vitest run`, `tsc --noEmit`, `npm run build`, and the five affected Playwright files with `UI_E2E_PORT=18300` and a separate `UI_E2E_STATE_ROOT`. Full browser-gate counts came from all three Playwright configs with `--list`; the remaining 433 browser cases were not executed in this round.

The final Python source/test snapshot is identical to the passing run at `0c3a0380`. Later changes concern UI display, browser assertions, documentation and measured ceilings. Local logs and JUnit evidence are under `/home/tnunamak/.tmp/w5-r3-*`; screenshots are in `web/test-results/engine-contract-390-inputs.png` and `engine-contract-390.png`.


## Limits

Draft tests deliberately do not carry over on save; run the test again after saving. Server-issued receipts waiting to be saved expire on gateway restart. Persisted proof survives restart only while the effective settings still match.

The browser checks use Chromium with a 390 px emulated viewport. They do not prove physical-phone keyboard behavior, thirty-second comprehension, actual ComfyUI execution, the separately built W1 round 3 server rejections, or exact wire-byte trace identity. This round does not deploy or restart the live gateway and does not use ports 5000/15000.
