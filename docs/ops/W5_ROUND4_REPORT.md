# W5 round 4 verification report

A secret edit must not restore a value from a marker or list position. The engine page now reads configured metadata and sends explicit commands against stable entry IDs. Secret inputs stay empty after reload. Ordinary settings and secret batches require separate saves because the API has no combined transaction.

## Requested changes and evidence

| Item | Result and evidence |
| --- | --- |
| Secret commands | Headers, env, inference keys, existing lifecycle keys and URL credentials use replace/clear commands; unchanged entries are omitted. Argv commands identify retained/replaced entries by ID and explicitly clear dropped entries. Component tests cover cancellation, toggles, revision races, reserved/empty values and endpoint guards. `tests/test_engine_w5_r4_backend.py` has 34 regression cases for atomic refusal, origin binding, projection privacy and routing. |
| Structured argv | One field per argument, Program/Argument labels, a secret toggle and no-shell help. Replacement keeps the entry ID and public prefix. Tests cover retained identity, reordering and secret-to-public replacement. |
| Seven UX residuals | MED-1/LOW-7 require timestamped historical evidence; MED-2 separates transmission from acceptance; MED-3 reports availability; MED-4 uses the latest matching check; LOW-5 explains argv; LOW-6 uses each result’s own note. The contract-residuals, capabilities-residuals and credential-availability component tests cover these corrections. Header copy now describes secret storage. |
| Installed protocols | Both forms select validated bundled/user descriptions without free-text IDs. Setup saves off, preserves unused legacy settings and shows actual protocol destinations. Tests reject legacy probes before traffic and invalidate evidence when dispatch changes. |
| Hidden settings | The page shows `hidden_settings` without exposing unknown values; component and backend projection tests cover it. |
| Browser journeys | `web/e2e/engine-secret-entries.spec.ts` uses real management routes at 1280px and 390px to edit/save/reload headers, env and argv. Both journeys check blank secret inputs, stable IDs, retained arguments and test secrets absent from management reads. The phone case checks overflow. |

## Validation

The final full hermetic run at `05c52321` passed: **12,478 passed, 3 skipped, 1 expected failure; 3,524 subtests passed** (249.67 s). There are **12,482 collected Python cases**. Backend source is unchanged in the later frontend checkpoint `6aca9c15`.

Focused checks passed 338 backend cases at `e804da6e` and 33 route-method cases at `12dedb90`. The final frontend run at `6aca9c15` passed **2,801 Vitest cases in 189 files** (110.69 s). TypeScript, changed-file ESLint and the production build passed. The isolated native-mode regression check failed 10 cases before applying the final source, then passed all 58 cases. Both required Playwright journeys passed at `6aca9c15` (1280px and 390px, 44.4 s), including cleanup. The run used port 18300 and an explicit isolated `UI_E2E_STATE_ROOT`. Playwright inventory is **465** across the three gate configurations; the requested secret journeys exercise two viewport sizes.

## Limits and dependency

Phase 1 round 2 (`a4a6e55a`) is merged; no task is waiting for it. Management reads do not decrypt ciphertext. Protocol description validation does not prove a live request succeeds, and this page has no protocol Check/Test executor. Native control credential references remain read-only because the current Phase 1 contract provides no command carrier for them. Creating an absent process block, arbitrary workflow sample execution and owner-file content editing remain outside this form.
